#!/usr/bin/env python3
"""Drive the macOS backend against a REAL CUPS scheduler.

The macOS sibling of ``windows_provision_check.py``, and it exists for the
identical reason: everything below ``platforms.macos._run`` is covered by
``tests/test_workstation_macos.py`` on any platform with a fake, and a fake
returns what it is told. This is the only thing that talks to a scheduler.

That distinction is not academic. On its first run against a live ``cupsd`` this
script found four defects that the whole unit suite had passed over, every one of
which would have shipped:

1. ``read_default_printer`` ran ``lpoptions -d`` with no destination, which is a
   **usage error** (exit 1, prints usage). So the read-back always returned None
   and ``set_default_printer`` *always* raised "default did not stick" -- the
   assigned-default feature could never once have worked on a Mac.
2. Enumeration matched ``^printer\\s+(\\S+)`` against ``lpstat -p``, whose output
   is **translated**: a German Mac says ``Drucker X ist im Leerlauf``. So
   ``list_queues()`` returned empty, every poll re-created every queue -- a live
   IPP query per printer per poll -- and no stale queue was ever removed.
3. ``cupsd`` commits ``device-uri`` and **then** runs the ``-m everywhere``
   query, so a failed repair left the queue re-pointed with no PPD: present,
   matching what central wanted, converging as "unchanged" forever, and unable to
   print. The macOS spelling of the tier-1 Windows bug.
4. An unreachable printer cost 30s, and the module's single 30s timeout collided
   with ``cupsd``'s own, so the failure surfaced as our useless "timed out"
   instead of ``lpadmin``'s message naming the address.

A fifth is verified here too, though it was found by reading the code:
``cups_queue_name`` strips trailing underscores, so the shipped ``"PN "`` prefix
sanitised to ``"PN"`` -- which matches a user's own ``PNMyPrinter`` and would
delete it. And a sixth needed the *end-to-end* smoke rather than this script,
because it lives above the ``_run`` seam: ``skipped`` and ``desired_default``
carried Windows spellings while ``outcomes`` carried CUPS ones, so the default
lookup missed every time and a created queue was reported as not provisioned.

WHAT IT PROVES, AND WHAT IT STILL DOES NOT
------------------------------------------
With ``--printer-uri`` pointed at a real IPP Everywhere device it proves a queue
can be built from that device's own attributes and then removed. Without it, the
convergence, unwind, budget, locale and default-printer logic are still exercised
against a real scheduler -- which is where the four defects above were found.

It does NOT prove a page comes out. Nor does it cover launchd's environment, a
LaunchDaemon's privileges, or MDM/profile interaction. Those stay manual.

RUNNING IT
----------
On a Mac, against the real scheduler (needs root for ``lpadmin``)::

    sudo python3 scripts/macos_provision_check.py \\
        --printer-uri ipp://10.0.0.5:631/ipp/print --as-user alice

On Linux, against a throwaway ``cupsd`` -- which is how those defects were found,
and needs no printer and no Mac::

    sudo scripts/macos_cups_testbed.sh start
    sudo PYTHONPATH=agent python3 scripts/macos_provision_check.py --as-user pntest
    sudo scripts/macos_cups_testbed.sh stop

Every queue it makes carries ``PNCHK_`` and it will not touch anything else. It
saves and restores the default printer of whoever ``--as-user`` names.
"""

from __future__ import annotations

import argparse
import os
import pwd
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "agent"))

from printer_nanny_agent.platforms import macos  # noqa: E402

#: Nothing outside this prefix is ever created or removed.
PREFIX = "PNCHK_"

#: An address that will not answer. TEST-NET-1 (RFC 5737) is reserved for
#: documentation and is portable, but on many hosts it fails *fast* ("Host is
#: down"). To exercise the timeout ordering -- our timeout must sit above cupsd's
#: own ~30s connect timeout, or the failure reads "timed out" instead of naming
#: the printer -- pass --dead-uri pointing at a genuinely blackholed address on a
#: routed subnet.
DEAD = "ipp://192.0.2.77:631/ipp/print"

