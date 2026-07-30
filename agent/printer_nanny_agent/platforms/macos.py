"""macOS: CUPS queues, and a per-user default that lives in a file.

WHY THIS IS NOT A TRANSLATION OF THE WINDOWS BACKEND
----------------------------------------------------
The shape of the problem is different in three ways that each changed a
decision:

1. **There is no privilege cliff.** The Windows client exists as a LocalSystem
   service largely because KB5005652 made Point and Print demand local admin.
   macOS has no equivalent: ``lpadmin`` needs root (or the ``lpadmin`` group),
   and a LaunchDaemon already has it. So the daemon is about *unattended*
   operation, not about borrowing privilege the user lacks.

2. **Driverless is the native case, and it is a single flag.**
   ``lpadmin -m everywhere`` tells CUPS to fetch the printer's IPP attributes and
   build the PPD itself -- the direct analogue of ``Add-Printer -IppURL``, and
   for the same reason the right call: CUPS picks what the device says it
   supports rather than us guessing. Requires macOS 10.12+; older releases have
   no ``everywhere`` model and are refused rather than fudged.

3. **The default printer is a FILE, not an API.** ``lpoptions -d`` writes
   ``~/.cups/lpoptions`` for whoever runs it. So "set the console user's
   default" means running it *as that user*, which is ``sudo -u`` rather than
   token impersonation. The verify-then-report rule is identical to Windows and
   for the identical reason: ``lpoptions`` exits 0 having written a file that
   CUPS may not honour, so the default is read back before it is claimed.

FOUR THINGS PROVEN AGAINST A REAL SCHEDULER, EACH OF WHICH THIS CODE GOT WRONG
------------------------------------------------------------------------------
Every claim below was checked by driving this module against a live ``cupsd``
(CUPS 2.4.7). Each one was a defect found that way and by no other means -- the
unit tests below the ``_run`` seam passed throughout.

* **``lpoptions -d`` with no destination is a usage error, not a read.** It
  exits 1 printing usage; the read-back therefore always came back ``None`` and
  ``set_default_printer`` *always* raised "default did not stick". The whole
  feature could never once have succeeded. ``lpstat -d`` is the read
  (``system default destination: NAME``, or ``no system default destination``).
  Note also that a *successful* ``lpoptions -d NAME`` prints the queue's whole
  option list, so parsing its output for the name yields ``copies=1``.

* **Every parse here is locale-dependent.** On a German Mac ``lpstat -p`` says
  ``Drucker X ist im Leerlauf``, so a ``^printer\\s+(\\S+)`` match finds nothing:
  enumeration returns empty, every poll re-creates every queue -- a live IPP
  query per printer per poll, exactly the round trip this file promises not to
  make -- and no stale queue is ever removed. So every command runs under
  ``LC_ALL=C``, including through ``sudo`` (see ``_run``), and enumeration reads
  ``lpstat -v``, which yields name *and* URI in one call.

* **A failed ``-m everywhere`` used to leave a re-pointed, PPD-less queue.**
  ``cupsd`` commits ``device-uri`` and *then* runs the IPP query, so a repair
  against an unreachable printer exits 1 having already moved the queue and
  generated no PPD. That queue exists, is listed, matches the desired URI, and
  so converges as "unchanged" **forever** while being unable to print -- the
  macOS spelling of the tier-1 Windows bug this codebase already shipped once.
  A failed change is now unwound completely: a failed *repair* restores the
  previous URI (a ``-v``-only ``lpadmin`` is instant, needs no network, and
  leaves the PPD byte-identical -- measured), and a failed *create* removes the
  carcass. Rejected alternative: probing ``printer-make-and-model`` for
  ``Local Raw Printer``, which depends on an undocumented server-side string.

* **An unreachable printer costs 30 seconds.** ``cupsd``'s own connect timeout
  is ~30s, which the single 30s timeout here collided with -- so the failure
  surfaced as our useless "timed out" instead of ``lpadmin``'s own message. Live
  queries now get their own, larger timeout, and a whole pass is bounded by
  ``_QUERY_BUDGET`` so a rack of sleeping printers cannot outlast the poll
  interval. Queues the budget did not reach are reported as a stated skip, never
  silently omitted.

WHAT HAS NO MACOS EQUIVALENT
----------------------------
"Let Windows manage my default printer" has no counterpart -- macOS does not
silently re-point the default. ``manage_windows_default`` is therefore accepted
and ignored here, which is why the parameter keeps its Windows-specific name:
renaming it to something neutral would imply this backend honours a setting it
does not have.

Vendor drivers are NOT staged here yet. macOS deprecated vendor PPDs in 10.14
and vendor packages are ``.pkg`` installers rather than driver stores, so the
Windows ``pnputil`` path has no direct analogue; ``driver_required`` printers are
skipped with a stated reason, exactly as they were on Windows before a package
existed.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from typing import Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

NAME = "macos"

#: macOS deprecated vendor PPDs in 10.14 and vendor packages are ``.pkg``
#: installers rather than a driver store, so ``pnputil`` has no analogue here.
#: A ``driver_required`` printer is a stated skip, never a wrong-driver bind.
SUPPORTS_VENDOR_DRIVERS = False

#: Where a LaunchDaemon can write and an ordinary user cannot. /Library rather
#: than /var: it is the documented location for machine-wide application state
#: and survives OS upgrades, where /var/db has been reorganised more than once.
_STATE_BASE = "/Library/Application Support"

#: CUPS rejects these in a queue name outright -- space, slash, hash and quote.
#: Unlike the Windows list this is about the *scheduler's* validation, so it is
#: applied by us rather than discovered as a failure. Proven: CUPS refuses
#: ``PN Front Desk MFP (1)`` with "Printer name can only contain printable
#: characters", so the substitution is load-bearing and not decoration.
_CUPS_NAME_BANNED = set(' \t/#"\'\\?')

_LPADMIN = "/usr/sbin/lpadmin"
_LPSTAT = "/usr/bin/lpstat"
_LPOPTIONS = "/usr/bin/lpoptions"
_SUDO = "/usr/bin/sudo"
_ENV = "/usr/bin/env"

#: Local commands -- lpstat, lpoptions, and any lpadmin that does not carry
#: ``-m everywhere``. These talk to the scheduler over a domain socket and
#: return in milliseconds (a ``-v``-only lpadmin measured at 13ms), so this
#: bound is a wedge guard rather than a working budget.
_TIMEOUT = 15

#: ``lpadmin -m everywhere`` runs a live IPP query against the printer. This
#: must stay comfortably ABOVE cupsd's own connect timeout (~30s observed),
#: because a timeout of ours discards lpadmin's error message and replaces it
#: with "timed out", which tells a technician nothing about why the printer did
#: not answer.
_QUERY_TIMEOUT = 75

#: Wall-clock ceiling on live queries within a single ``provision_queues`` pass.
#: Without it, N unreachable printers cost N * 30s: a dozen sleeping devices
#: outlast the 300s default poll interval and the client never completes a
#: cycle. Deliberately under half that interval, and what it skips it *says*.
_QUERY_BUDGET = 150


class CupsError(RuntimeError):
    """A CUPS command failed. Carries the command's own stderr, because
    "lpadmin failed" without it sends a technician nowhere."""


def default_state_dir() -> str:
    base = os.environ.get("PN_STATE_DIR_BASE") or _STATE_BASE
    return os.path.join(base, "PrinterNanny")


# --------------------------------------------------------------------------- #
# Running CUPS commands
# --------------------------------------------------------------------------- #


def _run(
    argv: Sequence[str], *, as_user: Optional[str] = None, timeout: int = _TIMEOUT
) -> str:
    """Run a CUPS command under ``LC_ALL=C`` and return stdout.

    Every value reaches the command as a **separate argv element** -- there is no
    shell, so a queue name containing ``; rm -rf /`` is one argument and nothing
    interprets it. That is the same reasoning as the Windows backend's
    environment-variable discipline, reached by a different route: here the
    absence of a shell does the work, so there is no quoting to get wrong.

    ``LC_ALL=C`` is not tidiness. Every string this module parses comes out of
    ``_cupsLangPrintf``, so on a German Mac ``lpstat -p`` reads ``Drucker X ist
    im Leerlauf`` and enumeration silently finds nothing. Forcing the C locale
    makes the parses mean the same thing on every Mac regardless of the user's
    language.

    ``as_user`` runs it through ``sudo -u`` for the per-user default. The
    username comes from ``console_user_name()`` -- from the OS, never from
    central -- because a username interpolated into a privileged command from a
    remote source is exactly the wrong shape even when nothing is shell-parsed.
    The locale travels through an explicit ``/usr/bin/env`` rather than as a
    ``sudo VAR=value`` prefix, because whether sudo forwards that depends on the
    host's sudoers policy and this must not.

    An empty ``as_user`` is a **refusal**, not "run as root". Silently falling
    back to root is how a read-back verifies root's default printer and reports
    that the console user's was set -- precisely the failure this module's
    read-back exists to prevent.
    """
    cmd: List[str] = list(argv)
    if as_user is not None:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", as_user or ""):
            # Defensive: dscl/console names are this alphabet. A name outside it
            # -- including empty -- means something upstream is wrong, and
            # refusing beats running as the wrong user.
            raise CupsError(f"refusing to run as implausible username {as_user!r}")
        cmd = [_SUDO, "-u", as_user, _ENV, "LC_ALL=C", "LANG=C"] + cmd

    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False, env=env
        )
    except FileNotFoundError as exc:
        raise CupsError(f"{cmd[0]} not found: {exc}")
    except subprocess.TimeoutExpired:
        raise CupsError(f"{os.path.basename(argv[0])} timed out after {timeout}s")

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise CupsError(
            f"{os.path.basename(cmd[0])} exited {proc.returncode}: "
            + (detail[0] if detail else "no output")
        )
    return proc.stdout


# --------------------------------------------------------------------------- #
# Who is signed in
# --------------------------------------------------------------------------- #


def console_user_name() -> Optional[str]:
    """The short name of whoever owns the console, or None.

    ``stat -f %Su /dev/console`` is the documented shell-level equivalent of
    ``SCDynamicStoreCopyConsoleUser`` and needs no framework binding. At the
    login window this is ``root``, which is treated as nobody -- root is not a
    person whose default printer we should be setting.
    """
    try:
        out = _run(["/usr/bin/stat", "-f", "%Su", "/dev/console"]).strip()
    except CupsError as exc:
        log.info("could not read the console owner: %s", exc)
        return None
    if not out or out in ("root", "_windowserver"):
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", out):
        # Not a name we will hand to sudo. Reported, not silently accepted.
        log.warning("implausible console owner %r; treating as nobody", out)
        return None
    return out


def console_user() -> Optional[str]:
    """The console user as central identifies them -- UPN or email, else None.

    Central matches ``end_users.upn`` then ``email``, so a bare short name would
    match nothing. On a directory-bound Mac the UPN is recorded in the local
    directory node, so it is read from there; on a standalone Mac there is no
    UPN to find and this returns None, which the loop already handles as the
    login-screen case (the machine's own printers still provision).

    Deliberately NOT guessed by appending a domain: a fabricated UPN that
    happens to match another person's record would hand them someone else's
    printers, and this repo refuses that class of guess everywhere else.
    """
    short = console_user_name()
    if not short:
        return None

    for attr in ("NetworkUser", "UserPrincipalName", "EMailAddress"):
        try:
            out = _run(["/usr/bin/dscl", ".", "-read", f"/Users/{short}", attr])
        except CupsError:
            continue
        # dscl prints "attr: value" or "attr:\n value" -- take the last token.
        value = out.replace(f"{attr}:", " ").split()
        if value and "@" in value[-1]:
            return value[-1].strip().lower()

    log.info("no UPN or email recorded for console user %s", short)
    return None


# --------------------------------------------------------------------------- #
# Queues
# --------------------------------------------------------------------------- #


def cups_queue_name(name: str) -> str:
    """A queue name CUPS will accept, derived from the one central sent.

    CUPS rejects spaces outright, so the Windows-style ``PN Front Desk MFP (1)``
    cannot be used verbatim. Substituted rather than truncated, and the printer
    id central already embedded keeps it unique.
    """
    cleaned = re.sub(r"_+", "_", _substitute(name)).strip("_")
    return cleaned[:127] or "PN_printer"


def _substitute(name: str) -> str:
    return "".join("_" if ch in _CUPS_NAME_BANNED else ch for ch in name)


def cups_queue_prefix(prefix: str) -> str:
    """``managed_prefix`` as it appears at the front of a sanitised queue name.

    Separate from ``cups_queue_name`` because that strips leading and trailing
    underscores, and for a *prefix* the trailing one is the separator. The
    shipped prefix is ``"PN "``: sanitised as a name it becomes ``"PN"``, which
    matches a user's own ``PNMyPrinter`` and would delete it -- the precise
    failure the prefix exists to prevent. Sanitised as a prefix it is ``"PN_"``,
    which matches ``PN_Front_Desk`` and nothing the user named themselves.
    """
    return re.sub(r"_+", "_", _substitute(prefix)).lstrip("_")


#: The name a queue has on this machine, for whatever central called it. The
#: orchestrator keys its whole report through this so ``skipped``, ``outcomes``
#: and ``desired_default`` cannot disagree about what a printer is called.
queue_name = cups_queue_name


def list_queue_uris() -> Dict[str, str]:
    """``{queue name: device URI}`` for every locally configured queue.

    ``lpstat -v`` rather than ``-p``: it enumerates exactly the same set --
    verified against a live scheduler, disabled queues included -- and carries
    the device URI, so convergence needs one command per pass instead of one
    plus one per queue.

    Deliberately **not** ``lpstat -e``, which also lists DNS-SD-discovered
    printers. On a Mac that means every Bonjour printer on the subnet would
    appear as an existing "queue" we might try to remove.

    An empty mapping when CUPS has none: ``lpstat`` exits non-zero in that case,
    which is not an error worth propagating.
    """
    try:
        out = _run([_LPSTAT, "-v"])
    except CupsError as exc:
        log.debug("lpstat -v reported no queues (%s)", exc)
        return {}
    # "device for NAME: URI". A queue name can never contain a space (CUPS
    # refuses it), so \S+ is exact rather than merely convenient.
    return {
        m.group(1): m.group(2)
        for m in re.finditer(r"^device for (\S+):\s*(\S+)\s*$", out, re.M)
    }


def list_queues() -> List[str]:
    """Existing queue names."""
    return sorted(list_queue_uris())


def queue_uri(name: str) -> Optional[str]:
    """The device URI a queue currently points at, or None if it has none.

    Read so convergence can tell "already correct" from "pointing at the old
    address" -- a re-addressed printer must be repaired in place rather than
    leaving a broken queue beside a new one.
    """
    return list_queue_uris().get(name)


def _set_uri(name: str, uri: str) -> None:
    """Point a queue at ``uri`` without touching its PPD.

    No ``-m``, so no IPP query: measured at 13ms against an unreachable address,
    with the generated PPD byte-identical afterwards. That is what makes an
    unwind possible at all.
    """
    _run([_LPADMIN, "-p", name, "-v", uri])


def _apply_everywhere(name: str, uri: str) -> None:
    """Create or re-point a queue as IPP Everywhere. Costs a live IPP query."""
    _run(
        [_LPADMIN, "-p", name, "-v", uri, "-m", "everywhere", "-E"],
        timeout=_QUERY_TIMEOUT,
    )


def _discard(name: str) -> None:
    """Best-effort removal used to unwind a failed create. Never raises: the
    queue may not exist, and the caller is already reporting a failure."""
    try:
        remove_queue(name)
    except CupsError as exc:
        log.warning("could not clean up the failed queue %s: %s", name, exc)


def ensure_driverless_queue(
    name: str, uri: str, known: Optional[Dict[str, str]] = None
) -> str:
    """Create or repair one IPP Everywhere queue. Returns what happened.

    ``-m everywhere`` is the whole point: CUPS queries the printer's IPP
    attributes and generates the PPD from what the device reports. Handing it a
    vendor PPD we picked would be the macOS spelling of the tier-1 mistake this
    codebase already made once on Windows -- guessing what the device wants
    instead of asking it.

    Converges rather than blindly creating: this runs on every poll, so an
    unconditional ``lpadmin`` would either fail or silently churn. An existing
    queue already on the right URI is left completely alone -- and costs no
    network round trip, because ``-m everywhere`` is a live query and re-running
    it would fail queues whose printer is merely asleep.

    **A failure leaves nothing half-applied.** ``cupsd`` commits ``device-uri``
    before it runs the query, so a failed repair would otherwise strand the
    queue on the new URI with no PPD -- listed, matching what central wants, and
    unable to print, forever. A failed repair therefore restores the previous
    URI and a failed create removes the queue, so the next poll retries from a
    known state.

    ``known`` is a pre-fetched name->URI map so a whole pass costs one
    enumeration; omitted, it is fetched here.
    """
    if known is None:
        known = list_queue_uris()

    if name in known:
        previous = known[name]
        if previous == uri:
            return "unchanged"
        try:
            _apply_everywhere(name, uri)
        except CupsError:
            # Unwind to exactly the prior state: same URI, same PPD.
            try:
                _set_uri(name, previous)
            except CupsError as undo:
                log.warning(
                    "queue %s left on %s after a failed repair: %s", name, uri, undo
                )
            raise
        return "updated"

    try:
        _apply_everywhere(name, uri)
    except CupsError:
        _discard(name)
        raise
    # Shared defaults to on for a new CUPS queue, which would advertise a
    # workstation's printers to the whole subnet. Off explicitly.
    try:
        _run([_LPADMIN, "-p", name, "-o", "printer-is-shared=false"])
    except CupsError as exc:  # not fatal -- the queue works, it is just shared
        log.warning("could not unshare %s: %s", name, exc)
    return "created"


def remove_queue(name: str) -> None:
    _run([_LPADMIN, "-x", name])


def provision_queues(
    runner, desired: Sequence[dict], managed_prefix: str = ""
) -> Dict[str, str]:
    """Make CUPS match ``desired``. Same contract as the Windows backend.

    ``runner`` is accepted and unused -- the seam is shared with Windows, where
    it carries the PowerShell runner. Dropping it from the signature would make
    the two backends non-interchangeable for the sake of one unused parameter.

    Only queues carrying ``managed_prefix`` are ever removed, and an empty prefix
    disables removal entirely rather than deleting everything unrecognised.
    Deleting a printer the user added themselves is how a print tool gets
    uninstalled -- true on macOS for exactly the same reason.

    Live queries are bounded by ``_QUERY_BUDGET`` across the pass. Converged
    queues cost nothing and are never affected; what the budget stops is a
    *first* pass against a dozen sleeping printers taking longer than the poll
    interval. Anything it did not reach is reported as a skip naming the reason,
    because a queue silently absent from the outcomes reads to central as a
    queue that was never assigned.
    """
    outcomes: Dict[str, str] = {}
    wanted = set()
    known = list_queue_uris()
    spent = 0.0
    exhausted = False

    for spec in desired:
        name = cups_queue_name(spec["name"])
        wanted.add(name)
        if spec.get("tier") != "driverless":
            # Vendor drivers are not staged on macOS yet. Stated, never a
            # silently missing queue.
            outcomes[name] = (
                "error: vendor drivers are not supported on macOS yet; "
                "this printer needs one"
            )
            continue
        if known.get(name) == spec["uri"]:
            # No network, so no budget consumed -- the steady state is free.
            outcomes[name] = "unchanged"
            continue
        if exhausted:
            outcomes[name] = (
                f"skipped: time budget for this pass ({_QUERY_BUDGET}s) was spent "
                "on printers that did not answer; will retry next poll"
            )
            continue
        started = time.monotonic()
        try:
            outcomes[name] = ensure_driverless_queue(name, spec["uri"], known=known)
        except CupsError as exc:
            # One bad queue must not abort the rest, and the reason travels so
            # central can show which printer failed and why.
            log.warning("queue %s failed: %s", name, exc)
            outcomes[name] = f"error: {exc}"
        spent += time.monotonic() - started
        if spent >= _QUERY_BUDGET:
            exhausted = True

    if managed_prefix:
        prefix = cups_queue_prefix(managed_prefix)
        for existing in sorted(list_queue_uris()):
            if existing.startswith(prefix) and existing not in wanted:
                try:
                    remove_queue(existing)
                    outcomes[existing] = "removed"
                except CupsError as exc:
                    outcomes[existing] = f"error: could not remove: {exc}"

    return outcomes


# --------------------------------------------------------------------------- #
# The user's default printer
# --------------------------------------------------------------------------- #


def read_default_printer(as_user: str) -> Optional[str]:
    """That user's current default, or None.

    ``lpstat -d``, not ``lpoptions -d``: the latter *requires* a destination and
    exits 1 printing usage when given none, so the read-back it used to do could
    only ever return None -- meaning the default was reported as never having
    stuck even when it had. (And a successful ``lpoptions -d NAME`` prints the
    queue's whole option list, so parsing that for a name yields ``copies=1``.)

    Read as the user, because the answer is per-user: proven against a live
    scheduler, root reads its own ``/etc/cups/lpoptions`` default while the
    console user reads ``~/.cups/lpoptions``, so reading as root is how you
    convince yourself a write worked when it did not.

    What comes back is the user's **effective** default -- their own
    ``~/.cups/lpoptions`` if they have one, else the system-wide default, else
    None -- which is CUPS's own precedence and the right question to ask: the
    effective default is what their applications will print to. A queue instance
    (``NAME/duplex``) reports as ``NAME``, because a user whose default is an
    instance of our queue *does* have our queue as their default, and comparing
    the raw string would report the assigned default as never having stuck.
    """
    try:
        out = _run([_LPSTAT, "-d"], as_user=as_user)
    except CupsError:
        return None
    # C locale: "system default destination: NAME", or "no system default
    # destination". Split on the last colon so the phrase itself never matters.
    if ":" not in out:
        return None
    value = out.rsplit(":", 1)[1].strip()
    if not value:
        return None
    # CUPS appends "/instance" for a queue instance, which is not part of the
    # queue's name.
    return value.split()[0].split("/")[0]


def set_default_printer(name: str, *, manage_windows_default: bool = True) -> str:
    """Make ``name`` the console user's default. Returns the verified name.

    ``manage_windows_default`` is accepted and ignored: macOS has no equivalent
    of "Let Windows manage my default printer" and does not silently re-point
    the default. The parameter keeps its Windows name so the two backends stay
    interchangeable; a neutral name would imply this honours a setting it does
    not have.

    Verified by reading back **as that user**, for the same reason Windows reads
    back: ``lpoptions`` exits 0 having written a file, which is not evidence that
    CUPS resolved the default to what we asked for.
    """
    from printer_nanny_agent.workstation_service import DefaultPrinterError

    if not (name or "").strip():
        raise DefaultPrinterError("no default requested")

    user = console_user_name()
    if not user:
        # The login window. Not a failure -- there is no per-user default to set.
        raise DefaultPrinterError("nobody is signed in")

    queue = cups_queue_name(name)
    try:
        _run([_LPOPTIONS, "-d", queue], as_user=user)
    except CupsError as exc:
        raise DefaultPrinterError(f"lpoptions failed: {exc}")

    got = read_default_printer(user)
    if got != queue:
        raise DefaultPrinterError(
            f"default did not stick (wanted {queue!r}, got {got!r})"
        )
    return got
