"""Unit tests for the macOS backend, run on every platform with a fake ``_run``.

WHAT THESE CAN AND CANNOT PROVE
-------------------------------
Below ``_run`` these are exhaustive; above it they prove nothing, exactly as the
Windows tests prove nothing about a real spooler. That is not a hedge -- it is
the lesson this repo already paid for once: every ``tests/windows/`` test passed
while tier 1 could not print, because a fake runner returns what it is told.

So the four defects this backend actually had -- ``lpoptions -d`` being a usage
error, locale-translated output defeating enumeration, a failed repair stranding
a PPD-less queue, and a 30s-per-printer stall -- were found by
``scripts/macos_provision_check.py`` against a live scheduler, and not by
anything here. What these tests do is stop those four from coming back, by
asserting the *shape* of what reaches CUPS: the exact commands, the C locale,
the unwind, the budget.
"""

from __future__ import annotations

import hashlib
import os
import subprocess

import pytest

from printer_nanny_agent.platforms import macos
from printer_nanny_agent.workstation_service import DefaultPrinterError

# This file deliberately runs everywhere against a fake ``_run``, and almost all
# of it is separator-agnostic. The exceptions are the few tests that compare a
# path the backend built with ``os.path.join`` against a literal POSIX string:
# on Windows those differ by the separator alone, which says nothing about the
# behaviour under test. The backend itself only ever runs on macOS.
posix_paths_only = pytest.mark.skipif(
    os.name == "nt", reason="compares POSIX path strings built by the macOS backend"
)


class FakeRun:
    """Records every argv and replies from a scripted table.

    A key is a tuple of substrings that must all appear in the joined command,
    most-specific key first, so a test can pin ``("lpadmin", "-x")`` without
    spelling out the whole command.
    """

    def __init__(self, replies=None):
        self.calls = []
        self.replies = dict(replies or {})

    def __call__(self, argv, *, as_user=None, timeout=macos._TIMEOUT):
        self.calls.append((list(argv), as_user, timeout))
        flat = " ".join(argv)
        for key in sorted(self.replies, key=lambda k: sum(map(len, k)), reverse=True):
            if all(part in flat for part in key):
                reply = self.replies[key]
                if isinstance(reply, Exception):
                    raise reply
                if callable(reply):
                    return reply(argv)
                return reply
        return ""

    def argvs(self):
        return [c[0] for c in self.calls]

    def flat(self):
        return [" ".join(c[0]) for c in self.calls]


@pytest.fixture
def fake(monkeypatch):
    f = FakeRun()
    monkeypatch.setattr(macos, "_run", f)
    return f


# --------------------------------------------------------------------------- #
# The C locale, which enumeration depends on entirely
# --------------------------------------------------------------------------- #


def test_run_forces_the_c_locale(monkeypatch):
    """A German Mac reports 'Drucker X ist im Leerlauf', which the enumeration
    regex does not match -- so every poll would re-create every queue and never
    remove a stale one. LC_ALL=C is what stops that."""
    seen = {}

    def fake_subprocess(cmd, **kw):
        seen["cmd"] = cmd
        seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(macos.subprocess, "run", fake_subprocess)
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    macos._run(["/usr/bin/lpstat", "-v"])
    assert seen["env"]["LC_ALL"] == "C"
    assert seen["env"]["LANG"] == "C"


def test_run_carries_the_c_locale_through_sudo(monkeypatch):
    """sudo scrubs the environment, so LC_ALL has to travel as an explicit
    /usr/bin/env rather than being inherited -- otherwise the read-back of a
    user's default parses translated prose."""
    seen = {}

    def fake_subprocess(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(macos.subprocess, "run", fake_subprocess)
    macos._run(["/usr/bin/lpstat", "-d"], as_user="alice")
    assert seen["cmd"] == [
        "/usr/bin/sudo", "-u", "alice",
        "/usr/bin/env", "LC_ALL=C", "LANG=C",
        "/usr/bin/lpstat", "-d",
    ]


def test_run_never_uses_a_shell(monkeypatch):
    """Queue names come from devices on customer LANs. There is no shell, so
    there is no quoting to get wrong."""
    seen = {}

    def fake_subprocess(cmd, **kw):
        seen.update(kw)
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(macos.subprocess, "run", fake_subprocess)
    macos._run(["/usr/sbin/lpadmin", "-p", 'x"; rm -rf / #'])
    assert seen.get("shell") in (None, False)
    assert seen["cmd"][2] == 'x"; rm -rf / #'


# --------------------------------------------------------------------------- #
# The sudo -u guard
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad", ["root; rm -rf /", "../root", "a b", "", "user$(id)", "root\nlp", "ali ce"]
)
def test_implausible_usernames_are_refused(monkeypatch, bad):
    monkeypatch.setattr(macos.subprocess, "run", lambda *a, **k: pytest.fail("ran"))
    with pytest.raises(macos.CupsError, match="implausible username"):
        macos._run(["/usr/bin/true"], as_user=bad)


def test_empty_as_user_is_refused_not_silently_root(monkeypatch):
    """An empty username must not mean 'as root'. Reading root's default and
    reporting the console user's was set is exactly what the read-back exists
    to prevent."""
    monkeypatch.setattr(macos.subprocess, "run", lambda *a, **k: pytest.fail("ran"))
    with pytest.raises(macos.CupsError, match="implausible username"):
        macos._run(["/usr/bin/lpstat", "-d"], as_user="")