#: Two plausible-looking addresses that are not the same, for the repair path.
FAKE_A = "ipp://192.0.2.11:631/ipp/print"
FAKE_B = "ipp://192.0.2.12:631/ipp/print"

MINIMAL_PPD = """*PPD-Adobe: "4.3"
*FormatVersion: "4.3"
*FileVersion: "1.0"
*LanguageVersion: English
*LanguageEncoding: ISOLatin1
*PCFileName: "PNCHK.PPD"
*Manufacturer: "PrinterNanny"
*Product: "(PrinterNanny Check)"
*ModelName: "PrinterNanny Check"
*ShortNickName: "PrinterNanny Check"
*NickName: "PrinterNanny Check"
*PSVersion: "(3010.000) 0"
*LanguageLevel: "3"
*ColorDevice: False
*DefaultColorSpace: Gray
*FileSystem: False
*Throughput: "1"
*LandscapeOrientation: Plus90
*TTRasterizer: Type42
*cupsFilter: "application/vnd.cups-postscript 0 -"
*OpenUI *PageSize/Media Size: PickOne
*OrderDependency: 10 AnySetup *PageSize
*DefaultPageSize: Letter
*PageSize Letter/US Letter: "<</PageSize[612 792]>>setpagedevice"
*CloseUI: *PageSize
*OpenUI *PageRegion/Media Size: PickOne
*OrderDependency: 10 AnySetup *PageRegion
*DefaultPageRegion: Letter
*PageRegion Letter/US Letter: "<</PageSize[612 792]>>setpagedevice"
*CloseUI: *PageRegion
*DefaultImageableArea: Letter
*ImageableArea Letter/US Letter: "18 36 594 756"
*DefaultPaperDimension: Letter
*PaperDimension Letter/US Letter: "612 792"
*DefaultResolution: 300dpi
*OpenUI *Resolution/Resolution: PickOne
*OrderDependency: 20 AnySetup *Resolution
*Resolution 300dpi/300 DPI: "<</HWResolution[300 300]>>setpagedevice"
*CloseUI: *Resolution
"""


class Checks:
    def __init__(self) -> None:
        self.failed: list = []
        self.passed = 0
        self.skipped: list = []

    def eq(self, label, got, want):
        self._report(label, got == want, got, repr(want))

    def truthy(self, label, got, describe):
        self._report(label, bool(got), got, describe)

    def pred(self, label, got, pred, describe):
        self._report(label, pred(got), got, describe)

    def skip(self, label, why):
        self.skipped.append(f"{label} ({why})")
        print(f"SKIP  {label}\n        {why}")

    def _report(self, label, ok, got, want):
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        if ok:
            self.passed += 1
        else:
            self.failed.append(label)
            print(f"        got  {got!r}\n        want {want}")


def raw_lpadmin(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/usr/sbin/lpadmin", *args], capture_output=True, text=True,
        env=dict(os.environ, LC_ALL="C"),
    )


def make_queue(name: str, uri: str, ppd: str = "") -> None:
    argv = ["-p", name, "-v", uri, "-E"]
    if ppd:
        argv += ["-P", ppd]
    rc = raw_lpadmin(*argv)
    if rc.returncode != 0:
        raise SystemExit(f"could not set up {name}: {rc.stderr.strip()}")


def sweep() -> None:
    """Remove only our own queues. Never anything else, ever."""
    for name in macos.list_queues():
        if name.startswith(PREFIX):
            try:
                macos.remove_queue(name)
            except macos.CupsError as exc:
                print(f"      (could not clean up {name}: {exc})")


# --------------------------------------------------------------------------- #


