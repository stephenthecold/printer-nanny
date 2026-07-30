"""Windows backend: the shipped, hardware-proven implementation.

Deliberately a thin re-export rather than a move. The Windows code has been
proven against a real Brother NIC (`scripts/windows_provision_check.py`,
2026-07-28), and relocating several hundred lines of ctypes and PowerShell to
introduce a seam would put that evidence at risk for no behavioural gain. The
seam is what is new; the implementation is untouched.

``provision_queues`` looks up ``ws.reconcile`` at CALL time rather than binding
it at import, so a test that monkeypatches ``ws.reconcile`` still takes effect
through the backend. That is not incidental -- it is what let this seam land
without rewriting the existing suite.
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence

from printer_nanny_agent import workstation as ws

NAME = "windows"

#: pnputil stages a vendor driver into the DriverStore. The only backend that can.
SUPPORTS_VENDOR_DRIVERS = True

#: The command seam this backend drives everything through.
make_runner = ws.PowerShellRunner


def queue_name(name: str) -> str:
    """Identity: ``build_specs`` already sanitises for Windows, and the spooler
    takes those names verbatim -- so the report is already consistent here. The
    hook exists because CUPS is not so accommodating."""
    return name


def default_state_dir() -> str:
    from printer_nanny_agent import workstation_service as svc

    return svc._windows_state_dir()


def console_user() -> Optional[str]:
    from printer_nanny_agent import workstation_service as svc

    return svc._windows_console_user()


def provision_queues(
    runner, desired: Sequence[dict], managed_prefix: str = ""
) -> Dict[str, str]:
    return ws.reconcile(runner, desired, managed_prefix=managed_prefix)


def set_default_printer(name: str, *, manage_windows_default: bool = True) -> str:
    from printer_nanny_agent import workstation_service as svc

    return svc._windows_set_default_printer(
        name, manage_windows_default=manage_windows_default
    )