def test_as_user_none_runs_without_sudo(monkeypatch):
    seen = {}

    def fake_subprocess(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(macos.subprocess, "run", fake_subprocess)
    macos._run(["/usr/bin/lpstat", "-v"], as_user=None)
    assert seen["cmd"] == ["/usr/bin/lpstat", "-v"]


# --------------------------------------------------------------------------- #
# Enumeration
# --------------------------------------------------------------------------- #


LPSTAT_V = (
    "device for PN_One: ipp://10.0.0.5:631/ipp/print\n"
    "device for PN_Two: ipp://10.0.0.9:631/ipp/print\n"
    "device for UserOwn: usb://Brother/HL-L2350DW\n"
)


def test_list_queue_uris_parses_lpstat_v(fake):
    fake.replies[("lpstat", "-v")] = LPSTAT_V
    assert macos.list_queue_uris() == {
        "PN_One": "ipp://10.0.0.5:631/ipp/print",
        "PN_Two": "ipp://10.0.0.9:631/ipp/print",
        "UserOwn": "usb://Brother/HL-L2350DW",
    }
    assert macos.list_queues() == ["PN_One", "PN_Two", "UserOwn"]


def test_enumeration_uses_lpstat_v_not_p_or_e(fake):
    """-p is prose that translates; -e also lists DNS-SD-discovered printers,
    which on a Mac means every Bonjour printer on the subnet would look like an
    existing queue."""
    fake.replies[("lpstat", "-v")] = LPSTAT_V
    macos.list_queue_uris()
    assert fake.flat() == ["/usr/bin/lpstat -v"]


def test_no_queues_is_empty_not_an_error(fake):
    fake.replies[("lpstat", "-v")] = macos.CupsError("lpstat exited 1: no destinations")
    assert macos.list_queue_uris() == {}
    assert macos.list_queues() == []
    assert macos.queue_uri("PN_One") is None


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,want",
    [
        ("PN Front Desk MFP (1)", "PN_Front_Desk_MFP_(1)"),
        ("PN_Already_Fine", "PN_Already_Fine"),
        # ; is legal in a CUPS queue name and there is no shell, so it stays.
        ('PN x"; rm -rf / #', "PN_x_;_rm_-rf"),
        ("PN  double   space", "PN_double_space"),
        ("   ", "PN_printer"),
        ("", "PN_printer"),
        ("PN/slash", "PN_slash"),
    ],
)
def test_cups_queue_name(raw, want):
    assert macos.cups_queue_name(raw) == want


def test_queue_name_is_bounded():
    assert len(macos.cups_queue_name("PN " + "x" * 500)) <= 127


def test_prefix_keeps_its_separator():
    """The bug this guards: cups_queue_name('PN ') is 'PN', which matches a
    user's own 'PNMyPrinter' and would delete it."""
    assert macos.cups_queue_prefix("PN ") == "PN_"
    assert not "PNMyPrinter".startswith(macos.cups_queue_prefix("PN "))
    assert macos.cups_queue_name("PN Front Desk").startswith(
        macos.cups_queue_prefix("PN ")
    )


# --------------------------------------------------------------------------- #
# Convergence
# --------------------------------------------------------------------------- #

URI = "ipp://10.0.0.5:631/ipp/print"
OTHER = "ipp://10.0.0.9:631/ipp/print"


def test_converged_queue_costs_no_network(fake):
    """-m everywhere is a live IPP query and this runs every poll, so a
    re-query would fail queues whose printer is merely asleep."""
    fake.replies[("lpstat", "-v")] = f"device for PN_A: {URI}\n"
    assert macos.ensure_driverless_queue("PN_A", URI) == "unchanged"
    assert not [c for c in fake.flat() if "lpadmin" in c]


def test_create_uses_everywhere_and_unshares(fake):
    fake.replies[("lpstat", "-v")] = ""
    assert macos.ensure_driverless_queue("PN_A", URI) == "created"
    assert fake.flat()[1:] == [
        f"/usr/sbin/lpadmin -p PN_A -v {URI} -m everywhere -E",
        "/usr/sbin/lpadmin -p PN_A -o printer-is-shared=false",
    ]


def test_a_new_queue_is_never_left_shared_on_failure_to_unshare(fake):
    """The queue works; it is just advertised. Not worth failing the queue."""
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("-o", "printer-is-shared=false")] = macos.CupsError("nope")
    assert macos.ensure_driverless_queue("PN_A", URI) == "created"


def test_live_query_gets_its_own_longer_timeout(fake):
    """Our timeout must sit above cupsd's own ~30s connect timeout, or the
    failure reads 'timed out' instead of naming the unreachable printer."""
    fake.replies[("lpstat", "-v")] = ""
    macos.ensure_driverless_queue("PN_A", URI)
    everywhere = [c for c in fake.calls if "everywhere" in c[0]][0]
    assert everywhere[2] == macos._QUERY_TIMEOUT
    assert macos._QUERY_TIMEOUT > 30


def test_repoint_updates_in_place(fake):
    fake.replies[("lpstat", "-v")] = f"device for PN_A: {OTHER}\n"
    assert macos.ensure_driverless_queue("PN_A", URI) == "updated"
    assert f"/usr/sbin/lpadmin -p PN_A -v {URI} -m everywhere -E" in fake.flat()


# --------------------------------------------------------------------------- #
# A failed change changes nothing -- the defect that mattered most
# --------------------------------------------------------------------------- #


def test_failed_repair_restores_the_previous_uri(fake):
    """cupsd commits device-uri and THEN runs the query, so a failed repair
    otherwise leaves the queue on the new URI with no PPD: listed, matching what
    central wants, converging as 'unchanged' forever, unable to print."""
    fake.replies[("lpstat", "-v")] = f"device for PN_A: {OTHER}\n"
    fake.replies[("everywhere",)] = macos.CupsError(
        "lpadmin exited 1: Unable to connect to 10.0.0.5:631"
    )
    with pytest.raises(macos.CupsError):
        macos.ensure_driverless_queue("PN_A", URI)
    # Restored with a -v-only lpadmin: no -m, so no network and the PPD is
    # untouched.
    assert fake.flat()[-1] == f"/usr/sbin/lpadmin -p PN_A -v {OTHER}"
    assert "-m" not in fake.argvs()[-1]


def test_failed_repair_does_not_delete_the_users_queue(fake):
    fake.replies[("lpstat", "-v")] = f"device for PN_A: {OTHER}\n"
    fake.replies[("everywhere",)] = macos.CupsError("boom")
    with pytest.raises(macos.CupsError):
        macos.ensure_driverless_queue("PN_A", URI)
    assert not [c for c in fake.flat() if "-x" in c]


def test_failed_create_leaves_no_carcass(fake):
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("everywhere",)] = macos.CupsError("boom")
    with pytest.raises(macos.CupsError):
        macos.ensure_driverless_queue("PN_A", URI)
    assert fake.flat()[-1] == "/usr/sbin/lpadmin -x PN_A"


def test_a_failed_cleanup_is_not_a_second_exception(fake):
    """The caller is already reporting a failure; a noisy cleanup must not
    replace the real reason with its own."""
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("everywhere",)] = macos.CupsError("the real reason")
    fake.replies[("lpadmin", "-x")] = macos.CupsError("cleanup also failed")
    with pytest.raises(macos.CupsError, match="the real reason"):
        macos.ensure_driverless_queue("PN_A", URI)


def test_a_failed_unwind_is_not_a_second_exception(fake):
    fake.replies[("lpstat", "-v")] = f"device for PN_A: {OTHER}\n"
    fake.replies[("everywhere",)] = macos.CupsError("the real reason")
    # Keyed on the OLD uri, which only the undo carries.
    fake.replies[("lpadmin", f"-v {OTHER}")] = macos.CupsError("undo failed")
    with pytest.raises(macos.CupsError, match="the real reason"):
        macos.ensure_driverless_queue("PN_A", URI)


