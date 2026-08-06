"""Serve the agent install scripts so `curl https://central/install-agent.sh` and
`iwr https://central/install-agent.ps1 -OutFile $p; & $p` work.

Public (no auth) like any get.example.com bootstrap - the secret is the per-agent
API key passed as an argument by the operator, never embedded in the script.

Also mirrors the NSSM Windows service wrapper so the agent's Windows installer
only needs outbound HTTPS to this central server (the whole MSP architecture
promise). The first request fetches nssm.cc/release/nssm-2.24.zip into the
cache directory; every subsequent request is served from disk and nssm.cc can
go dark without affecting installs.
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from pathlib import Path

import httpx
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

from central import artifact_integrity as ai

router = APIRouter(tags=["installer"])
log = logging.getLogger("printer_nanny.installer")

_DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy"
_SCRIPT_PATH = _DEPLOY_DIR / "install-agent.sh"
_PS1_PATH = _DEPLOY_DIR / "install-agent.ps1"

# NSSM mirror - operators can override the upstream URL (e.g. for air-gapped
# deployments pointing at an internal artifact mirror) and the cache location.
# Resolved lazily so tests can redirect via env vars after import.
_NSSM_VERSION = "2.24"


def _nssm_upstream() -> str:
    return os.environ.get("PN_NSSM_URL", f"https://nssm.cc/release/nssm-{_NSSM_VERSION}.zip")


def _nssm_expected_sha256(arch: str = "x64") -> str:
    """Operator-pinned digest for an extracted nssm binary, or "".

    Per-arch (``PN_NSSM_SHA256_X64`` / ``PN_NSSM_SHA256_X86``) because the two
    binaries are different files; a single variable would have to mean one of
    them and would silently fail to protect the other. ``PN_NSSM_SHA256`` is
    accepted as an alias for the x64 pin, which is the one an MSI build uses.

    Env rather than a runtime setting deliberately: this module is a static
    mirror with no database dependency, and giving it one so a checksum could be
    typed in the Settings UI would make an unauthenticated bootstrap route open a
    DB session. The URL knob it pairs with (``PN_NSSM_URL``) is env for the same
    reason.
    """
    suffix = "X86" if arch == "x86" else "X64"
    return (
        os.environ.get(f"PN_NSSM_SHA256_{suffix}")
        or (os.environ.get("PN_NSSM_SHA256", "") if suffix == "X64" else "")
        or ""
    )


def _nssm_cache_dir() -> Path:
    return Path(os.environ.get("PN_CACHE_DIR", "/var/lib/printer-nanny/cache"))


def _nssm_cache_path(arch: str) -> Path:
    suffix = "x86" if arch == "x86" else "x64"
    return _nssm_cache_dir() / f"nssm-{_NSSM_VERSION}-{suffix}.exe"


@router.get("/install-agent.sh", response_class=PlainTextResponse)
def install_script() -> PlainTextResponse:
    try:
        body = _SCRIPT_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return PlainTextResponse("# install-agent.sh not found on server\n", status_code=500)
    return PlainTextResponse(body, media_type="text/x-shellscript")


@router.get("/install-agent.ps1", response_class=PlainTextResponse)
def install_script_ps1() -> PlainTextResponse:
    """Windows installer - operator runs:
       iwr -useb https://CENTRAL/install-agent.ps1 -OutFile $p; & $p"""
    try:
        body = _PS1_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return PlainTextResponse("# install-agent.ps1 not found on server\n", status_code=500)
    return PlainTextResponse(body, media_type="text/plain")


