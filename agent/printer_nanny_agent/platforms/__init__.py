"""Per-OS backends for the three things the workstation client cannot share.

Everything else -- enrollment, the machine GUID, the poll loop, spec mapping,
driver fetch/verify, ProvisionReport and every skip reason -- is identical on
every platform and stays in ``workstation_service``. Only three things are
genuinely OS-specific:

* where per-machine state lives,
* who is signed in at the console,
* how a queue is created and how a user's default printer is set.

A backend rather than a second client, deliberately. A parallel macOS module
would duplicate enrollment, adoption handling, the verify-then-report rule for
defaults and every skip reason -- and per this repo's own hard-won lesson, a
second implementation is a second place to get it wrong, and the one that drifts
is the one nobody remembered existed.

The unsupported case is a backend too, not an exception at import: the client
must be runnable on a developer's Linux box far enough to fail with a sentence
rather than a traceback.
"""

from __future__ import annotations

import sys


def current():
    """The backend for the running OS.

    Resolved at call time, not import, so a test can select one explicitly and
    so importing the package never depends on where it is imported.
    """
    if sys.platform.startswith("win"):
        from printer_nanny_agent.platforms import windows

        return windows
    if sys.platform == "darwin":
        from printer_nanny_agent.platforms import macos

        return macos
    from printer_nanny_agent.platforms import unsupported

    return unsupported