# --------------------------------------------------------------------------- #
# provision_queues
# --------------------------------------------------------------------------- #


def _spec(name, uri=URI, tier="driverless"):
    return {"name": name, "uri": uri, "tier": tier}


def test_provision_converges_and_removes_only_the_prefix(fake):
    fake.replies[("lpstat", "-v")] = (
        f"device for PN_Keep: {URI}\n"
        f"device for PN_Stale: {URI}\n"
        f"device for UserOwn: {URI}\n"
    )
    outcomes = macos.provision_queues(None, [_spec("PN Keep")], managed_prefix="PN ")
    assert outcomes == {"PN_Keep": "unchanged", "PN_Stale": "removed"}
    assert "UserOwn" not in outcomes


def test_provision_never_removes_a_lookalike_of_the_users_own(fake):
    """'PNMyPrinter' is not one of ours. The separator in the prefix is what
    keeps it safe."""
    fake.replies[("lpstat", "-v")] = (
        f"device for PN_Keep: {URI}\ndevice for PNMyPrinter: {URI}\n"
    )
    outcomes = macos.provision_queues(None, [_spec("PN Keep")], managed_prefix="PN ")
    assert "PNMyPrinter" not in outcomes
    assert not [c for c in fake.flat() if "-x PNMyPrinter" in c]


def test_empty_prefix_disables_removal_entirely(fake):
    fake.replies[("lpstat", "-v")] = f"device for PN_Stale: {URI}\n"
    outcomes = macos.provision_queues(None, [], managed_prefix="")
    assert outcomes == {}
    assert not [c for c in fake.flat() if "-x" in c]


def test_driver_required_is_a_stated_skip(fake):
    fake.replies[("lpstat", "-v")] = ""
    outcomes = macos.provision_queues(
        None, [_spec("PN Vendor", tier="driver_required")], managed_prefix=""
    )
    assert outcomes["PN_Vendor"].startswith("error:")
    assert "driver" in outcomes["PN_Vendor"]
    assert not [c for c in fake.flat() if "lpadmin" in c]


def test_one_failing_queue_does_not_abort_the_rest(fake):
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("PN_Bad",)] = macos.CupsError("unreachable")
    outcomes = macos.provision_queues(
        None, [_spec("PN Bad"), _spec("PN Good")], managed_prefix=""
    )
    assert outcomes["PN_Bad"].startswith("error:")
    assert outcomes["PN_Good"] == "created"


def test_a_whole_pass_costs_one_enumeration(fake):
    """Otherwise a 20-printer machine pays 2 lpstat calls per printer per poll."""
    fake.replies[("lpstat", "-v")] = "".join(
        f"device for PN_{i}: {URI}\n" for i in range(20)
    )
    macos.provision_queues(
        None, [_spec(f"PN {i}") for i in range(20)], managed_prefix=""
    )
    assert len([c for c in fake.flat() if "lpstat" in c]) == 1


def test_removal_sweep_re_reads_after_creating(fake):
    """The map fetched before the convergence loop is stale for the sweep --
    queues were just created, and a stale map would remove them."""
    calls = {"n": 0}

    def lpstat(argv):
        calls["n"] += 1
        return f"device for PN_Stale: {URI}\n" if calls["n"] == 1 else (
            f"device for PN_Stale: {URI}\ndevice for PN_New: {URI}\n"
        )

    fake.replies[("lpstat", "-v")] = lpstat
    outcomes = macos.provision_queues(None, [_spec("PN New")], managed_prefix="PN ")
    assert outcomes["PN_New"] == "created"
    assert outcomes["PN_Stale"] == "removed"
    assert calls["n"] == 2


def test_a_failed_removal_is_reported_not_swallowed(fake):
    fake.replies[("lpstat", "-v")] = f"device for PN_Stale: {URI}\n"
    fake.replies[("lpadmin", "-x")] = macos.CupsError("in use")
    outcomes = macos.provision_queues(None, [], managed_prefix="PN ")
    assert outcomes["PN_Stale"].startswith("error: could not remove")


# --------------------------------------------------------------------------- #
# The live-query budget
# --------------------------------------------------------------------------- #


def test_budget_stops_a_rack_of_sleeping_printers_wedging_the_poll(fake, monkeypatch):
    """N unreachable printers cost N x cupsd's ~30s connect timeout; a dozen
    outlast the 300s poll interval and the client never completes a cycle."""
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("everywhere",)] = macos.CupsError("Unable to connect")
    monkeypatch.setattr(macos, "_QUERY_BUDGET", 0.0)
    outcomes = macos.provision_queues(
        None, [_spec(f"PN D{i}") for i in range(4)], managed_prefix=""
    )
    assert outcomes["PN_D0"].startswith("error:")
    for i in (1, 2, 3):
        assert outcomes[f"PN_D{i}"].startswith("skipped:")
        assert "budget" in outcomes[f"PN_D{i}"]


def test_every_desired_queue_appears_in_the_outcomes(fake, monkeypatch):
    """A queue silently absent from the outcomes reads to central as a queue
    that was never assigned."""
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("everywhere",)] = macos.CupsError("Unable to connect")
    monkeypatch.setattr(macos, "_QUERY_BUDGET", 0.0)
    specs = [_spec(f"PN D{i}") for i in range(5)]
    outcomes = macos.provision_queues(None, specs, managed_prefix="")
    assert sorted(outcomes) == sorted(macos.cups_queue_name(s["name"]) for s in specs)


def test_the_budget_never_starves_a_converged_queue(fake, monkeypatch):
    """Converged queues cost no network, so they must not be skipped when the
    budget is spent -- otherwise one dead printer blanks the whole fleet."""
    fake.replies[("lpstat", "-v")] = f"device for PN_Fine: {URI}\n"
    fake.replies[("everywhere",)] = macos.CupsError("Unable to connect")
    monkeypatch.setattr(macos, "_QUERY_BUDGET", 0.0)
    outcomes = macos.provision_queues(
        None, [_spec("PN Dead", uri=OTHER), _spec("PN Fine")], managed_prefix=""
    )
    assert outcomes["PN_Dead"].startswith("error:")
    assert outcomes["PN_Fine"] == "unchanged"


def test_the_budget_leaves_a_healthy_fleet_alone(fake):
    fake.replies[("lpstat", "-v")] = ""
    specs = [_spec(f"PN {i}") for i in range(10)]
    outcomes = macos.provision_queues(None, specs, managed_prefix="")
    assert set(outcomes.values()) == {"created"}


