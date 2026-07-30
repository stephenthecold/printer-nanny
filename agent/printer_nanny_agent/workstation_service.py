"""The workstation client: enroll once, then poll and converge queues.

This is the piece that makes printer assignment act on something. It runs as a
Windows service under LocalSystem, and the three things it does map onto the
three endpoints central exposes: enroll (once), ask what this machine and the
signed-in person should have, make the spooler match.

WHY LocalSystem
---------------
Since KB5005652 (Aug 2021) ``RestrictDriverInstallationToAdministrators``
defaults to 1, so Point and Print demands local admin and an ordinary user hits
a UAC prompt they cannot satisfy. The usual escape -- setting that policy to 0 --
reopens PrintNightmare. So the service installs on the user's behalf from a
context that already has the privilege, and Point and Print never enters the
picture.

THE TWO THINGS THAT LOOK LIKE SUCCESS WHEN THEY ARE NOT
-------------------------------------------------------
Both of these do real work, and both can fail in ways central would otherwise
record as done. That is why each one verifies, and why each failure becomes a
stated reason rather than an absence.

**It sets the user's default printer by impersonating them, and verifies it.**
The default is per-user state, so from LocalSystem the only correct way to touch
it is to become that user for the duration: ``WTSQueryUserToken`` on the console
session, ``ImpersonateLoggedOnUser``, then the ordinary Win32
``SetDefaultPrinter`` -- which acts on the calling thread's user. Writing the
registry directly was rejected: the ``Device`` value's ``Name,Driver,Port``
format has to be assembled by hand, and an IPP queue's port is one Windows
chose, so a guess there produces a default that looks set and does not work.

Two things make this honest rather than hopeful. It **reads the default back**
with ``GetDefaultPrinter`` and only reports success if the read agrees --
``SetDefaultPrinter`` returning non-zero is not evidence the user has that
default. And it addresses **"Let Windows manage my default printer"**, which
ships ON and silently re-points the default at whatever was printed to last:
without turning that off, an assigned default appears to apply and then quietly
does not. Turning it off overrides a user-facing preference, so it is a setting
(``workstation.manage_default_printer``) and what happened is reported per
machine rather than done invisibly.

Every failure path -- no console session, no token, a session that logged off
mid-write -- becomes a stated reason on ``ProvisionReport``, never a claim.

**It does not stage vendor drivers UNLESS central supplies one.** A
``driver_required`` printer whose client has a matching uploaded package gets
that package downloaded, checksum-verified, unpacked and staged with
``pnputil``. Without a package the queue is still skipped with a stated reason,
because binding a wrong driver prints garbage -- worse than no queue at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from printer_nanny_agent import workstation as ws

log = logging.getLogger(__name__)

#: Every queue this service creates carries it, and ``reconcile`` will only ever
#: remove queues that do. Without a prefix it would be free to delete a printer
#: the user added themselves, which is how a print tool gets uninstalled.
MANAGED_PREFIX = "PN "

#: Windows rejects these in a printer name outright; the rest of a name is free
#: text from an operator or a device and travels by environment variable, so
#: this is about validity, not injection.
_NAME_BANNED = set('\\/,!"')


class ServiceError(RuntimeError):
    """Enrollment or configuration failed in a way retrying will not fix."""


class DefaultPrinterError(RuntimeError):
    """The default could not be set or could not be verified. Never fatal --
    the queues are already provisioned; only the default is in question.

    Lives here rather than in a backend because every backend raises it and the
    orchestrator catches it; putting it in one platform module would make the
    others import that platform to raise their own errors.
    """


def _platform():
    """The backend for this OS, resolved per call so tests can substitute one."""
    from printer_nanny_agent import platforms

    return platforms.current()


def _platform_name() -> str:
    """What this client tells central it is, for the driver decision.

    Taken from the backend rather than ``sys.platform`` so a test that
    substitutes a backend substitutes this too -- otherwise a macOS-backend test
    would ask central for Windows drivers and the mismatch would only show up on
    a real Mac.
    """
    return str(getattr(_platform(), "NAME", "") or "")


def _platform_runner():
    """The command seam this OS's backend needs, or None where it needs none.

    Windows supplies ``PowerShellRunner``; CUPS is driven by argv with no shell,
    so macOS needs nothing. Asked of the backend rather than branched on here,
    because a Mac constructing a PowerShellRunner -- which silently falls back to
    the literal string "powershell" when it finds none -- is an object that
    exists only to be ignored.
    """
    factory = getattr(_platform(), "make_runner", None)
    return factory() if factory else None


def default_state_dir() -> str:
    """Where per-machine identity and credentials live, per platform.

    ProgramData on Windows, /Library/Application Support on macOS -- in both
    cases a machine-wide location an upgrade does not replace, because a client
    that forgets its credential has to re-enroll to recover.
    """
    return _platform().default_state_dir()


def console_user() -> Optional[str]:
    """Who is signed in, as central identifies them, or None at the login screen."""
    return _platform().console_user()


def set_default_printer(name: str, *, manage_windows_default: bool = True) -> str:
    """Make ``name`` the console user's default, verified. Raises on every
    failure path with a stated reason."""
    return _platform().set_default_printer(
        name, manage_windows_default=manage_windows_default
    )


# --------------------------------------------------------------------------- #
# Machine identity
# --------------------------------------------------------------------------- #


def _windows_state_dir() -> str:
    """Where the machine's identity and credentials live.

    ProgramData, not the install directory: an upgrade replaces program files
    and would take the credential with it, and a machine that forgets its
    credential has to re-enroll to recover.
    """
    base = os.environ.get("PROGRAMDATA") or os.environ.get(
        "PN_STATE_DIR_BASE", "/var/lib"
    )
    return os.path.join(base, "PrinterNanny")


def _write_json_atomic(path: str, payload: dict) -> None:
    """Write owner-only, atomically.

    The permissions are set on the temp file *before* the rename, so the file
    never exists under its final name in a world-readable state -- this holds a
    live API key, and a window is still a window.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".pn-state.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2, sort_keys=True)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state(state_dir: Optional[str] = None) -> dict:
    path = os.path.join(state_dir or default_state_dir(), "machine.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # A truncated or hand-edited state file must not be fatal: returning {}
        # re-enrolls, and re-enrolling the same machine_uid rotates rather than
        # duplicating. Refusing to start would need a site visit instead.
        log.warning("machine state at %s is unreadable; re-enrolling", path)
        return {}


def save_state(state: dict, state_dir: Optional[str] = None) -> None:
    _write_json_atomic(
        os.path.join(state_dir or default_state_dir(), "machine.json"), state
    )


def machine_uid(state_dir: Optional[str] = None) -> str:
    """This machine's identity, minted once and then stable.

    A GUID rather than the computer name or the machine SID. Names are reused
    across a fleet, are not unique across customers, and change on rename -- so
    a renamed PC would silently become a new machine while a recycled name would
    silently inherit another machine's printers. The SID survives a rename but
    not a re-image, and cloned images that skipped sysprep share one.

    This survives renames, IP changes and domain moves. It does NOT survive a
    re-image -- a wiped ProgramData means a fresh GUID and a machine that looks
    like a stranger.

    That case is handled on the server, not here, and deliberately so. Central
    can see the whole tenant, so it can tell "one retired record with this
    computer name" from "two live PCs that happen to share one"; a client
    knows only itself and would have to guess. So the client always sends its
    real GUID and its real name, and ``services.adopt_by_name`` decides whether
    this is a returning machine. The client never asserts an identity it cannot
    prove.
    """
    state = load_state(state_dir)
    uid = state.get("machine_uid")
    if uid:
        return str(uid)

    uid = uuid.uuid4().hex
    state["machine_uid"] = uid
    save_state(state, state_dir)
    log.info("minted machine uid %s", uid)
    return uid


# --------------------------------------------------------------------------- #
# Who is signed in
# --------------------------------------------------------------------------- #


def _windows_console_user() -> Optional[str]:
    """The UPN of whoever is at the console, or None at the login screen.

    Two Windows calls, because neither alone is enough. ``WTSQuerySessionInformation``
    on the active console session yields ``DOMAIN\\sam``, which is not what
    central stores -- it matches on UPN or email. ``TranslateName`` converts one
    to the other using the domain, which is why this works from LocalSystem: the
    machine account can make that query.

    Returns None rather than raising on every failure path -- no session, a
    workgroup machine with no UPN to translate to, a domain that cannot be
    reached. None is a real state (the login screen) that the caller already
    handles, so a failure degrades into it rather than stopping the service.
    """
    try:  # pragma: no cover - Windows-only, exercised by the Windows job
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    try:  # pragma: no cover - Windows-only
        wtsapi = ctypes.WinDLL("wtsapi32")
        kernel32 = ctypes.WinDLL("kernel32")
    except (OSError, AttributeError):
        return None

    try:  # pragma: no cover - Windows-only
        session_id = kernel32.WTSGetActiveConsoleSessionId()
        if session_id in (0xFFFFFFFF, None):
            return None  # No console session: nobody is signed in.

        def _query(info_class: int) -> str:
            buf = ctypes.c_wchar_p()
            size = wintypes.DWORD()
            ok = wtsapi.WTSQuerySessionInformationW(
                None, session_id, info_class, ctypes.byref(buf), ctypes.byref(size)
            )
            if not ok or not buf.value:
                return ""
            try:
                return str(buf.value)
            finally:
                wtsapi.WTSFreeMemory(buf)

        user = _query(5)  # WTSUserName
        domain = _query(7)  # WTSDomainName
        if not user:
            return None

        sam = f"{domain}\\{user}" if domain else user
        return _translate_to_upn(sam) or None
    except Exception as exc:  # pragma: no cover - Windows-only
        log.warning("could not determine console user: %s", exc)
        return None


def _translate_to_upn(sam: str) -> str:  # pragma: no cover - Windows-only
    """``DOMAIN\\sam`` -> ``sam@domain.tld`` via secur32 TranslateName."""
    import ctypes
    from ctypes import wintypes

    NameSamCompatible, NameUserPrincipal = 2, 8
    secur32 = ctypes.WinDLL("secur32")
    size = wintypes.ULONG(1024)
    buf = ctypes.create_unicode_buffer(size.value)
    ok = secur32.TranslateNameW(
        sam, NameSamCompatible, NameUserPrincipal, buf, ctypes.byref(size)
    )
    if not ok:
        # Workgroup machine, or the domain is unreachable. The caller degrades
        # to "nobody signed in", which still provisions the machine's own
        # printers -- the shared-terminal case -- rather than failing.
        log.info("no UPN for %s; treating as unidentified", sam)
        return ""
    return buf.value


# --------------------------------------------------------------------------- #
# The user's default printer
# --------------------------------------------------------------------------- #


#: HKCU value that decides whether Windows re-points the default at whatever was
#: printed to last. Absent or 0 means Windows manages it (the shipped default),
#: 1 means the user's choice stands.
_LEGACY_MODE_KEY = r"Software\Microsoft\Windows NT\CurrentVersion\Windows"
_LEGACY_MODE_VALUE = "LegacyDefaultPrinterMode"


def _windows_set_default_printer(
    name: str, *, manage_windows_default: bool = True
) -> str:
    """Make ``name`` the console user's default. Returns the verified name.

    Runs the whole thing **impersonating that user**, because the default is
    per-user state and LocalSystem's own HKCU is not theirs. ``SetDefaultPrinter``
    acts on the calling thread's user, so impersonation makes the ordinary API do
    exactly the right thing -- no hand-assembled registry values, and no guessing
    the port of a queue whose port Windows chose.

    Raises ``DefaultPrinterError`` with a stated reason on every failure path,
    including "the write succeeded and the read back disagreed", which is the one
    that matters: a non-zero return from SetDefaultPrinter is not evidence that
    the user has that default.
    """
    if not (name or "").strip():
        raise DefaultPrinterError("no default requested")

    try:  # pragma: no cover - Windows-only, exercised only on a real machine
        import ctypes
        from ctypes import wintypes
    except Exception:
        raise DefaultPrinterError("not running on Windows")

    try:  # pragma: no cover - Windows-only
        wtsapi = ctypes.WinDLL("wtsapi32")
        kernel32 = ctypes.WinDLL("kernel32")
        advapi32 = ctypes.WinDLL("advapi32")
        winspool = ctypes.WinDLL("winspool.drv")
    except (OSError, AttributeError):
        raise DefaultPrinterError("not running on Windows")

    session_id = kernel32.WTSGetActiveConsoleSessionId()  # pragma: no cover
    if session_id in (0xFFFFFFFF, None):  # pragma: no cover
        # Nobody is signed in. Not an error -- there is no per-user default to
        # set at a login screen, and saying so beats implying a failure.
        raise DefaultPrinterError("nobody is signed in")

    token = wintypes.HANDLE()  # pragma: no cover - Windows-only below here
    if not wtsapi.WTSQueryUserToken(session_id, ctypes.byref(token)):
        raise DefaultPrinterError(
            "could not obtain the console user's token "
            f"(error {ctypes.get_last_error()})"
        )

    try:
        if not advapi32.ImpersonateLoggedOnUser(token):
            raise DefaultPrinterError(
                f"could not impersonate the console user "
                f"(error {ctypes.get_last_error()})"
            )
        try:
            if manage_windows_default:
                _stop_windows_managing_default()

            if not winspool.SetDefaultPrinterW(ctypes.c_wchar_p(name)):
                raise DefaultPrinterError(
                    f"SetDefaultPrinter failed (error {ctypes.get_last_error()})"
                )

            got = _read_default_printer(winspool)
            if got != name:
                # The usual cause: Windows is still managing defaults, so it
                # re-pointed immediately. Say which, rather than "failed".
                raise DefaultPrinterError(
                    f"default did not stick (wanted {name!r}, got {got!r}); "
                    "Windows may still be managing the default printer"
                )
            return got
        finally:
            advapi32.RevertToSelf()
    finally:
        kernel32.CloseHandle(token)


def _stop_windows_managing_default() -> None:  # pragma: no cover - Windows-only
    """Turn off "Let Windows manage my default printer" for the impersonated user.

    **HKEY_CURRENT_USER is not the impersonated user's hive**, which is what this
    used to assume. ``HKEY_CURRENT_USER`` is resolved against the *process* token
    and cached per process, so in a session-0 service running as LocalSystem it
    keeps pointing at SYSTEM's own hive no matter who the thread is impersonating.
    The write then either lands on SYSTEM (wrong user, silently useless) or is
    refused outright.

    Measured on a real Windows 11 machine, which is the only thing that could show
    it: under NSSM as LocalSystem this raised ``[WinError 5] Access is denied``,
    and because it runs *before* ``SetDefaultPrinter`` it took the whole feature
    down with it -- with the shipped default (``manage_default_printer`` on) an
    assigned default never applied at all. Flipping that setting off made the same
    poll succeed, which is what isolated this call. The identical call succeeds
    when run outside the service, so nothing short of the service context reaches
    it.

    ``RegOpenCurrentUser`` is the documented API for precisely this: it resolves
    the hive of the **thread** token, so it follows the impersonation.
    """
    import ctypes
    import winreg
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    hkey = wintypes.HANDLE()
    rc = advapi32.RegOpenCurrentUser(
        wintypes.DWORD(winreg.KEY_WRITE), ctypes.byref(hkey)
    )
    if rc != 0:
        raise OSError(0, f"RegOpenCurrentUser failed (error {rc})")
    try:
        with winreg.CreateKeyEx(
            hkey.value, _LEGACY_MODE_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, _LEGACY_MODE_VALUE, 0, winreg.REG_DWORD, 1)
    finally:
        advapi32.RegCloseKey(hkey)


def _read_default_printer(winspool) -> str:  # pragma: no cover - Windows-only
    """GetDefaultPrinter for the impersonated user, or '' if there is none."""
    import ctypes

    size = ctypes.c_ulong(0)
    winspool.GetDefaultPrinterW(None, ctypes.byref(size))
    if not size.value:
        return ""
    buf = ctypes.create_unicode_buffer(size.value)
    if not winspool.GetDefaultPrinterW(buf, ctypes.byref(size)):
        return ""
    return buf.value


# --------------------------------------------------------------------------- #
# Vendor driver packages
# --------------------------------------------------------------------------- #


class DriverError(RuntimeError):
    """A package could not be fetched, verified or unpacked. Never fatal to the
    poll -- the queue is skipped with the reason and the rest still provision."""


def _safe_extract(archive: str, dest: str) -> None:
    """Unpack a zip, refusing any entry that escapes ``dest``.

    An uploaded archive is attacker-shaped input, and the classic attack is an
    entry named ``../../Windows/System32/...`` -- extracted naively, that writes
    outside the temp directory as LocalSystem. Python's ``extractall`` does not
    protect against this, so every member's resolved destination is checked to
    be inside the target before anything is written.

    Absolute paths, drive letters and symlink members are refused for the same
    reason: each is a different spelling of "write somewhere I was not given".
    """
    root = os.path.realpath(dest)
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = member.filename
            if name.endswith("/"):
                continue
            if os.path.isabs(name) or ntpath_is_abs(name):
                raise DriverError(f"archive entry is an absolute path: {name!r}")
            target = os.path.realpath(os.path.join(root, name))
            if target != root and not target.startswith(root + os.sep):
                raise DriverError(f"archive entry escapes the target dir: {name!r}")
            # Mode bits carry the symlink flag on zips written by unix tools.
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise DriverError(f"archive contains a symlink: {name!r}")
        zf.extractall(root)


def ntpath_is_abs(name: str) -> bool:
    """Windows-absolute even when we are parsing on POSIX.

    ``os.path.isabs`` on Linux says ``C:\\Windows`` is relative, which is
    exactly wrong for an archive that will be unpacked on Windows -- the check
    has to hold wherever the test suite runs, not only on the target.
    """
    n = (name or "").replace("/", "\\")
    return n.startswith("\\") or (len(n) > 1 and n[1] == ":")


def fetch_driver(
    client: "WorkstationClient", driver: dict, cache_dir: str
) -> str:
    """Download and verify a package; return the directory it unpacked into.

    Cached by **sha256**, not by package id: the digest is what was verified, so
    a re-uploaded package under the same id gets a different cache entry rather
    than silently reusing the old bytes.

    The digest is checked BEFORE the archive is opened. Verifying after
    unpacking would mean a hostile archive had already been written to disk.
    """
    sha = str(driver.get("sha256") or "")
    if len(sha) != 64:
        raise DriverError("driver package has no usable checksum")

    unpacked = os.path.join(cache_dir, sha)
    marker = os.path.join(unpacked, ".pn-ok")
    if os.path.exists(marker):
        return unpacked

    os.makedirs(cache_dir, exist_ok=True)
    archive = os.path.join(cache_dir, sha + ".pkg")
    client.download_driver(int(driver["package_id"]), archive)

    got = _sha256_file(archive)
    if got != sha:
        os.unlink(archive)
        raise DriverError(
            "driver package checksum mismatch (expected {}, got {})".format(
                sha[:12], got[:12]
            )
        )

    shutil.rmtree(unpacked, ignore_errors=True)
    os.makedirs(unpacked, exist_ok=True)
    try:
        _safe_extract(archive, unpacked)
    except Exception:
        shutil.rmtree(unpacked, ignore_errors=True)
        raise
    finally:
        try:
            os.unlink(archive)
        except OSError:
            pass

    with open(marker, "w", encoding="utf-8") as fp:
        fp.write(sha)
    return unpacked


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inf_path_in(unpacked: str, inf_relpath: str) -> str:
    """Resolve the INF inside an unpacked package, refusing to leave it.

    ``inf_relpath`` is operator-typed, so it gets the same treatment as an
    archive member: a path that resolves outside the package would hand pnputil
    an arbitrary file on the workstation.
    """
    root = os.path.realpath(unpacked)
    target = os.path.realpath(os.path.join(root, inf_relpath.replace("\\", os.sep)))
    if target != root and not target.startswith(root + os.sep):
        raise DriverError(f"inf path escapes the package: {inf_relpath!r}")
    if not os.path.isfile(target):
        raise DriverError(f"inf not found in package: {inf_relpath!r}")
    return target


# --------------------------------------------------------------------------- #
# Turning assignments into queue specs
# --------------------------------------------------------------------------- #


def queue_name_for(printer: dict, prefix: str = MANAGED_PREFIX) -> str:
    """A stable, valid Windows queue name for an assigned printer.

    Derived from the printer id, not only the name: two printers can share a
    display name, and a queue name collision would make one silently replace the
    other on every poll. The name is decoration for the user; the id is what
    makes it converge.
    """
    raw = (printer.get("name") or "").strip() or f"printer-{printer['printer_id']}"
    cleaned = "".join(ch for ch in raw if ch not in _NAME_BANNED and ch.isprintable())
    cleaned = " ".join(cleaned.split())[:180].strip() or f"printer-{printer['printer_id']}"
    return f"{prefix}{cleaned} ({printer['printer_id']})"


@dataclass
class ProvisionReport:
    """What actually happened, per queue.

    ``skipped`` carries a reason rather than being a silent absence, because a
    printer that was assigned and did not appear is precisely the thing an
    operator will be asked about.
    """

    outcomes: Dict[str, str] = field(default_factory=dict)
    skipped: Dict[str, str] = field(default_factory=dict)
    #: The queue central asked to be default.
    desired_default: Optional[str] = None
    #: The queue that is ACTUALLY the user's default, read back after setting.
    #: Populated only when a verifying read agreed -- a write that returned
    #: success is not evidence, which is the whole reason this is a separate
    #: field from desired_default.
    default_applied: Optional[str] = None
    #: Why the default was not applied, when it was not. Stated for the same
    #: reason skips are: "central shows a default the user does not have" is the
    #: failure this module exists to avoid.
    default_reason: Optional[str] = None
    #: Why the cycle itself did not complete -- central unreachable, enrollment
    #: not yet possible. Distinct from a per-queue error: none of the queues
    #: failed, we never got as far as asking about them. Set so a ``--once`` run
    #: cannot report success for a cycle that never ran, which is the same rule
    #: as a channel that must set ``sent=False`` when nothing left the building.
    cycle_error: Optional[str] = None

    @property
    def ok(self) -> bool:
        if self.cycle_error:
            return False
        return not any(v.startswith("error") for v in self.outcomes.values())


def build_specs(
    assignments: dict, prefix: str = MANAGED_PREFIX
) -> Tuple[List[dict], Dict[str, str], Optional[str]]:
    """Map an ``/assignments`` payload onto ``reconcile`` specs.

    Returns ``(specs, skipped, desired_default_queue_name)``.

    Only ``driverless`` printers are provisioned. Every other tier is skipped
    with its reason:

    * ``driver_required`` -- central does not yet carry a driver package per
      printer, and binding the wrong driver prints garbage. Worse than nothing.
    * ``ipp_disabled`` -- port 631 is off, which is a checkbox in the device's
      web UI. Reporting it as a driver problem sends a technician to the wrong
      place, which is why the tier exists separately at all.
    * ``unreachable`` / ``error`` -- nothing to bind to.
    * missing tier -- "no new information", never "unknown": probing is
      throttled to roughly daily while polls run every few minutes.
    """
    specs: List[dict] = []
    skipped: Dict[str, str] = {}
    desired_default: Optional[str] = None

    for printer in assignments.get("printers") or []:
        name = queue_name_for(printer, prefix)
        tier = printer.get("driver_tier")
        uri = printer.get("ipp_endpoint")

        if printer.get("is_default"):
            desired_default = name

        if tier == "driverless":
            if not uri:
                skipped[name] = "driverless but central sent no IPP endpoint"
                continue
            specs.append({"name": name, "tier": "driverless", "uri": uri})
        elif tier == "driver_required":
            driver = printer.get("driver")
            if not driver:
                skipped[name] = "needs a vendor driver; no driver package configured"
            elif not printer.get("ip"):
                skipped[name] = "needs a vendor driver but central sent no address"
            else:
                specs.append({
                    "name": name,
                    "tier": "vendor",
                    "host": printer["ip"],
                    "driver": driver["driver_name"],
                    "package": driver,
                    # Carried for backends that bind a driver rather than
                    # staging one into a store. Windows ignores all three; the
                    # macOS backend needs the URI because a CUPS queue is
                    # addressed by device-uri, not by a port it built.
                    "uri": uri or "",
                    "driver_kind": driver.get("kind") or "",
                    "driver_ref": driver.get("ref") or "",
                    "allow_pkg_install": bool(
                        assignments.get("allow_macos_pkg_install", False)
                    ),
                })
        elif tier == "ipp_disabled":
            skipped[name] = "IPP is disabled on the device (port 631 refused)"
        elif tier in ("unreachable", "error"):
            skipped[name] = f"device not usable: {tier}"
        else:
            skipped[name] = "no driver tier known yet; waiting for a probe"

    return specs, skipped, desired_default


def provision(
    runner: ws.PowerShellRunner,
    assignments: dict,
    prefix: str = MANAGED_PREFIX,
    client: "Optional[WorkstationClient]" = None,
    cache_dir: Optional[str] = None,
    default_setter=None,
) -> ProvisionReport:
    """Converge the spooler onto what central says this machine should have.

    Driver packages are resolved here rather than inside ``reconcile`` so that a
    package that will not download, verify or unpack becomes a **skip with a
    reason** rather than a queue failure -- the operator needs to know the
    driver is the problem, not the printer.
    """
    specs, skipped, desired_default = build_specs(assignments, prefix)

    backend = _platform()
    ready = []
    for spec in specs:
        pkg = spec.pop("package", None)
        if pkg is None:
            ready.append(spec)
            continue
        if (pkg.get("kind") or "") == "system":
            # No bytes by design: the vendor package was installed out of band by
            # MDM and central recorded only the PPD path. Downloading nothing is
            # the point, so this must not fall through to fetch_driver.
            ready.append(spec)
            continue
        if not getattr(backend, "SUPPORTS_VENDOR_DRIVERS", False):
            # Downloading and unpacking a Windows driver archive on a platform
            # that cannot stage it accomplishes nothing and unpacks untrusted
            # bytes for no reason. Stated as a skip, which is what it is -- an
            # unimplemented feature, not a failure of this printer.
            skipped[spec["name"]] = (
                f"needs a vendor driver; staging them is not supported on "
                f"{getattr(backend, 'NAME', 'this platform')} yet"
            )
            continue
        if client is None:
            skipped[spec["name"]] = "needs a vendor driver but no way to fetch it"
            continue
        try:
            unpacked = fetch_driver(
                client, pkg, cache_dir or os.path.join(default_state_dir(), "drivers")
            )
            spec["unpacked"] = unpacked
            if pkg.get("inf_relpath"):
                # Windows resolves its INF here, where the zip-slip guard lives.
                # A macOS payload is resolved by the backend instead, because
                # what counts as a valid path differs by kind (inside the archive
                # for a .ppd, an allowlisted system directory for a PPD an MDM
                # installed) and only the backend knows which.
                spec["inf"] = inf_path_in(unpacked, pkg["inf_relpath"])
            ready.append(spec)
        except Exception as exc:
            # Stated, never silent: an operator seeing "skipped" needs to know
            # whether to re-upload the package or fix the printer.
            skipped[spec["name"]] = f"driver package unusable: {exc}"

    outcomes = backend.provision_queues(runner, ready, managed_prefix=prefix)

    # Report every queue under the name it actually has ON THE MACHINE.
    #
    # ``build_specs`` names queues for Windows, which accepts them verbatim. CUPS
    # does not -- it rejects spaces outright -- so the macOS backend derives its
    # own name and keys its outcomes by that. Without normalising here, one report
    # carried two naming conventions and ``outcomes.get(desired_default)`` below
    # missed **every time**: on a Mac the assigned default could never be applied,
    # and the reason given was "its queue was not provisioned (skipped)" even when
    # the queue had been created perfectly. Found by an end-to-end smoke against a
    # real scheduler; nothing above the seam could see it, because the fake
    # backend the unit tests use echoes the names it was handed.
    name_of = getattr(backend, "queue_name", None) or (lambda value: value)
    skipped = {name_of(key): why for key, why in skipped.items()}
    if desired_default is not None:
        desired_default = name_of(desired_default)

    report = ProvisionReport(
        outcomes=outcomes, skipped=skipped, desired_default=desired_default
    )

    if desired_default is None:
        return report

    # Only ever point the default at a queue that actually exists. Setting it to
    # one that was skipped or errored would be the failure this module is built
    # around: central reporting a default the user does not have.
    outcome = outcomes.get(desired_default, "")
    if not outcome or outcome.startswith("error"):
        report.default_reason = (
            f"its queue was not provisioned ({outcome or 'skipped'})"
        )
        return report

    setter = default_setter or set_default_printer
    try:
        report.default_applied = setter(
            desired_default,
            manage_windows_default=bool(assignments.get("manage_default_printer", True)),
        )
    except Exception as exc:
        # Including "nobody is signed in", which is normal at a login screen --
        # stated rather than swallowed so an operator can tell it apart from a
        # real failure.
        report.default_reason = str(exc)
    return report


# --------------------------------------------------------------------------- #
# Talking to central
# --------------------------------------------------------------------------- #


class WorkstationClient:
    """Sync HTTP against the three workstation endpoints.

    Sync on purpose: this client's whole job is one request every poll and a
    subprocess call per queue. An event loop would buy nothing and would have to
    be explained to whoever debugs it inside a Windows service.
    """

    def __init__(
        self,
        base_url: str,
        *,
        machine_id: Optional[int] = None,
        api_key: Optional[str] = None,
        verify_tls: bool = True,
        timeout: float = 30.0,
    ):
        import httpx

        self._base = base_url.rstrip("/")
        self._machine_id = machine_id
        self._client = httpx.Client(verify=verify_tls, timeout=timeout)
        if api_key:
            self._client.headers["Authorization"] = f"Bearer {api_key}"

    def enroll(self, enroll_key: str, uid: str, name: str) -> dict:
        resp = self._client.post(
            f"{self._base}/api/v1/workstations/enroll",
            json={
                "enroll_key": enroll_key,
                "machine_uid": uid,
                "name": name,
                "platform": _platform_name(),
            },
        )
        if resp.status_code == 401:
            # Central deliberately does not say whether the key is unknown or
            # revoked, so neither can this.
            raise ServiceError("enrollment refused: the key is not valid")
        resp.raise_for_status()
        return resp.json()

    def assignments(self, user: Optional[str] = None) -> dict:
        # The platform travels on every request rather than being read from the
        # machine row, so central's driver choice is based on what this client
        # actually is now. A stored value can be stale -- a PC re-imaged as a Mac
        # keeps its row through adoption -- and a stale platform means a Mac is
        # handed a Windows driver archive.
        params = {"platform": _platform_name()}
        if user:
            params["user"] = user
        resp = self._client.get(
            f"{self._base}/api/v1/workstations/{self._machine_id}/assignments",
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    def download_driver(self, package_id: int, dest: str) -> None:
        """Stream a package to disk.

        Streamed rather than held in memory: these are up to 512 MB, and a
        workstation service that spikes half a gigabyte of RSS to install a
        printer driver is one an admin kills.
        """
        url = (
            f"{self._base}/api/v1/workstations/{self._machine_id}"
            f"/drivers/{int(package_id)}"
        )
        with self._client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fp:
                for chunk in resp.iter_bytes(1024 * 1024):
                    fp.write(chunk)

    def checkin(self, name: str) -> dict:
        resp = self._client.post(
            f"{self._base}/api/v1/workstations/{self._machine_id}/checkin",
            json={"name": name, "platform": _platform_name()},
        )
        resp.raise_for_status()
        return resp.json()

    def adopt(self, machine_id: int, api_key: str) -> None:
        self._machine_id = machine_id
        self._client.headers["Authorization"] = f"Bearer {api_key}"

    def close(self) -> None:
        self._client.close()


def ensure_enrolled(
    client: WorkstationClient,
    *,
    enroll_key: str,
    computer_name: str,
    state_dir: Optional[str] = None,
) -> dict:
    """Return live credentials, enrolling first if we have none.

    The credentials are persisted **before** the client adopts them, matching
    the site agent's rule and for a sharper reason here: if the process died
    between redeeming and writing, the machine would come back, mint a *new*
    GUID only if its state file were also lost -- otherwise it re-enrolls the
    same uid and central rotates. Writing first means the common case (crash
    right after enrollment) costs nothing at all.
    """
    state = load_state(state_dir)
    if state.get("machine_id") and state.get("api_key"):
        client.adopt(int(state["machine_id"]), str(state["api_key"]))
        return state

    uid = machine_uid(state_dir)
    result = client.enroll(enroll_key, uid, computer_name)

    state.update(
        {
            "machine_uid": uid,
            "machine_id": int(result["machine_id"]),
            "api_key": str(result["api_key"]),
            "client_id": result.get("client_id"),
        }
    )
    save_state(state, state_dir)
    client.adopt(state["machine_id"], state["api_key"])
    if result.get("created"):
        how = "new"
    elif result.get("adopted"):
        # Worth its own word in the log: this PC came back after a re-image and
        # reclaimed the printers its predecessor had. "rotated" would hide that.
        how = "adopted a previous record for this computer name"
    else:
        how = "rotated"
    log.info("enrolled as machine %s (%s)", state["machine_id"], how)
    return state


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def poll_once(
    client: WorkstationClient,
    runner: ws.PowerShellRunner,
    *,
    computer_name: str,
    prefix: str = MANAGED_PREFIX,
    user: Optional[str] = None,
    state_dir: Optional[str] = None,
) -> ProvisionReport:
    """One cycle: ask, converge, check in.

    The check-in happens after provisioning and regardless of its outcome: a
    machine that is failing to provision is exactly the one an operator needs to
    see as alive, and skipping the check-in on failure would make it look
    offline instead.

    ``state_dir`` is threaded down to the driver cache so ``--state-dir`` moves
    the whole footprint. Without it the cache silently used the platform default
    while identity used the override -- harmless on a real install where the two
    coincide, and confusing the moment somebody runs the client by hand against a
    scratch directory, which is exactly when they are debugging something else.
    """
    payload = client.assignments(user=user)
    report = provision(
        runner, payload, prefix, client=client,
        cache_dir=os.path.join(state_dir, "drivers") if state_dir else None,
    )
    try:
        client.checkin(computer_name)
    except Exception as exc:
        log.warning("check-in failed: %s", exc)
    return report


def run(
    base_url: str,
    enroll_key: str,
    *,
    interval: float = 300.0,
    state_dir: Optional[str] = None,
    prefix: str = MANAGED_PREFIX,
    verify_tls: bool = True,
    once: bool = False,
    runner: Optional[ws.PowerShellRunner] = None,
    computer_name: Optional[str] = None,
) -> ProvisionReport:
    """Enroll if needed, then poll forever.

    Transport failures are logged and retried on the next tick rather than
    killing the service: a workstation is off-network constantly (a laptop lid,
    a VPN reconnect), and a print client that needs restarting after every one
    is a print client that gets uninstalled.

    **That includes enrollment**, which is the one this got wrong for as long as
    it existed. ``ensure_enrolled`` sat above the loop with no handler, so the
    rule held for every poll and failed at precisely the moment it matters most
    -- a freshly imaged Mac's first boot, where the network is least likely to be
    up. Found by installing on a real Mac (nothing below the ``_run`` seam can
    see it, and no unit test drives ``run()`` against an unreachable host): the
    process died on an unhandled ``httpx.ConnectError``, launchd respawned it
    every 60s, and the log grew ~9.8 MB/day of tracebacks stating no reason.

    A refused **key** is different and stays terminal: it re-raises, because
    retrying a credential central has rejected only buries the reason.
    """
    import socket

    name = computer_name or os.environ.get("COMPUTERNAME") or socket.gethostname()
    runner = runner or _platform_runner()
    client = WorkstationClient(base_url, verify_tls=verify_tls)

    report = ProvisionReport()
    while True:
        try:
            # INSIDE the loop, deliberately. Enrolling is exactly where a freshly
            # imaged Mac is least likely to have the network: first boot behind a
            # captive portal, a VLAN that is not up yet, a VPN that has not
            # connected. Hoisted above the loop -- where this used to be -- a
            # transport failure there escaped uncaught and killed the process,
            # which is the precise opposite of this function's contract. launchd's
            # KeepAlive then respawned it every ThrottleInterval (60s -- five times
            # more often than a successful poll), each cycle writing a full httpx
            # traceback and no stated reason: measured at ~9.8 MB/day of
            # root-owned, unrotated log on a Mac pointed at an unreachable central.
            # Re-running it costs nothing once enrolled: it adopts the stored
            # credentials and makes no request at all.
            ensure_enrolled(
                client, enroll_key=enroll_key, computer_name=name,
                state_dir=state_dir,
            )
            report = poll_once(
                client, runner, computer_name=name, prefix=prefix,
                user=console_user(), state_dir=state_dir,
            )
            for queue, why in report.skipped.items():
                log.info("skipped %s: %s", queue, why)
            for queue, outcome in report.outcomes.items():
                log.info("queue %s: %s", queue, outcome)
            if report.default_applied:
                log.info("default printer: %s", report.default_applied)
            elif report.desired_default:
                log.info(
                    "default printer %s NOT applied: %s",
                    report.desired_default, report.default_reason,
                )
        except ServiceError:
            # A key central has refused is terminal, not transient. Re-raised so
            # the CLI reports it once and exits with its distinct code, rather
            # than this loop retrying a credential that will never be accepted
            # and burying the reason in a restart loop.
            raise
        except Exception as exc:
            report = ProvisionReport(cycle_error=f"{type(exc).__name__}: {exc}")
            log.warning("cycle failed, will retry in %ss: %s", interval, exc)
        if once:
            return report
        time.sleep(interval)
