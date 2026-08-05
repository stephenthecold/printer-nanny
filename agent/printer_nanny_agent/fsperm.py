"""Restrict a directory to its owner, on whichever OS this is.

**``os.chmod`` is not a permission on Windows.** It toggles the read-only
attribute and writes no DACL at all, so a mode set on a file there is decoration
-- the file simply inherits whatever its parent grants. Under ``%PROGRAMDATA%``
that means ``BUILTIN\\Users:(I)(RX)``, and `C:\\ProgramData` additionally grants
Users *create-subdirectory* with CREATOR OWNER full control, so an unprivileged
user who creates our subdirectory first owns everything written into it
afterwards.

This module exists because that mistake was made twice in this codebase, in two
different components, each with a comment asserting a protection that did not
exist on the target platform:

* the workstation client's state directory, holding ``machine.json`` -- a live
  bearer credential any logged-in user could read;
* the site agent's definition cache, which holds no secret but, in its own
  words, "decides what every printer at this site is read with", so a writable
  one is a way to change that.

Two copies would have drifted, and the second was found only because a POSIX
mode assertion failed on a Windows test run. So there is one copy.
"""

from __future__ import annotations

import logging
import os
import stat

log = logging.getLogger(__name__)

#: SIDs, not names. ``BUILTIN\\Administrators`` is ``VORDEFINIERT\\Administratoren``
#: on a German Windows and localised again on every other one, so granting by
#: name silently fails off an English install -- the same "every string is
#: translated" lesson the CUPS backend already paid for. S-1-5-18 is LocalSystem
#: (the service account, which must read its own state) and S-1-5-32-544 is
#: Administrators.
WINDOWS_ACL_SIDS = ("*S-1-5-18", "*S-1-5-32-544")

#: Applied once per directory per process. Doing it per write would spawn a
#: subprocess on every poll; doing it at first use still covers the case that
#: actually matters, which is a directory somebody else created before us.
_secured: set = set()


def reset_cache_for_tests() -> None:
    """Forget which directories have been secured. Test-only."""
    _secured.clear()


def secure_dir(directory: str) -> None:
    """Restrict ``directory`` to SYSTEM + Administrators (Windows) or 0700 (POSIX).

    Enforced rather than only set-on-create: we re-assert once per process
    whether or not we made the directory, which is what closes the
    someone-created-it-first case.

    Best-effort by design. A failure must not stop an agent polling or a
    workstation provisioning its queues, so it warns and continues -- but it
    *does* warn, because a silent failure is how this stayed invisible twice.
    """
    real = os.path.abspath(directory)
    if real in _secured:
        return
    _secured.add(real)

    if os.name != "nt":
        try:
            os.chmod(real, stat.S_IRWXU)  # 0700; a real permission here
        except OSError as exc:
            log.warning("could not restrict %s: %s", real, exc)
        return

    import subprocess

    # argv list, never a shell: the path comes from %PROGRAMDATA%, a config file
    # or a --state-dir flag, and is not ours to trust with a command line.
    cmd = ["icacls", real, "/inheritance:r"]
    for sid in WINDOWS_ACL_SIDS:
        cmd += ["/grant:r", f"{sid}:(OI)(CI)F"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not restrict %s (icacls did not run): %s", real, exc)
        return
    if proc.returncode != 0:
        log.warning(
            "could not restrict %s: icacls exited %s: %s",
            real, proc.returncode, (proc.stderr or proc.stdout).strip()[:300],
        )