def test_the_budget_is_under_the_default_poll_interval():
    """300s is the shipped default. A pass that can exceed it never completes."""
    assert macos._QUERY_BUDGET < 300 / 2 + 1


# --------------------------------------------------------------------------- #
# Who is signed in
# --------------------------------------------------------------------------- #


def test_console_user_name_reads_dev_console(fake):
    fake.replies[("stat",)] = "alice\n"
    assert macos.console_user_name() == "alice"


@pytest.mark.parametrize("owner", ["root", "_windowserver", "", "  "])
def test_login_window_is_nobody(fake, owner):
    fake.replies[("stat",)] = owner
    assert macos.console_user_name() is None


def test_an_implausible_console_owner_is_nobody(fake):
    """Never handed to sudo, and reported rather than silently accepted."""
    fake.replies[("stat",)] = "ali ce\n"
    assert macos.console_user_name() is None


def test_console_user_reads_the_upn_from_the_directory(fake):
    fake.replies[("stat",)] = "alice\n"
    fake.replies[("dscl",)] = "NetworkUser: alice@corp.example\n"
    assert macos.console_user() == "alice@corp.example"


def test_console_user_lowercases(fake):
    fake.replies[("stat",)] = "alice\n"
    fake.replies[("dscl",)] = "NetworkUser: Alice@Corp.Example\n"
    assert macos.console_user() == "alice@corp.example"


def test_console_user_handles_the_wrapped_dscl_form(fake):
    fake.replies[("stat",)] = "alice\n"
    fake.replies[("dscl",)] = "EMailAddress:\n alice@corp.example\n"
    assert macos.console_user() == "alice@corp.example"


def test_console_user_never_fabricates_a_upn(fake):
    """A guessed UPN that happens to match another person's record would hand
    them someone else's printers."""
    fake.replies[("stat",)] = "alice\n"
    fake.replies[("dscl",)] = macos.CupsError("no such key")
    assert macos.console_user() is None


def test_console_user_is_none_at_the_login_window(fake):
    fake.replies[("stat",)] = "root\n"
    assert macos.console_user() is None
    assert not [c for c in fake.flat() if "dscl" in c]


# --------------------------------------------------------------------------- #
# The default printer
# --------------------------------------------------------------------------- #


def test_read_default_uses_lpstat_d_not_lpoptions_d(fake):
    """`lpoptions -d` with no destination is a USAGE ERROR (exit 1). The
    read-back therefore always came back None and set_default_printer always
    raised 'default did not stick' -- the feature could never once succeed."""
    fake.replies[("lpstat", "-d")] = "system default destination: PN_A\n"
    assert macos.read_default_printer("alice") == "PN_A"
    assert fake.flat() == ["/usr/bin/lpstat -d"]


def test_read_default_is_read_as_the_user(fake):
    """Root reads /etc/cups/lpoptions, the user reads ~/.cups/lpoptions, so
    reading as root is how you convince yourself a write worked."""
    fake.replies[("lpstat", "-d")] = "system default destination: PN_A\n"
    macos.read_default_printer("alice")
    assert fake.calls[0][1] == "alice"


def test_read_default_survives_a_translated_phrase(fake):
    """Belt and braces with LC_ALL=C: split on the last colon so the wording
    never matters."""
    fake.replies[("lpstat", "-d")] = "systemvoreingestelltes Ziel: PN_A\n"
    assert macos.read_default_printer("alice") == "PN_A"


def test_no_default_reads_as_none(fake):
    fake.replies[("lpstat", "-d")] = "no system default destination\n"
    assert macos.read_default_printer("alice") is None


def test_an_instance_reports_the_bare_queue(fake):
    """Somebody whose default is 'PN_A/duplex' has our queue as their default."""
    fake.replies[("lpstat", "-d")] = "system default destination: PN_A/duplex\n"
    assert macos.read_default_printer("alice") == "PN_A"


def test_read_default_swallows_a_command_failure(fake):
    fake.replies[("lpstat", "-d")] = macos.CupsError("boom")
    assert macos.read_default_printer("alice") is None


def test_set_default_writes_as_the_user_and_verifies(fake):
    fake.replies[("stat",)] = "alice\n"
    fake.replies[("lpstat", "-d")] = "system default destination: PN_A\n"
    assert macos.set_default_printer("PN A") == "PN_A"
    write = [c for c in fake.calls if "lpoptions" in c[0][0]][0]
    assert write[0] == ["/usr/bin/lpoptions", "-d", "PN_A"]
    assert write[1] == "alice"


def test_set_default_reports_failure_when_the_read_back_disagrees(fake):
    """lpoptions exits 0 having written a file, which is not evidence CUPS
    resolved the default to what we asked for."""
    fake.replies[("stat",)] = "alice\n"
    fake.replies[("lpstat", "-d")] = "system default destination: SomethingElse\n"
    with pytest.raises(DefaultPrinterError, match="did not stick"):
        macos.set_default_printer("PN A")


def test_set_default_at_the_login_window_is_a_stated_reason(fake):
    fake.replies[("stat",)] = "root\n"
    with pytest.raises(DefaultPrinterError, match="nobody is signed in"):
        macos.set_default_printer("PN A")


@pytest.mark.parametrize("name", ["", "   ", None])
def test_set_default_with_no_name(fake, name):
    with pytest.raises(DefaultPrinterError, match="no default requested"):
        macos.set_default_printer(name)


def test_set_default_sanitises_the_queue_name(fake):
    fake.replies[("stat",)] = "alice\n"
    fake.replies[("lpstat", "-d")] = "system default destination: PN_Front_Desk\n"
    assert macos.set_default_printer("PN Front Desk") == "PN_Front_Desk"


def test_manage_windows_default_is_accepted_and_ignored(fake):
    """macOS has no 'Let Windows manage my default printer'. The parameter keeps
    its Windows name so a neutral one does not imply this honours a setting it
    does not have."""
    fake.replies[("stat",)] = "alice\n"
    fake.replies[("lpstat", "-d")] = "system default destination: PN_A\n"
    for flag in (True, False):
        assert macos.set_default_printer("PN A", manage_windows_default=flag) == "PN_A"


def test_lpoptions_failure_is_a_stated_reason(fake):
    fake.replies[("stat",)] = "alice\n"
    fake.replies[("lpoptions",)] = macos.CupsError("permission denied")
    with pytest.raises(DefaultPrinterError, match="lpoptions failed"):
        macos.set_default_printer("PN A")


# --------------------------------------------------------------------------- #
# Errors carry the scheduler's own words
# --------------------------------------------------------------------------- #