def _populate_nssm_cache() -> None:
    """Download nssm-X.YZ.zip from upstream and extract the two .exe binaries
    into the cache dir. Idempotent - does nothing when both binaries already
    exist. Raises on download / extract failure so the caller returns 503.

    Integrity: nssm.exe is the *service host* -- it is the process that runs the
    workstation agent as LocalSystem -- so a substituted binary here is SYSTEM
    code execution on every machine in a client's fleet. The transport is pinned
    to https (the URL is operator-overridable free text, so plaintext and a
    downgrading redirect were both previously accepted), and each extracted
    binary is pinned on arrival so later cache reads are verified rather than
    trusted for existing. Set ``PN_NSSM_SHA256_X64`` / ``_X86`` to verify the
    first fetch too. See ``central.artifact_integrity``.
    """
    x64 = _nssm_cache_path("x64")
    x86 = _nssm_cache_path("x86")
    if x64.exists() and x86.exists():
        return
    upstream = _nssm_upstream()
    ai.require_secure_url(upstream, label="the NSSM service wrapper")
    _nssm_cache_dir().mkdir(parents=True, exist_ok=True)
    log.info("populating NSSM cache from %s", upstream)
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        resp = client.get(upstream)
        resp.raise_for_status()
    ai.assert_secure_response_url(resp.url, label="the NSSM service wrapper")
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        # Layout inside the official zip: nssm-X.YZ/win64/nssm.exe + win32/nssm.exe
        x64_member = next(
            (n for n in zf.namelist() if n.endswith("win64/nssm.exe")), None
        )
        x86_member = next(
            (n for n in zf.namelist() if n.endswith("win32/nssm.exe")), None
        )
        if not x64_member or not x86_member:
            raise RuntimeError(
                f"NSSM zip from {upstream} did not contain expected members"
            )
        # Verify BEFORE the bytes land in the cache, so a rejected binary is
        # never written to a path something else may serve from. The pinned
        # digest is of the extracted .exe, not of the zip: the zip's name and
        # layout vary between mirrors, the binary an operator would checksum on
        # their own machine does not.
        payloads = {}
        for arch, member, dest in (
            ("x64", x64_member, x64), ("x86", x86_member, x86),
        ):
            data = zf.read(member)
            ai.verify_bytes(
                data,
                expected=ai.normalise_pin(_nssm_expected_sha256(arch)),
                label=f"nssm.exe ({arch})",
            )
            payloads[dest] = data
    for dest, data in payloads.items():
        dest.write_bytes(data)
        ai.write_pin(dest, ai.sha256_bytes(data))
    log.info("NSSM cache populated (%d bytes x64)", x64.stat().st_size)


@router.get("/install-agent-nssm.exe")
def install_nssm(arch: str = "x64") -> Response:
    """Mirror nssm.exe so Windows agents need only outbound HTTPS to central.

    First request downloads from nssm.cc and caches; later requests serve from
    disk. Survives nssm.cc outages and air-gapped redeployments. Pass
    ?arch=x86 for 32-bit Windows (rare on Server 2022; default x64).

    The cached binary is verified against its pin on **every** request, not only
    when it is fetched. This route hands an executable to a machine that is about
    to run it as a service, so "the file is on disk" is not a sufficient reason
    to serve it -- the cache is a volume that outlives any single process and is
    exactly what a pinned digest exists to detect changes in.
    """
    cache = _nssm_cache_path(arch)
    if not cache.exists():
        try:
            _populate_nssm_cache()
        except Exception as exc:  # noqa: BLE001 - any failure becomes a 503 for the agent
            log.exception("nssm mirror download failed")
            return PlainTextResponse(
                f"NSSM mirror unavailable: {exc}\n"
                f"Operator: download nssm-{_NSSM_VERSION}.zip from {_nssm_upstream()} "
                f"manually, extract win64/nssm.exe to {cache}, and retry.\n",
                status_code=503,
            )
    try:
        ai.verify_or_pin_file(
            cache,
            expected=ai.normalise_pin(_nssm_expected_sha256(arch)),
            label=f"nssm.exe ({arch})",
        )
    except ai.ArtifactIntegrityError as exc:
        # Refuse rather than serve. An agent that gets a 503 retries; an agent
        # that gets a tampered service host does not get a second chance.
        log.error("nssm mirror integrity failure: %s", exc)
        ai.discard(cache)
        return PlainTextResponse(
            f"NSSM mirror refused: {exc}\n"
            "The cached binary did not match its pinned checksum and has been "
            "removed. Retry to re-fetch it from upstream.\n",
            status_code=503,
        )
    return Response(content=cache.read_bytes(), media_type="application/octet-stream")
