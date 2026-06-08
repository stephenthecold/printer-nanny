"""Self-update: pip install the agent package, then exit so the service
manager (systemd / NSSM) restarts the process against the freshly-installed
code.

Triggered by the `update_agent` command type pulled on heartbeat. The
heavy lifting (pip subprocess) runs detached so a failed install doesn't
kill the running agent -- we only exit when pip reports success.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("printer_nanny_agent.updater")


def _pip_path() -> Path:
    """Resolve pip.exe / pip next to the current Python.

    The agent is always launched from its own venv (the installer creates
    .venv/bin/printer-nanny-agent / Scripts\\printer-nanny-agent.exe), so
    sys.executable is the venv python and pip lives next to it.
    """
    py_dir = Path(sys.executable).resolve().parent
    candidate = py_dir / ("pip.exe" if os.name == "nt" else "pip")
    return candidate


async def run_self_update(pip_source: str, timeout_seconds: float = 300.0) -> bool:
    """Run pip install --force-reinstall --no-deps <pip_source>.

    Returns True only when pip exits 0. Output is captured so a hang doesn't
    fill the agent log; first 2KB of stderr goes into the agent log on failure
    so the operator can diagnose without ssh-ing into the host.
    """
    pip = _pip_path()
    if not pip.exists():
        log.error("self-update: pip not found at %s", pip)
        return False
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
        log.error("self-update: failed to spawn pip: %s", exc)
        return False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        proc.kill()
        log.error("self-update: pip install timed out after %ss", timeout_seconds)
        return False
    if proc.returncode != 0:
        snippet = (stderr or b"").decode("utf-8", errors="replace")[:2048]
        log.error("self-update: pip install failed (exit %d): %s",
                  proc.returncode, snippet)
        return False
    log.info("self-update: pip install succeeded")
    return True


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
    agent keeps running on the OLD code rather than going dark."""
    if not pip_source:
        log.warning("self-update: no pip_source in command payload -- skipping")
        return
    success = await run_self_update(pip_source)
    if not success:
        log.error("self-update: aborting -- agent will keep running on old code")
        return
    restart_for_service_manager(0)
