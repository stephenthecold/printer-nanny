"""The backend for everything that is neither Windows nor macOS.

Exists so the client fails with a sentence instead of an ImportError or an
AttributeError. A technician running it on a Linux box to check connectivity
should get "this platform is not supported for provisioning", not a traceback
that looks like a bug in the client.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, Optional, Sequence

NAME = sys.platform

#: Nothing is provisioned here at all, drivers least of all.
SUPPORTS_VENDOR_DRIVERS = False


def queue_name(name: str) -> str:
    """Identity. No queue is created, so no name is derived."""
    return name


def default_state_dir() -> str:
    base = os.environ.get("PN_STATE_DIR_BASE", "/var/lib")
    return os.path.join(base, "PrinterNanny")


def console_user() -> Optional[str]:
    """No console concept to report. None is the login-screen case the loop
    already handles, so this degrades rather than failing."""
    return None


def provision_queues(
    runner, desired: Sequence[dict], managed_prefix: str = ""
) -> Dict[str, str]:
    """Every queue reported as unsupported, with the platform named.

    Not an exception: the poll should still check in and report, so central shows
    the machine as alive with a stated reason rather than silently absent.
    """
    return {
        spec["name"]: f"error: provisioning is not supported on {NAME}"
        for spec in desired
    }


def set_default_printer(name: str, *, manage_windows_default: bool = True) -> str:
    from printer_nanny_agent.workstation_service import DefaultPrinterError

    raise DefaultPrinterError(f"setting a default printer is not supported on {NAME}")