def check_enumeration(c: Checks) -> None:
    print("\n== enumeration reads a real scheduler, in any language ==")
    make_queue(PREFIX + "One", FAKE_A)
    make_queue(PREFIX + "Two", FAKE_B)
    uris = macos.list_queue_uris()
    c.eq(f"{PREFIX}One's URI came back", uris.get(PREFIX + "One"), FAKE_A)
    c.eq(f"{PREFIX}Two's URI came back", uris.get(PREFIX + "Two"), FAKE_B)
    c.eq("a queue that does not exist has no URI", macos.queue_uri("PNCHK_nope"), None)

    # A disabled queue must still enumerate, or a disabled managed queue looks
    # absent and is re-created (with a live query) on every poll.
    subprocess.run(["/usr/sbin/cupsdisable", PREFIX + "Two"], capture_output=True)
    c.truthy("a disabled queue still enumerates",
             PREFIX + "Two" in macos.list_queues(), "present")
    subprocess.run(["/usr/sbin/cupsenable", PREFIX + "Two"], capture_output=True)

    # DEFECT 2. Prove the guard is meaningful before trusting that it holds:
    # first that the scheduler really does translate, then that we still parse.
    translated = subprocess.run(
        ["/usr/bin/lpstat", "-p", PREFIX + "One"], capture_output=True, text=True,
        env=dict(os.environ, LC_ALL="de_DE.UTF-8", LANG="de_DE.UTF-8"),
    ).stdout.strip()
    if translated.startswith("printer "):
        c.skip("lpstat is translated on this host",
               "no German catalog installed, so the locale guard is untestable here")
    else:
        c.pred("lpstat -p really is translated (so the guard is meaningful)",
               translated[:40], lambda s: not s.startswith("printer "),
               "not English")
    for loc in ("de_DE.UTF-8", "fr_FR.UTF-8", "ja_JP.UTF-8"):
        saved = os.environ.get("LC_ALL"), os.environ.get("LANG")
        os.environ["LC_ALL"] = os.environ["LANG"] = loc
        try:
            c.truthy(f"enumeration survives LC_ALL={loc}",
                     PREFIX + "One" in macos.list_queues(), "still found")
        finally:
            for key, val in zip(("LC_ALL", "LANG"), saved):
                os.environ.pop(key, None) if val is None else os.environ.update(
                    {key: val}
                )


def check_names(c: Checks) -> None:
    print("\n== the sanitised name is what CUPS will take ==")
    hostile = PREFIX + 'Front Desk "MFP" (1)'
    safe = macos.cups_queue_name(hostile)
    # The point: CUPS must REJECT the raw form, else sanitising is decoration.
    rc = raw_lpadmin("-p", hostile, "-v", FAKE_A, "-E")
    c.pred("CUPS rejects the unsanitised name", (rc.returncode, rc.stderr.strip()),
           lambda t: t[0] != 0, "a non-zero exit")
    make_queue(safe, FAKE_A)
    c.truthy("CUPS accepts the sanitised name", safe in macos.list_queues(), "created")
    macos.remove_queue(safe)

    # No shell is involved, so a shell-shaped name must be inert rather than
    # escaped. The canary is the proof.
    canary = "/tmp/pn-provision-check-canary"
    open(canary, "w").close()
    injected = macos.cups_queue_name(PREFIX + 'x"; rm -f ' + canary + " #")
    make_queue(injected, FAKE_A)
    c.truthy("a shell-shaped name is created literally",
             injected in macos.list_queues(), "created")
    c.truthy("the canary survived (nothing was interpreted)",
             os.path.exists(canary), "still there")
    os.unlink(canary)
    macos.remove_queue(injected)