def test_cups_error_quotes_stderr(monkeypatch):
    monkeypatch.setattr(
        macos.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 1, "", "lpadmin: Unable to connect to 10.0.0.5:631\n"
        ),
    )
    with pytest.raises(macos.CupsError, match="Unable to connect to 10.0.0.5:631"):
        macos._run(["/usr/sbin/lpadmin", "-p", "PN_A"])


def test_cups_error_falls_back_to_stdout(monkeypatch):
    monkeypatch.setattr(
        macos.subprocess, "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "said it on stdout", ""),
    )
    with pytest.raises(macos.CupsError, match="said it on stdout"):
        macos._run(["/usr/sbin/lpadmin"])


def test_a_timeout_names_the_command_not_sudo(monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

    monkeypatch.setattr(macos.subprocess, "run", boom)
    with pytest.raises(macos.CupsError, match=r"lpstat timed out after 15s"):
        macos._run(["/usr/bin/lpstat", "-v"], timeout=15)


def test_a_missing_binary_is_a_stated_reason(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(macos.subprocess, "run", boom)
    with pytest.raises(macos.CupsError, match="not found"):
        macos._run(["/usr/sbin/lpadmin"])


# --------------------------------------------------------------------------- #
# State directory
# --------------------------------------------------------------------------- #


@posix_paths_only
def test_state_dir_is_machine_wide(monkeypatch):
    monkeypatch.delenv("PN_STATE_DIR_BASE", raising=False)
    assert macos.default_state_dir() == (
        "/Library/Application Support/PrinterNanny"
    )


@posix_paths_only
def test_state_dir_is_overridable_for_tests(monkeypatch):
    monkeypatch.setenv("PN_STATE_DIR_BASE", "/tmp/pn")
    assert macos.default_state_dir() == "/tmp/pn/PrinterNanny"


# --------------------------------------------------------------------------- #
# One report, one naming convention
# --------------------------------------------------------------------------- #
#
# The defect: build_specs names queues for Windows, which takes them verbatim.
# CUPS rejects spaces, so this backend derives its own name and keys outcomes by
# that. Un-normalised, `outcomes.get(desired_default)` in provision() missed EVERY
# time on a Mac -- the assigned default could never be applied, and the reason
# reported was "its queue was not provisioned (skipped)" even when the queue had
# been created perfectly. Found by an end-to-end smoke; the fake backend the other
# tests use echoes the names it is handed, so nothing above the seam could see it.


def test_queue_name_is_the_cups_name():
    assert macos.queue_name is macos.cups_queue_name


def test_queue_name_is_idempotent():
    """provision() normalises, and set_default_printer sanitises again."""
    for raw in ["PN Front Desk (1)", "PN_Already_Fine", 'PN x"; rm #', "PN//x"]:
        once = macos.cups_queue_name(raw)
        assert macos.cups_queue_name(once) == once


@pytest.fixture
def macos_provision(monkeypatch, fake):
    """provision() driven through the real macOS backend."""
    from printer_nanny_agent import workstation_service as svc

    monkeypatch.setattr(svc, "_platform", lambda: macos)
    fake.replies[("lpstat", "-v")] = ""
    return svc


def _assignments(default=True, tier="driverless", driver=None):
    return {
        "manage_default_printer": True,
        "default_printer_id": 7 if default else None,
        "printers": [
            {
                "printer_id": 7,
                "is_default": default,
                "name": "Front Desk MFP",
                "ip": "10.0.0.5",
                "driver_tier": tier,
                "ipp_endpoint": "ipp://10.0.0.5:631/ipp/print",
                "driver": driver,
            }
        ],
    }


def test_the_default_is_applied_when_its_queue_was_created(macos_provision, fake):
    svc = macos_provision
    applied = []
    report = svc.provision(
        None, _assignments(), "PN ",
        default_setter=lambda name, **kw: applied.append(name) or name,
    )
    assert report.desired_default == "PN_Front_Desk_MFP_(7)"
    assert report.outcomes["PN_Front_Desk_MFP_(7)"] == "created"
    # The bug: this used to be None with reason "(skipped)".
    assert report.default_applied == "PN_Front_Desk_MFP_(7)"
    assert applied == ["PN_Front_Desk_MFP_(7)"]


def test_a_failed_queue_reports_the_real_reason_not_skipped(macos_provision, fake):
    svc = macos_provision
    fake.replies[("everywhere",)] = macos.CupsError("Unable to connect to 10.0.0.5")
    report = svc.provision(
        None, _assignments(), "PN ",
        default_setter=lambda name, **kw: pytest.fail("must not be called"),
    )
    assert report.default_applied is None
    assert "Unable to connect" in report.default_reason
    assert "(skipped)" not in report.default_reason


def test_skips_are_keyed_by_the_machines_name_too(macos_provision, fake):
    """Otherwise central shows one printer twice under two spellings.

    Uses a payload central would refuse to send -- a driver with no kind -- because
    what is under test is the KEY, and every well-formed vendor payload now
    reaches the queue path instead of the skip path.
    """
    svc = macos_provision
    report = svc.provision(
        None,
        _assignments(tier="driver_required", driver=None),
        "PN ",
    )
    assert list(report.skipped) == ["PN_Front_Desk_MFP_(7)"]


# --------------------------------------------------------------------------- #
# Vendor drivers: staging
# --------------------------------------------------------------------------- #


def _driver(kind, ref, **extra):
    d = {
        "package_id": 1,
        "driver_name": "Acme 9000",
        "inf_relpath": "",
        "sha256": "b" * 64,
        "size": 10,
        "kind": kind,
        "ref": ref,
    }
    d.update(extra)
    return d


def test_a_system_driver_is_never_downloaded(macos_provision, fake, monkeypatch):
    """It has no bytes by design -- the vendor package came from MDM and central
    recorded only the PPD path. A download would 404 forever."""
    svc = macos_provision

    class Boom:
        def download_driver(self, *a, **kw):
            pytest.fail("a system driver was downloaded")

    monkeypatch.setattr(
        macos, "_resolve_system_ppd", lambda ref: "/Library/Printers/acme.ppd"
    )
    report = svc.provision(
        None,
        _assignments(
            tier="driver_required",
            driver=_driver("system", "/Library/Printers/acme.ppd"),
        ),
        "PN ",
        client=Boom(),
    )
    assert report.outcomes == {"PN_Front_Desk_MFP_(7)": "created"}
    assert not report.skipped


def test_a_pkg_driver_is_refused_when_the_setting_is_off(macos_provision, fake, tmp_path):
    """A .pkg runs arbitrary root scripts, so it is opted into rather than
    inherited from whoever may upload."""
    svc = macos_provision
    blob, digest = _zip_bytes({"Acme.pkg": "not really a package"})
    payload = _assignments(
        tier="driver_required",
        driver=_driver("pkg", "/Library/Printers/a.ppd", sha256=digest),
    )
    payload["allow_macos_pkg_install"] = False
    report = svc.provision(
        None, payload, "PN ",
        client=_FakeDownloader(blob), cache_dir=str(tmp_path),
    )
    outcome = report.outcomes["PN_Front_Desk_MFP_(7)"]
    assert outcome.startswith("error:")
    assert "turned off" in outcome


def test_a_pkg_driver_is_installed_when_the_setting_is_on(
    macos_provision, fake, tmp_path, monkeypatch
):
    svc = macos_provision
    ran = []
    monkeypatch.setattr(macos, "install_pkg", lambda p: ran.append(p))
    monkeypatch.setattr(
        macos, "_resolve_system_ppd", lambda ref: "/Library/Printers/a.ppd"
    )
    # _ppd_present is gone: it answered "no" both to "not installed yet" and to
    # "this ref can never resolve", and merging those is what made a bad ref run
    # the root installer on every poll forever. The decision is now
    # _validate_system_ppd_path (shape only, raises on a bad ref) plus a plain
    # isfile check, so "not installed" is expressed by pointing at a file that
    # genuinely is not there.
    monkeypatch.setattr(
        macos, "_validate_system_ppd_path",
        lambda ref: str(tmp_path / "not-installed-yet.ppd"),
    )
    fake.replies[("lpoptions", "-p")] = "printer-state-reasons=none"
    blob, digest = _zip_bytes({"Acme.pkg": "not really a package"})
    payload = _assignments(
        tier="driver_required",
        driver=_driver("pkg", "/Library/Printers/a.ppd", sha256=digest),
    )
    payload["allow_macos_pkg_install"] = True
    report = svc.provision(
        None, payload, "PN ",
        client=_FakeDownloader(blob), cache_dir=str(tmp_path),
    )
    assert report.outcomes["PN_Front_Desk_MFP_(7)"] == "created"
    assert len(ran) == 1 and ran[0].endswith("Acme.pkg")


def test_a_tampered_package_never_reaches_the_installer(
    macos_provision, fake, tmp_path, monkeypatch
):
    """The digest is checked BEFORE the archive is opened, so altered bytes never
    become a root install."""
    monkeypatch.setattr(
        macos, "install_pkg", lambda p: pytest.fail("installed a tampered package")
    )
    svc = macos_provision
    blob, _digest = _zip_bytes({"Acme.pkg": "x"})
    payload = _assignments(
        tier="driver_required",
        driver=_driver("pkg", "/Library/Printers/a.ppd", sha256="c" * 64),
    )
    payload["allow_macos_pkg_install"] = True
    report = svc.provision(
        None, payload, "PN ",
        client=_FakeDownloader(blob), cache_dir=str(tmp_path),
    )
    assert "checksum mismatch" in report.skipped["PN_Front_Desk_MFP_(7)"]


@posix_paths_only
def test_a_ppd_driver_binds_the_ppd_from_the_package(
    macos_provision, fake, tmp_path
):
    """The whole shared path for real: download, verify, unpack, resolve the PPD
    inside the archive, bind it, verify cupsd is happy."""
    svc = macos_provision
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("lpoptions", "-p")] = "printer-state-reasons=none"
    blob, digest = _zip_bytes({"drivers/acme.ppd": '*PPD-Adobe: "4.3"\n'})
    report = svc.provision(
        None,
        _assignments(
            tier="driver_required",
            driver=_driver("ppd", "drivers/acme.ppd", sha256=digest),
        ),
        "PN ",
        client=_FakeDownloader(blob),
        cache_dir=str(tmp_path),
    )
    assert report.outcomes["PN_Front_Desk_MFP_(7)"] == "created"
    bind = [c for c in fake.flat() if "-P " in c][0]
    assert bind.endswith("drivers/acme.ppd -E")


def _zip_bytes(members):
    """A deterministic zip and its digest, so the SHARED fetch path -- download,
    re-verify the checksum, refuse escaping entries, unpack -- runs for real
    rather than being stubbed out. A mismatched digest here is indistinguishable
    from a tampered package, which is the point."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    blob = buf.getvalue()
    return blob, hashlib.sha256(blob).hexdigest()


class _FakeDownloader:
    def __init__(self, blob):
        self.blob = blob
        self.calls = 0

    def download_driver(self, package_id, dest):
        self.calls += 1
        with open(dest, "wb") as fp:
            fp.write(self.blob)


def test_an_unknown_driver_kind_is_refused_with_a_reason(
    macos_provision, fake, tmp_path
):
    svc = macos_provision
    blob, digest = _zip_bytes({"a.ppd": "x"})
    report = svc.provision(
        None,
        _assignments(tier="driver_required", driver=_driver("dmg", "x", sha256=digest)),
        "PN ",
        client=_FakeDownloader(blob),
        cache_dir=str(tmp_path),
    )
    outcome = report.outcomes["PN_Front_Desk_MFP_(7)"]
    assert outcome.startswith("error:")
    assert "unknown driver kind" in outcome


def test_a_ppd_that_escapes_the_package_is_refused(tmp_path):
    with pytest.raises(macos.DriverStagingError, match="escapes the package"):
        macos._resolve_in_package(str(tmp_path), "../../etc/cups/cupsd.conf")


def test_a_ppd_missing_from_the_package_is_refused(tmp_path):
    with pytest.raises(macos.DriverStagingError, match="not found in the package"):
        macos._resolve_in_package(str(tmp_path), "nope.ppd")


def test_an_empty_ref_is_refused(tmp_path):
    with pytest.raises(macos.DriverStagingError, match="does not say which file"):
        macos._resolve_in_package(str(tmp_path), "")


@pytest.mark.parametrize(
    "ref",
    [
        "/etc/shadow",
        "/tmp/evil.ppd",
        "relative/path.ppd",
        "",
        "/Users/alice/.ssh/id_rsa",
    ],
)
def test_a_system_ppd_outside_the_allowed_roots_is_refused(ref):
    """`lpadmin -P` copies whatever it is handed into /etc/cups/ppd as root, so an
    unconstrained operator-typed path is "read any root-readable file"."""
    with pytest.raises(macos.DriverStagingError):
        macos._resolve_system_ppd(ref)


def test_a_symlink_out_of_an_allowed_root_is_refused(tmp_path, monkeypatch):
    """realpath BEFORE the root check, or a symlink inside an allowed directory
    smuggles an arbitrary file through."""
    secret = tmp_path / "secret"
    secret.write_text("x")
    link = tmp_path / "link.ppd"
    try:
        link.symlink_to(secret)
    except OSError as exc:
        # Windows needs admin or Developer Mode to create one. The guard being
        # tested is real everywhere; only the rig for it is unavailable here.
        pytest.skip(f"cannot create a symlink in this environment: {exc}")
    monkeypatch.setattr(macos, "_SYSTEM_PPD_ROOTS", (str(tmp_path) + "/link",))
    with pytest.raises(macos.DriverStagingError, match="not under a directory"):
        macos._resolve_system_ppd(str(link))


def test_the_sole_installer_rule_refuses_ambiguity(tmp_path):
    (tmp_path / "one.pkg").write_text("")
    (tmp_path / "two.pkg").write_text("")
    with pytest.raises(macos.DriverStagingError, match="refusing to guess"):
        macos._sole_installer_in(str(tmp_path))


def test_the_sole_installer_rule_refuses_none(tmp_path):
    with pytest.raises(macos.DriverStagingError, match="no .pkg found"):
        macos._sole_installer_in(str(tmp_path))


def test_the_sole_installer_is_found_in_a_subdirectory(tmp_path):
    sub = tmp_path / "Acme" / "inner"
    sub.mkdir(parents=True)
    target = sub / "Acme.pkg"
    target.write_text("")
    assert macos._sole_installer_in(str(tmp_path)) == str(target)


def test_a_pkg_already_installed_is_not_reinstalled(monkeypatch, tmp_path):
    """Otherwise a vendor package is reinstalled as root every five minutes."""
    ran = []
    monkeypatch.setattr(macos, "install_pkg", lambda p: ran.append(p))
    monkeypatch.setattr(macos, "_resolve_system_ppd", lambda ref: str(tmp_path / "a.ppd"))
    # The shape check runs first now and is OS-specific (absolute POSIX path
    # under a macOS PPD root), so it is stubbed here for the same reason
    # _resolve_system_ppd is: this test is about the install decision.
    monkeypatch.setattr(macos, "_validate_system_ppd_path",
                        lambda ref: str(tmp_path / "a.ppd"))
    (tmp_path / "a.ppd").write_text("")
    got = macos.stage_driver({
        "driver_kind": "pkg",
        "driver_ref": str(tmp_path / "a.ppd"),
        "unpacked": str(tmp_path),
        "allow_pkg_install": True,
    })
    assert got == str(tmp_path / "a.ppd")
    assert ran == []


def test_a_pkg_not_yet_installed_is_installed_once(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr(macos, "install_pkg", lambda p: ran.append(p))
    (tmp_path / "Acme.pkg").write_text("")
    calls = {"n": 0}

    def resolve(ref):
        calls["n"] += 1
        return "/Library/Printers/acme.ppd"

    monkeypatch.setattr(macos, "_resolve_system_ppd", resolve)
    # Absence is now expressed by the path simply not existing, rather than by
    # the resolver raising. That distinction is the fix: a resolver that raised
    # meant BOTH "not installed yet" and "this ref can never resolve", and the
    # second one used to run the root installer on every poll, forever.
    monkeypatch.setattr(macos, "_validate_system_ppd_path",
                        lambda ref: str(tmp_path / "not-there-yet.ppd"))
    got = macos.stage_driver({
        "driver_kind": "pkg",
        "driver_ref": "/Library/Printers/acme.ppd",
        "unpacked": str(tmp_path),
        "allow_pkg_install": True,
    })
    assert got == "/Library/Printers/acme.ppd"
    assert ran == [str(tmp_path / "Acme.pkg")]


# --------------------------------------------------------------------------- #
# Vendor drivers: the bind must be verified, not assumed
# --------------------------------------------------------------------------- #


def test_a_vendor_bind_is_verified_against_cupsds_own_verdict(fake):
    """lpadmin exits 0 for a PPD whose filters are absent, producing a queue that
    is created, listed, converged and unable to print. cupsd says so in
    printer-state-reasons, so that is what decides."""
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("lpoptions", "-p")] = "printer-state-reasons=none copies=1"
    assert macos.ensure_vendor_queue("PN_A", URI, "/Library/Printers/a.ppd") == "created"
    assert f"/usr/sbin/lpadmin -p PN_A -v {URI} -P /Library/Printers/a.ppd -E" in fake.flat()


def test_a_bad_ppd_never_touches_the_real_queue_at_all(fake):
    """The strongest form of "a failed change changes nothing".

    Binding first and unwinding after CANNOT work here, and the live-scheduler
    check proved it: `lpadmin -P` exits 0 having already replaced the PPD, so
    restoring the URI leaves the queue on its old address with the new broken
    PPD -- right URI, so it converges forever; broken PPD, so it cannot print.
    The tier-1 failure, reintroduced by an incomplete unwind. So the PPD is tried
    on a throwaway queue and the real one is only touched once it is proven.
    """
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("lpoptions", "-p")] = (
        "printer-state-reasons=cups-missing-filter-warning copies=1"
    )
    with pytest.raises(macos.DriverStagingError, match="missing filter"):
        macos.ensure_vendor_queue("PN_A", URI, "/Library/Printers/a.ppd")
    # Only the probe queue was ever created, and it was removed.
    probe = macos.ppd_probe_name("/Library/Printers/a.ppd")
    assert f"/usr/sbin/lpadmin -x {probe}" in fake.flat()
    assert not [c for c in fake.flat() if " -p PN_A " in c]


def test_a_bad_ppd_on_a_REPAIR_leaves_the_working_queue_alone(fake):
    """A user with a working queue keeps it. No URI change, no re-bind, no
    removal -- there is nothing to unwind because nothing was done."""
    fake.replies[("lpstat", "-v")] = f"device for PN_A: {OTHER}\n"
    fake.replies[("lpoptions", "-p")] = (
        "printer-state-reasons=cups-missing-filter-warning"
    )
    with pytest.raises(macos.DriverStagingError):
        macos.ensure_vendor_queue("PN_A", URI, "/Library/Printers/a.ppd")
    assert not [c for c in fake.flat() if " -p PN_A " in c]
    assert not [c for c in fake.flat() if "-x PN_A" in c]


def test_the_probe_queue_is_removed_even_when_the_bind_fails(fake):
    """Otherwise a dead probe queue accumulates in the user's printer list."""
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("lpadmin", "-P")] = macos.CupsError("lpadmin exited 1: bad ppd")
    assert macos.ppd_is_usable("/Library/Printers/a.ppd") is False
    probe = macos.ppd_probe_name("/Library/Printers/a.ppd")
    assert f"/usr/sbin/lpadmin -x {probe}" in fake.flat()


