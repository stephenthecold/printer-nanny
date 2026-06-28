"""printer-nanny-agent — site collector for Printer Nanny.

Discovers and polls printers over SNMP (RFC 3805 Printer MIB), then pushes
readings to the central server and pulls queued commands. See the package README
and ``central/snmp.md`` for the OID reference.
"""

from __future__ import annotations

import datetime as _dt
import os as _os

# Baseline package version. Bump in pyproject.toml + here together when we
# do a real release; the install-time suffix below is what actually changes
# on each self-update so operators can see updates landed.
__base_version__ = "0.2.0"


def _install_marker() -> str:
    """A short marker that changes on every pip install of the package.

    Returns the mtime of THIS file (which pip writes during install) formatted
    as ``YYYYMMDD-HHMMSS``. After ``pip install --force-reinstall`` the file's
    mtime is the install time, so the version reported on heartbeat flips and
    the operator can verify the update actually landed from the dashboard
    instead of guessing.
    """
    try:
        stamp = _os.path.getmtime(__file__)
    except OSError:
        return ""
    return _dt.datetime.utcfromtimestamp(stamp).strftime("%Y%m%d-%H%M%S")


__version__ = f"{__base_version__}+{_install_marker()}" if _install_marker() else __base_version__
