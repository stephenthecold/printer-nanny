"""Self-update: pip install the agent package, then exit so the service
manager (systemd / NSSM) restarts the process against the freshly-installed
code.

Triggered by the `update_agent` command type pulled on heartbeat. The
heavy lifting (pip subprocess) runs detached so a failed install doesn't
kill the running agent -- we only exit when pip reports success.

The result of every update attempt is written to a small JSON marker file
next to the package install (``.pn-update-result.json``). The agent reads
this file on startup and forwards the result in its next heartbeat so the
operator can see whether the LAST attempted update actually succeeded --
no log-spelunking required.

Hardening (so a bad config or a flaky network can't crash-loop the service):

* **Pre-flight** before pip ever runs -- a configured/non-empty pip source,
  an invokable pip, and a plausible target. A pre-flight failure records a
  clear result and returns WITHOUT exiting/restarting.
* **Structured result** -- ``{ok, status, old_version, new_version, error, ts}``
  (plus a legacy ``detail`` string for older dashboards). After pip installs,
  the BASE version is re-read off disk so ``new_version`` reflects what
  actually landed; ``new == old`` flags a no-op/failed update even when pip
  exits 0.
* **Atomic write** -- temp file + ``os.replace`` so an interrupted update never
  leaves a half-written / corrupt marker; the last result is always coherent.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("printer_nanny_agent.updater")

# The result-marker file. Lives in the agent's install directory so it
# survives across pip reinstalls (pip wipes the package dir, but writes new
# files; the marker is at the parent of __file__ so it's only blown away
# during a full uninstall).
_RESULT_FILE_NAME = ".pn-update-result.json"

# Placeholder pip source shipped in the sample config / docs. It will never
# install -- treat it as a pre-flight failure rather than letting pip churn.
_PLACEHOLDER_MARKER = "your-org"

# Cap on persisted error/detail text so a multi-MB pip stderr can't bloat the
# heartbeat payload the dashboard renders.
_DETAIL_CAP = 1024


def _result_path() -> Path:
    """Where we record the most recent update attempt's outcome.

    Stored at the parent of the package so pip --force-reinstall doesn't
    wipe it (pip only replaces the package contents).
    """
    return Path(__file__).resolve().parent.parent / _RESULT_FILE_NAME


def _now_iso() -> str:
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _current_base_version() -> str:
    """The base version of the package as loaded in THIS process.

    Captured before the update so it can be compared against the on-disk
    version after pip runs.
    """
    try:
        from printer_nanny_agent import __base_version__

        return str(__base_version__)
    except Exception:  # noqa: BLE001 - never let version capture break an update
        return ""


def _installed_base_version() -> str:
    """Re-read ``__base_version__`` from the package's ``__init__.py`` ON DISK.

    The running process imported the OLD module at startup, so its in-memory
    ``__base_version__`` won't change after pip --force-reinstall. To confirm
    the install actually landed (and detect a no-op where new == old), parse the
    constant straight out of the freshly-written source file instead of relying
    on the stale import.

    Returns "" if the file is unreadable or the constant can't be found, which
    callers treat as "couldn't confirm" rather than a hard failure.
    """
    init_path = Path(__file__).resolve().parent / "__init__.py"
    try:
        text = init_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("self-update: could not re-read installed version: %s", exc)
        return ""
    match = re.search(
        r"""__base_version__\s*=\s*['"]([^'"]+)['"]""",
        text,
    )
    return match.group(1) if match else ""


def _write_result(
    status: str,
    *,
    ok: bool,
    old_version: str = "",
    new_version: str = "",
    error: Optional[str] = None,
    detail: str = "",
) -> dict:
    """Atomically persist the latest update attempt's outcome.

    The payload is structured (``ok``/``status``/``old_version``/``new_version``/
    ``error``/``ts``) so the dashboard can confirm the install landed, plus a
    legacy ``detail`` string so older central builds that only read ``detail``
    keep working.

    Written via a temp file + ``os.replace`` so an interrupted update never
    leaves a partially-written / corrupt marker -- the previous coherent result
    survives, or the new one fully replaces it; never an in-between.

    Returns the payload dict so callers (and the smoke harness) can assert on it
    without a re-read.
    """
    # error and detail carry the same human-readable text on the failure paths;
    # keep both populated (error=null on success) and cap each.
    err_text = None if error is None else str(error)[:_DETAIL_CAP]
    payload = {
        "ok": bool(ok),
        # "ok" | "pip_failed" | "pip_timeout" | "no_pip" | "spawn_failed"
        # | "no_pip_source" | "preflight_failed" | "no_op"
        "status": status,
        "old_version": old_version or "",
        "new_version": new_version or "",
        "error": err_text,
        "detail": (detail or err_text or "")[:_DETAIL_CAP],
        "ts": _now_iso(),
    }
    path = _result_path()
    try:
        # Temp file in the SAME directory so os.replace is an atomic rename
        # (cross-filesystem renames aren't atomic / can fail).
        fd, tmp_name = tempfile.mkstemp(
            prefix=_RESULT_FILE_NAME + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except OSError:
            # Clean up the temp file on failure so we don't litter the dir.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.warning("self-update: could not write result marker: %s", exc)
    return payload


def read_last_update_result() -> Optional[dict]:
    """Return the most recent update-attempt result, or None if no attempt
    has been recorded (or the marker file is malformed).

    Called once at agent startup; the result rides along on the next
    heartbeat so the central UI can show 'last update: ok at X' or
    'FAILED: pip exit 1: ...'.
    """
    path = _result_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _pip_path() -> Path:
    """Resolve pip.exe / pip next to the current Python.

    The agent is always launched from its own venv (the installer creates
    .venv/bin/printer-nanny-agent / Scripts\\printer-nanny-agent.exe), so
    sys.executable is the venv python and pip lives next to it.
    """
    py_dir = Path(sys.executable).resolve().parent
    candidate = py_dir / ("pip.exe" if os.name == "nt" else "pip")
    return candidate


async def _pip_invokable(pip: Path, timeout_seconds: float = 30.0) -> Tuple[bool, str]:
    """Pre-flight: confirm pip actually runs (``pip --version`` exits 0).

    A pip binary can exist on disk yet be unusable (broken venv, wrong perms,
    interpreter shebang pointing at a deleted python). Catching that here keeps
    a misconfigured host from looping through a doomed force-reinstall.
    """
    if not pip.exists():
        return False, f"pip not found at {pip}"
    try:
        proc = await asyncio.create_subprocess_exec(
            str(pip), "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return False, f"failed to spawn pip: {exc}"
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        return False, f"pip --version timed out after {timeout_seconds}s"
    if proc.returncode != 0:
        snippet = (stderr or b"").decode("utf-8", errors="replace")[:256]
        return False, f"pip --version failed (exit {proc.returncode}): {snippet}"
    return True, ""


def _plausible_pip_source(source: str) -> bool:
    """Cheap sanity check that ``source`` is something pip could install.

    Accepts the shapes the installer/docs actually use -- a VCS URL
    (``git+...``), an http(s) wheel/sdist URL, a local path that exists, or a
    plain PyPI requirement (``printer-nanny-agent`` / ``pkg==1.2.3``). Rejects
    obvious junk like a leading dash (which pip would read as a flag) or
    embedded whitespace.
    """
    if source.startswith("-"):
        return False
    if any(ch.isspace() for ch in source):
        return False
    if source.startswith(("git+", "hg+", "bzr+", "svn+")):
        return True
    if source.startswith(("http://", "https://")):
        return True
    if os.path.exists(source):
        return True
    # Bare requirement: a PEP 508 name, optionally with an extras/version/marker
    # tail. Just require it to start with a name-like token.
    return bool(re.match(r"^[A-Za-z0-9._-]+", source))


async def preflight(pip_source: Optional[str]) -> Tuple[bool, str]:
    """Run all checks that must pass BEFORE we touch pip install.

    Returns (ok, reason). On ``ok=False`` the caller records a failure result
    and returns WITHOUT exiting -- a misconfig must never crash-loop the
    service through repeated doomed reinstalls + restarts.

    Checks, cheapest first:
      1. pip_source is configured / non-empty and not the docs placeholder.
      2. The target is plausible (looks like a pip-installable spec, not e.g.
         a stray shell flag).
      3. pip is invokable (``pip --version`` exits 0).
    """
    source = (pip_source or "").strip()
    if not source:
        return False, "no pip_source configured"
    if _PLACEHOLDER_MARKER in source:
        return False, f"pip_source is the unconfigured placeholder ({source!r})"
    if not _plausible_pip_source(source):
        return False, f"pip_source does not look installable: {source!r}"
    pip = _pip_path()
    ok, reason = await _pip_invokable(pip)
    if not ok:
        return False, reason
    return True, ""


async def run_self_update(pip_source: str, timeout_seconds: float = 300.0) -> Tuple[bool, str]:
    """Run pip install --force-reinstall --no-deps <pip_source>.

    Returns (ok, detail). On failure, ``detail`` carries the truncated pip
    stderr (or a synthetic reason like "pip not found") so the next heartbeat
    can surface it.

    Assumes :func:`preflight` already passed -- it still re-checks pip presence
    defensively so a direct caller can't trip on a missing binary.
    """
    pip = _pip_path()
    if not pip.exists():
        msg = f"pip not found at {pip}"
        log.error("self-update: %s", msg)
        return False, msg
    cmd = [
        str(pip), "install", "--force-reinstall", "--no-deps", "--quiet", pip_source,
    ]
    log.info("self-update: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        msg = f"failed to spawn pip: {exc}"
        log.error("self-update: %s", msg)
        return False, msg
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        msg = f"pip install timed out after {timeout_seconds}s"
        log.error("self-update: %s", msg)
        return False, msg
    if proc.returncode != 0:
        snippet = (stderr or b"").decode("utf-8", errors="replace")[:2048]
        msg = f"pip install failed (exit {proc.returncode}): {snippet}"
        log.error("self-update: %s", msg)
        return False, snippet
    log.info("self-update: pip install succeeded")
    return True, ""


def restart_for_service_manager(exit_code: int = 0) -> None:
    """Exit so systemd / NSSM restarts us with the new code.

    Uses os._exit so any in-flight asyncio tasks don't block shutdown. Service
    managers (systemd Restart=always, NSSM auto-restart) bring the process
    back within ~10 seconds against the freshly-installed package.
    """
    log.info("self-update: exiting (service manager will restart)")
    # Flush logs before the abrupt exit so operators see the message.
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(exit_code)


async def perform_self_update(pip_source: Optional[str]) -> None:
    """End-to-end: pre-flight -> install -> restart. Logs and returns on any
    failure so the agent keeps running on the OLD code rather than going dark.

    The outcome -- success or specific failure -- is written (atomically) to the
    result marker file so the NEXT heartbeat (after the service-manager restart,
    or on the current process if pip failed) can surface it on the dashboard.
    The marker records old/new base versions so the operator can confirm the
    install landed (and spot a no-op where new == old).
    """
    old_version = _current_base_version()

    # 1) Pre-flight. A misconfig (empty/placeholder source, dead pip) must NOT
    #    exit/restart -- that would crash-loop the service through repeated
    #    doomed reinstalls + restarts. Record a clear result and return.
    ok, reason = await preflight(pip_source)
    if not ok:
        # An explicitly-absent source keeps its dedicated status for the
        # dashboard; everything else is a generic pre-flight failure.
        status = "no_pip_source" if not (pip_source or "").strip() else "preflight_failed"
        log.warning("self-update: pre-flight failed (%s) -- skipping, agent stays up", reason)
        _write_result(
            status,
            ok=False,
            old_version=old_version,
            new_version=old_version,  # nothing installed -> unchanged
            error=reason,
            detail=reason,
        )
        return

    # preflight guarantees a non-empty source here.
    source = (pip_source or "").strip()

    # 2) Install.
    success, detail = await run_self_update(source)
    if not success:
        log.error("self-update: pip failed -- agent will keep running on old code")
        _write_result(
            "pip_failed",
            ok=False,
            old_version=old_version,
            new_version=old_version,  # install didn't land
            error=detail,
            detail=detail,
        )
        return

    # 3) Confirm what landed. Re-read the base version off disk (the in-process
    #    import is stale). new == old after a "successful" pip is a no-op --
    #    surface it as ok=False so the dashboard doesn't claim a phantom update.
    new_version = _installed_base_version() or old_version
    if new_version and old_version and new_version == old_version:
        msg = (
            f"pip reported success but installed version is unchanged "
            f"({old_version}) -- treating as a no-op"
        )
        log.warning("self-update: %s", msg)
        _write_result(
            "no_op",
            ok=False,
            old_version=old_version,
            new_version=new_version,
            error=msg,
            detail=msg,
        )
        # Still restart: the on-disk files were force-reinstalled (mtime/marker
        # changed) even if the base version is identical, so the service should
        # come back on the freshly-written code. A same-base reinstall is a
        # legitimate operation (e.g. re-pull a moving git ref).
        restart_for_service_manager(0)
        return

    _write_result(
        "ok",
        ok=True,
        old_version=old_version,
        new_version=new_version,
        error=None,
        detail="pip install succeeded; restarting via service manager",
    )
    restart_for_service_manager(0)