def test_the_probe_queue_carries_the_managed_prefix(fake):
    """So a probe stranded by a kill is something reconcile will clean up, not an
    orphan nobody owns."""
    probe = macos.ppd_probe_name("/Library/Printers/Acme 9000.ppd")
    assert probe.startswith(macos.cups_queue_prefix("PN "))
    assert probe.endswith("_pnprobe")
    assert len(probe) <= 127
    # Two different PPDs must not collide on one probe name.
    assert probe != macos.ppd_probe_name("/Library/Printers/Other.ppd")


def test_the_probe_never_contacts_the_real_printer(fake):
    """It exists to test a local file. Pointing it at the device would be a
    connection attempt for no reason -- and would fail for a sleeping printer."""
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("lpoptions", "-p")] = "printer-state-reasons=none"
    macos.ensure_vendor_queue("PN_A", "ipp://10.0.0.5:631/ipp/print", "/x/a.ppd")
    probe_bind = [c for c in fake.flat() if "_pnprobe" in c and "-P" in c][0]
    assert "10.0.0.5" not in probe_bind
    assert "127.0.0.1" in probe_bind


def test_an_unreadable_state_is_treated_as_a_failure(fake):
    """If we cannot tell whether the filters resolve, we must not claim they do --
    the opposite default reports a queue as working on a command that did not
    run."""
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("lpoptions", "-p")] = macos.CupsError("boom")
    with pytest.raises(macos.DriverStagingError):
        macos.ensure_vendor_queue("PN_A", URI, "/Library/Printers/a.ppd")