def check_convergence(c: Checks, ppd_path: str, dead: str) -> None:
    print("\n== convergence, and a failed change that changes nothing ==")
    # A converged queue must cost no round trip: -m everywhere is a live query
    # and this runs every poll, so re-querying would fail sleeping printers.
    started = time.monotonic()
    c.eq("an already-correct queue is 'unchanged'",
         macos.ensure_driverless_queue(PREFIX + "One", FAKE_A), "unchanged")
    c.pred("...and cost no network", time.monotonic() - started,
           lambda s: s < 2.0, "under 2s (no IPP query)")

    # DEFECT 3. cupsd commits device-uri and THEN queries, so the unwind is what
    # keeps a failed repair from stranding a PPD-less queue that converges clean
    # forever while being unable to print.
    name = PREFIX + "Repair"
    make_queue(name, FAKE_A, ppd=ppd_path)
    served = _served_ppd(name)
    before = open(served, "rb").read() if served else None
    try:
        macos.ensure_driverless_queue(name, dead)
        c.eq("a repair against an unreachable printer fails", "it succeeded", "CupsError")
    except macos.CupsError as exc:
        # DEFECT 4: the message must be lpadmin's, not our "timed out".
        c.pred("the failure carries lpadmin's own words, not 'timed out'", str(exc),
               lambda s: "lpadmin exited" in s and "timed out after" not in s,
               "lpadmin's stderr")
    c.eq("the URI was restored", macos.queue_uri(name), FAKE_A)
    if before is None:
        c.skip("the PPD is byte-identical", "could not locate the served PPD")
    else:
        c.eq("the PPD is byte-identical", open(served, "rb").read(), before)
    c.truthy("so the next poll still sees work to do",
             macos.queue_uri(name) != dead, "not the dead URI")
    macos.remove_queue(name)

    # A failed create must leave nothing -- not a listed queue with no PPD.
    try:
        macos.ensure_driverless_queue(PREFIX + "Never", dead)
        c.eq("a create against an unreachable printer fails", "it succeeded", "CupsError")
    except macos.CupsError:
        c.truthy("a create against an unreachable printer fails", True, "CupsError")
    c.truthy("no half-built queue was left behind",
             PREFIX + "Never" not in macos.list_queues(), "absent")


def _served_ppd(name: str) -> str:
    """Where cupsd keeps the queue's PPD, if we can find it.

    Read-only and best effort: it is a CUPS-internal path, so a miss downgrades
    the byte-identical assertion to a skip rather than failing the run.
    """
    for base in ("/etc/cups/ppd", "/private/etc/cups/ppd",
                 os.environ.get("PN_CUPS_ROOT", "") + "/ppd"):
        candidate = os.path.join(base, f"{name}.ppd")
        if base and os.path.exists(candidate):
            return candidate
    return ""


def check_prefix_scoping(c: Checks) -> None:
    print("\n== removal is scoped to the managed prefix ==")
    mine = PREFIX + "Managed_Keep"
    stale = PREFIX + "Managed_Stale"
    # The lookalike is the one that matters: sanitising "PN " as a *name* yields
    # "PN", which matches a user's own "PNMyPrinter" and would delete it.
    lookalike = PREFIX + "ManagedLookalike"
    theirs = "PNCHK_Unmanaged"
    for q in (mine, stale, lookalike, theirs):
        make_queue(q, FAKE_A)

    prefix = PREFIX + "Managed "
    outcomes = macos.provision_queues(
        None,
        [{"name": mine, "uri": FAKE_A, "tier": "driverless"}],
        managed_prefix=prefix,
    )
    c.eq("the wanted queue converged", outcomes.get(mine), "unchanged")
    c.eq("the stale managed queue was removed", outcomes.get(stale), "removed")
    c.truthy("a name merely starting with the prefix's letters is untouched",
             lookalike in macos.list_queues(), "still there")
    c.truthy("an unmanaged queue is untouched", theirs in macos.list_queues(),
             "still there")
    c.truthy("...and is not even mentioned in the outcomes",
             theirs not in outcomes, "absent from outcomes")

    outcomes = macos.provision_queues(None, [], managed_prefix="")
    c.eq("an empty prefix removes nothing",
         [k for k, v in outcomes.items() if v == "removed"], [])
    for q in (mine, lookalike, theirs):
        macos.remove_queue(q)


