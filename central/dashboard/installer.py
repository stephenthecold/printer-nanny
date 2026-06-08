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


def _nssm_cache_dir() -> Path:
    return Path(os.environ.get("PN_CACHE_DIR", "/var/lib/printer-nanny/cache"))


def _nssm_cache_path(arch: str) -> Path:
    suffix = "x86" if arch == "x86" else "x64"
    return _nssm_cache_dir() / f"nssm-{_NSSM_VERSION}-{suffix}.exe"


@router.get("/install-agent.sh", response_class=PlainTextResponse)
def install_script() -> PlainTextResponse:
    try:
        body = _SCRIPT_PATH.read_text()
    except OSError:
        return PlainTextResponse("# install-agent.sh not found on server\n", status_code=500)
    return PlainTextResponse(body, media_type="text/x-shellscript")


@router.get("/install-agent.ps1", response_class=PlainTextResponse)
def install_script_ps1() -> PlainTextResponse:
    """Windows installer - operator runs:
       iwr -useb https://CENTRAL/install-agent.ps1 -OutFile $p; & $p"""
    try:
        body = _PS1_PATH.read_text()
    except OSError:
        return PlainTextResponse("# install-agent.ps1 not found on server\n", status_code=500)
    return PlainTextResponse(body, media_type="text/plain")


def _populate_nssm_cache() -> None:
    """Download nssm-X.YZ.zip from upstream and extract the two .exe binaries
    into the cache dir. Idempotent - does nothing when both binaries already
    exist. Raises on download / extract failure so the caller returns 503."""
    x64 = _nssm_cache_path("x64")
    x86 = _nssm_cache_path("x86")
    if x64.exists() and x86.exists():
        return
    upstream = _nssm_upstream()
    _nssm_cache_dir().mkdir(parents=True, exist_ok=True)
    log.info("populating NSSM cache from %s", upstream)
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        resp = client.get(upstream)
        resp.raise_for_status()
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
        x64.write_bytes(zf.read(x64_member))
        x86.write_bytes(zf.read(x86_member))
    log.info("NSSM cache populated (%d bytes x64)", x64.stat().st_size)


@router.get("/install-agent-nssm.exe")
def install_nssm(arch: str = "x64") -> Response:
    """Mirror nssm.exe so Windows agents need only outbound HTTPS to central.

    First request downloads from nssm.cc and caches; later requests serve from
    disk. Survives nssm.cc outages and air-gapped redeployments. Pass
    ?arch=x86 for 32-bit Windows (rare on Server 2022; default x64).
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
    return Response(content=cache.read_bytes(), media_type="application/octet-stream")