def test_a_converged_vendor_queue_costs_nothing(fake):
    fake.replies[("lpstat", "-v")] = f"device for PN_A: {URI}\n"
    assert macos.ensure_vendor_queue("PN_A", URI, "/Library/Printers/a.ppd") == "unchanged"
    assert not [c for c in fake.flat() if "lpadmin" in c]
    # Not re-verified either: the filters were checked when it was created, and a
    # per-poll re-check spends a call to re-answer a question that only changes
    # when somebody uninstalls the driver.
    assert not [c for c in fake.flat() if "lpoptions" in c]


def test_a_vendor_queue_is_unshared(fake):
    fake.replies[("lpstat", "-v")] = ""
    fake.replies[("lpoptions", "-p")] = "printer-state-reasons=none"
    macos.ensure_vendor_queue("PN_A", URI, "/Library/Printers/a.ppd")
    assert "/usr/sbin/lpadmin -p PN_A -o printer-is-shared=false" in fake.flat()


def test_install_pkg_gets_a_longer_timeout_than_a_cups_call(fake):
    macos.install_pkg("/tmp/a.pkg")
    argv, _user, timeout = fake.calls[0]
    assert argv == ["/usr/sbin/installer", "-pkg", "/tmp/a.pkg", "-target", "/"]
    assert timeout > macos._TIMEOUT