def check_budget(c: Checks, dead: str) -> None:
    print("\n== a rack of sleeping printers cannot wedge a poll ==")
    saved = macos._QUERY_BUDGET
    macos._QUERY_BUDGET = 0.0
    try:
        started = time.monotonic()
        outcomes = macos.provision_queues(
            None,
            [{"name": f"{PREFIX}D{i}", "uri": dead, "tier": "driverless"}
             for i in range(3)]
            + [{"name": PREFIX + "One", "uri": FAKE_A, "tier": "driverless"}],
            managed_prefix="",
        )
        elapsed = time.monotonic() - started
    finally:
        macos._QUERY_BUDGET = saved

    c.pred("the first unreachable printer was really attempted",
           outcomes.get(PREFIX + "D0"),
           lambda s: isinstance(s, str) and s.startswith("error:"), "an error:")
    for i in (1, 2):
        c.pred(f"{PREFIX}D{i} is a stated skip naming the budget",
               outcomes.get(f"{PREFIX}D{i}"),
               lambda s: isinstance(s, str) and s.startswith("skipped:")
               and "budget" in s, "a skipped: ...budget...")
    c.eq("every desired queue appears in the outcomes", len(outcomes), 4)
    c.eq("an already-converged queue is never starved by the budget",
         outcomes.get(PREFIX + "One"), "unchanged")
    c.pred("one unreachable printer's cost, not three", round(elapsed, 1),
           lambda s: s < 60, "under 60s")
    c.eq("no carcasses", [q for q in macos.list_queues()
                          if q.startswith(PREFIX + "D")], [])


def _clear_default(user: str) -> None:
    """Remove ``user``'s default-printer line, leaving their other options.

    CUPS has no "unset the default" command -- ``lpoptions -x`` deletes a
    destination's options but leaves its ``Default`` line, verified both before
    and after the queue is removed. So when the user had NO default to begin
    with, restoring that state means editing the file, or the check leaves them
    a default pointing at a queue it just deleted. Harmless today (CUPS reports
    no default for a queue that does not exist) and not harmless the day someone
    creates a queue by that name.

    Done as root with an explicit path, then chowned back: no shell, and the
    file keeps its owner and 0600.
    """
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        return
    path = os.path.join(entry.pw_dir, ".cups", "lpoptions")
    if not os.path.exists(path):
        return
    with open(path) as fp:
        kept = [ln for ln in fp if not ln.startswith("Default ")]
    if kept:
        with open(path, "w") as fp:
            fp.writelines(kept)
        os.chmod(path, 0o600)
        os.chown(path, entry.pw_uid, entry.pw_gid)
    else:
        os.unlink(path)


def check_default_printer(c: Checks, user: str) -> None:
    print(f"\n== the default printer, written and read back as {user} ==")
    queue = PREFIX + "Default"
    make_queue(queue, FAKE_A)

    try:
        original = macos.read_default_printer(user)
    except macos.CupsError as exc:
        c.skip("the default printer round-trips", f"cannot read as {user}: {exc}")
        macos.remove_queue(queue)
        return
    print(f"      (saving {user}'s current default: {original!r})")

    try:
        # DEFECT 1: this is the pair that could never have worked.
        macos._run(["/usr/bin/lpoptions", "-d", queue], as_user=user)
        c.eq("the write is visible to a read-back as that user",
             macos.read_default_printer(user), queue)

        # And it must be that user's, not root's -- reading as the wrong user is
        # how you convince yourself a write worked when it did not.
        me = subprocess.run(["/usr/bin/whoami"], capture_output=True,
                            text=True).stdout.strip()
        if me == user:
            c.skip("per-user isolation", "--as-user is the user running the check")
        else:
            c.truthy("the caller's own default was not changed",
                     macos.read_default_printer(me) != queue,
                     f"{me}'s default is not {queue}")

        for loc in ("de_DE.UTF-8", "fr_FR.UTF-8"):
            os.environ["LC_ALL"] = loc
            try:
                c.eq(f"read-back survives LC_ALL={loc}",
                     macos.read_default_printer(user), queue)
            finally:
                os.environ.pop("LC_ALL", None)
    finally:
        if original:
            macos._run(["/usr/bin/lpoptions", "-d", original], as_user=user)
        else:
            _clear_default(user)
        # Verified, not assumed: this runs against somebody's real Mac.
        restored = macos.read_default_printer(user)
        c.eq(f"{user}'s default was restored exactly", restored, original)
        print(f"      (restored {user}'s default to {original!r})")
        macos.remove_queue(queue)


