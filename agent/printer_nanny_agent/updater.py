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
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("printer_nanny_agent.updater")

# The result-marker file. Lives in the agent's install directory so it
# survives across pip reinstalls (pip wipes the package dir, but writes new
# files; the marker is at the parent of __file__ so it's only blown away
# during a full uninstall).
_RESULT_FILE_NAME = ".pn-update-result.json"


def _result_path() -> Path:
    """Where we record the most recent update attempt's outcome.

    Stored at the parent of the package so pip --force-reinstall doesn't
    wipe it (pip only replaces the package contents).
    """
    return Path(__file__).resolve().parent.parent / _RESULT_FILE_NAME


def _now_iso() -> str:
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _write_result(status: str, detail: str = "") -> None:
    """Persist the latest update attempt's outcome for the next heartbeat."""
    payload = {
        "status": status,  # "ok" | "pip_failed" | "pip_timeout" | "no_pip" | "spawn_failed"
        "detail": detail[:1024],  # cap so a multi-MB pip stderr doesn't bloat heartbeats
        "ts": _now_iso(),
    }
    try:
        _result_path().write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        log.warning("self-update: could not write result marker: %s", exc)


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


async def run_self_update(pip_source: str, timeout_seconds: float = 300.0) -> tuple[bool, str]:
    """Run pip install --force-reinstall --no-deps <pip_source>.

    Returns (ok, detail). On failure, ``detail`` carries the truncated pip
    stderr (or a synthetic reason like "pip not found") so the next heartbeat
    can surface it.
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
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
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
    """End-to-end: install + restart. Logs and returns on any failure so the
    agent keeps running on the OLD code rather than going dark.

    The outcome -- success or specific failure -- is written to the result
    marker file so the NEXT heartbeat (after the service-manager restart, or
    on the current process if pip failed) can surface it on the dashboard.
    """
    if not pip_source:
        log.warning("self-update: no pip_source in command payload -- skipping")
        _write_result("no_pip_source", "central did not provide a pip_source URL")
        return
    success, detail = await run_self_update(pip_source)
    if not success:
        _write_result("pip_failed", detail)
        log.error("self-update: aborting -- agent will keep running on old code")
        return
    _write_result("ok", "pip install succeeded; restarting via service manager")
    restart_for_service_manager(0)