def test_a_mac_builds_no_powershell_runner(monkeypatch):
    from printer_nanny_agent import workstation_service as svc

    monkeypatch.setattr(svc, "_platform", lambda: macos)
    assert svc._platform_runner() is None


def test_windows_still_builds_one(monkeypatch):
    from printer_nanny_agent import workstation as ws
    from printer_nanny_agent import workstation_service as svc
    from printer_nanny_agent.platforms import windows

    monkeypatch.setattr(svc, "_platform", lambda: windows)
    assert isinstance(svc._platform_runner(), ws.PowerShellRunner)


# --------------------------------------------------------------------------- #
# The seam
# --------------------------------------------------------------------------- #


def test_the_backend_satisfies_the_seam():
    """Whatever the orchestrator calls through _platform() must exist here with
    a compatible signature, or a Mac fails at runtime on a path no Linux test
    exercises."""
    from printer_nanny_agent.platforms import unsupported, windows

    for attr in (
        "NAME", "SUPPORTS_VENDOR_DRIVERS", "queue_name", "default_state_dir",
        "console_user", "provision_queues", "set_default_printer",
    ):
        assert hasattr(macos, attr), attr
        assert hasattr(windows, attr), attr
        assert hasattr(unsupported, attr), attr


def test_platforms_current_selects_macos_on_darwin(monkeypatch):
    from printer_nanny_agent import platforms

    monkeypatch.setattr(platforms.sys, "platform", "darwin")
    assert platforms.current() is macos


def test_a_pkg_with_an_unresolvable_ref_never_runs_the_installer(monkeypatch, tmp_path):
    """The loop, and why it was one.

    `_ppd_present` returned False for a ref that could NEVER resolve -- a
    relative path, or one outside the directories PPDs live in -- so the root
    installer ran to discover a configuration error, the strict check raised
    immediately afterwards, and nothing recorded that the install had happened.
    Next poll: identical. Arbitrary vendor pre/postinstall scripts as root, every
    five minutes, on every Mac in the client, from one operator typo.
    """
    ran = []
    monkeypatch.setattr(macos, "install_pkg", lambda p: ran.append(p))
    (tmp_path / "Acme.pkg").write_text("")

    with pytest.raises(macos.DriverStagingError, match="absolute"):
        macos.stage_driver({
            "driver_kind": "pkg",
            "driver_ref": "Library/Printers/acme.ppd",  # relative
            "unpacked": str(tmp_path),
            "allow_pkg_install": True,
        })
    assert ran == [], "a ref that cannot resolve must not reach installer -pkg"


def test_a_pkg_that_installed_but_left_no_ppd_is_not_reinstalled(monkeypatch, tmp_path):
    """The other half: the install ran and the PPD still is not where the
    operator said. Re-running as root will not change that, so it is stated once
    rather than retried every poll."""
    ran = []
    monkeypatch.setattr(macos, "install_pkg", lambda p: ran.append(p))
    monkeypatch.setattr(macos, "_validate_system_ppd_path",
                        lambda ref: str(tmp_path / "never-appears.ppd"))
    (tmp_path / "Acme.pkg").write_text("")
    spec = {
        "driver_kind": "pkg",
        "driver_ref": "/Library/Printers/acme.ppd",
        "unpacked": str(tmp_path),
        "allow_pkg_install": True,
    }

    with pytest.raises(macos.DriverStagingError):
        macos.stage_driver(spec)
    assert len(ran) == 1

    # Second poll: the marker says we already did this to this machine.
    with pytest.raises(macos.DriverStagingError, match="no PPD appeared"):
        macos.stage_driver(spec)
    assert len(ran) == 1, "the root installer ran a second time"