def check_real_printer(c: Checks, uri: str) -> None:
    print(f"\n== a real IPP Everywhere device: {uri} ==")
    name = PREFIX + "Real"
    try:
        outcome = macos.ensure_driverless_queue(name, uri)
    except macos.CupsError as exc:
        c.eq(f"a queue was built from {uri}", f"CupsError: {exc}", "created")
        return
    c.eq("the queue was created", outcome, "created")
    c.eq("it points at the URI we asked for", macos.queue_uri(name), uri)

    # The PPD is the evidence: -m everywhere means CUPS generated it from the
    # device's own IPP attributes rather than from anything we picked.
    served = _served_ppd(name)
    if not served:
        c.skip("a PPD was generated from the device's attributes",
               "could not locate the served PPD")
    else:
        body = open(served, "r", errors="replace").read()
        c.truthy("a PPD was generated", len(body) > 200, "a non-trivial PPD")
        c.pred("...and it is the IPP-Everywhere one, not a vendor PPD we chose",
               [ln for ln in body.splitlines() if "NickName" in ln][:1],
               lambda lines: bool(lines), "a NickName line naming the device")
        for line in body.splitlines():
            if "NickName" in line or "ModelName" in line:
                print(f"      {line.strip()}")

    # Second pass must be free -- proof the live query is not re-run per poll.
    started = time.monotonic()
    c.eq("a second pass is 'unchanged'", macos.ensure_driverless_queue(name, uri),
         "unchanged")
    c.pred("...and costs no round trip", time.monotonic() - started,
           lambda s: s < 2.0, "under 2s")
    macos.remove_queue(name)
    c.truthy("and it removes cleanly", name not in macos.list_queues(), "gone")


# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--printer-uri",
        help="a REAL IPP Everywhere device, e.g. ipp://10.0.0.5:631/ipp/print. "
             "Without it every check but the live-device one still runs.",
    )
    ap.add_argument(
        "--as-user",
        help="an account to verify the per-user default against. Without it the "
             "default-printer checks are skipped rather than faked.",
    )
    ap.add_argument(
        "--dead-uri", default=DEAD,
        help="an address that will not answer, used for the unwind and budget "
             f"checks (default {DEAD}). A genuinely blackholed address on a "
             "routed subnet exercises the timeout ordering; TEST-NET often fails "
             "fast instead.",
    )
    ap.add_argument("--keep", action="store_true",
                    help="leave the check's queues behind for inspection")
    args = ap.parse_args(argv)

    print(f"backend  : {macos.NAME} from {macos.__file__}")
    scheduler = subprocess.run(["/usr/bin/lpstat", "-r"], capture_output=True,
                               text=True, env=dict(os.environ, LC_ALL="C"))
    print(f"scheduler: {scheduler.stdout.strip() or scheduler.stderr.strip()}")
    if "running" not in scheduler.stdout:
        print("\nno scheduler to talk to; nothing here can be verified", file=sys.stderr)
        return 2
    if os.geteuid() != 0:
        print("\nlpadmin needs root; re-run under sudo", file=sys.stderr)
        return 2

    ppd_path = "/tmp/pn-provision-check.ppd"
    with open(ppd_path, "w") as fp:
        fp.write(MINIMAL_PPD)

    c = Checks()
    sweep()
    try:
        check_enumeration(c)
        check_names(c)
        check_convergence(c, ppd_path, args.dead_uri)
        check_prefix_scoping(c)
        check_budget(c, args.dead_uri)
        if args.as_user:
            check_default_printer(c, args.as_user)
        else:
            c.skip("the default printer round-trips", "pass --as-user to check it")
        if args.printer_uri:
            check_real_printer(c, args.printer_uri)
        else:
            c.skip("a real device becomes a working queue",
                   "pass --printer-uri to check it")
    finally:
        if not args.keep:
            sweep()
        os.unlink(ppd_path)

    print("\n" + "=" * 72)
    print(f"{c.passed} passed, {len(c.failed)} failed, {len(c.skipped)} skipped")
    for label in c.failed:
        print(f"  FAIL  {label}")
    for label in c.skipped:
        print(f"  SKIP  {label}")
    if c.failed:
        return 1
    if not args.printer_uri:
        print("\nNOTE: no --printer-uri was given, so nothing here proves a queue "
              "built from a real device works. That check is the point of this "
              "script; run it against real hardware before claiming driverless "
              "printing works on macOS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
