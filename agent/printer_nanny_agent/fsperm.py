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
import subprocess

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


def _current_user_sid() -> "str | None":
    """This process's own SID, or None if it cannot be read."""
    try:
        proc = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    parts = [p.strip().strip('"') for p in (proc.stdout or "").strip().split(",")]
    sid = parts[-1] if parts else ""
    return sid if sid.startswith("S-1-") else None


def _grantees() -> "list[str]":
    """SYSTEM + Administrators, plus this process's own account.

    The service runs as LocalSystem, which is already on the list, so in
    production this adds nothing and the directory stays closed to every other
    user -- which was the whole point.

    It matters for the other caller: a technician running
    ``printer-nanny-workstation --once`` to diagnose a machine. Without their own
    SID, the first call here would revoke their access to a directory they had
    just created, and every subsequent write would fail with a PermissionError
    naming a path they own. That is a worse outcome than the inherited ACL was,
    and it is not hypothetical -- it broke the driver-package tests the moment
    this helper started running against their cache.

    Adding the current account is much narrower than the inherited
    ``BUILTIN\\Users`` this replaces: one account, not every logged-in user.
    """
    grantees = list(WINDOWS_ACL_SIDS)
    mine = _current_user_sid()
    if mine and f"*{mine}" not in grantees:
        grantees.append(f"*{mine}")
    return grantees


def reset_cache_for_tests() -> None:
    """Forget which directories have been secured. Test-only."""
    _secured.clear()


def _is_cwd(real: str) -> bool:
    """Is ``real`` the process's own working directory?

    Compared through ``normcase`` because the point is to recognise the working
    directory *however it was spelled*: on Windows ``C:\\Foo`` and ``c:\\foo`` are
    one directory, and a check that missed on letter case would hand the
    damaging path back at exactly the moment it was meant to stop it.

    ``os.getcwd()`` **raises** when the working directory has been deleted out
    from under a long-running process -- POSIX only; Windows refuses to delete a
    directory any process is sitting in. That must not escape: ``secure_dir``
    promises never to take a caller down, and the workstation client calls it
    while persisting a single-use enrollment credential, where an exception
    bricks the machine rather than merely leaving a directory open. A working
    directory we cannot identify is therefore reported as "not it", which is
    exactly the behaviour that predates this check.
    """
    try:
        cwd = os.getcwd()
    except OSError as exc:  # pragma: no cover - needs a deleted cwd
        log.debug("cannot read the working directory (%s); securing %s", exc, real)
        return False
    return os.path.normcase(real) == os.path.normcase(cwd)


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

    # Both callers derive this as ``os.path.dirname(path) or "."``, so a bare
    # filename resolves to the PROCESS'S CURRENT DIRECTORY -- which in a test run
    # or a CI checkout is the source tree, and on a hand-run agent is wherever
    # the technician happened to be standing. Locking that to 0700, or stripping
    # inheritance off it on Windows, is never what a caller means by "secure my
    # state directory", and it is the kind of side effect that is noticed a long
    # way from here.
    #
    # Deliberately not cached in ``_secured``: the check is free, and a process
    # that later chdirs away must still be able to secure this directory for real.
    if _is_cwd(real):
        log.debug("not restricting %s: it is the current directory", real)
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
    for sid in _grantees():
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
