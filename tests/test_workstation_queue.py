"""Windows queue provisioning: convergence, idempotence, and shell safety.

Runs on any platform -- the PowerShell calls sit behind a seam
(``PowerShellRunner``) that a fake replaces here, so everything above it is
exercised without Windows. The seam itself is covered by
tests/windows/test_queue_integration.py against a real spooler on a
windows-latest CI runner.

The safety property under test is stronger than "we escape correctly": no
caller value is ever placed into the PowerShell text at all. Values travel as
environment variables, so quoting cannot be broken out of. The tests assert
that invariant directly rather than trying to enumerate hostile inputs.
"""

from __future__ import annotations

import base64
import subprocess

import pytest

from printer_nanny_agent import workstation as ws


# --------------------- the port's transport, not its label ------------------ #
#
# Observed on Windows 11 26200 against a real Brother, on a queue built by
# Add-Printer -IppURL: Description = "IPP Port" while PortMonitor = "WSD Port
# Monitor". Everything used to assert against the description, so the
# "not on the Standard TCP/IP monitor" disproof was reading a label.


def test_port_transport_prefers_the_monitor_over_the_description():
    detail = {"description": "IPP Port", "monitor": "WSD Port Monitor"}
    assert ws.port_transport(detail) == "wsd port monitor"


def test_port_transport_falls_back_to_the_description():
    """Older callers, and ports where Windows reports no monitor."""
    assert ws.port_transport({"description": "Standard TCP/IP Port"}) == (
        "standard tcp/ip port"
    )
    assert ws.STANDARD_TCP_MONITOR in ws.port_transport(
        {"description": "Standard TCP/IP Port"}
    )


def test_a_standard_tcp_monitor_is_caught_even_when_the_label_says_ipp():
    """The exact shape that would otherwise slip through: a reassuring
    description over a monitor that speaks only RAW/LPR."""
    detail = {"description": "IPP Port", "monitor": "Standard TCP/IP Port"}
    assert ws.STANDARD_TCP_MONITOR in ws.port_transport(detail)


def test_the_port_detail_script_reads_all_three_fields():
    for field in ("Description", "PrinterHostAddress", "PortMonitor"):
        assert field in ws._SCRIPT_PORT_DETAIL, (
            f"{field} is needed to tell a working queue from a converged-but-dead one"
        )


class FakeRunner:
    """A tiny stateful fake spooler.

    Modelling state rather than replaying canned outputs is what makes the
    convergence and idempotence tests mean anything: a positional output queue
    would let "created, created, created" pass as idempotent because the fake
    never learned that the first call created something.
    """

    def __init__(self, queues=None, drivers=None, ippurl=True, ipp_reachable=True):
        self.calls = []                              # (script, variables)
        self.queues = dict(queues or {})             # name -> (port, driver)
        self.ports = {}                              # name -> monitor description
        # The IPP class driver is INBOX -- present on every supported Windows --
        # so a fake that starts without it models a machine that does not exist.
        # Pass drivers=[] explicitly to test the genuinely-missing case.
        self.drivers = set(drivers) if drivers is not None else {ws.IPP_CLASS_DRIVER}
        # Whether this pretend Windows has Add-Printer -IppURL at all (Server
        # 2019 does not), and whether the pretend printer answers its IPP query.
        self.ippurl = ippurl
        self.ipp_reachable = ipp_reachable

    def run(self, script, variables=None):
        v = dict(variables or {})
        self.calls.append((script, v))

        if "ContainsKey('IppURL')" in script:
            return "YES" if self.ippurl else "NO"

        if "Add-Printer -Name $env:PN_NAME -IppURL" in script:
            # Windows performs the discovery and picks BOTH the port name and
            # the driver. The fake mirrors that: it invents a port we did not
            # choose, which is what makes the "cannot predict the port name"
            # property testable at all.
            if not self.ipp_reachable:
                raise ws.PowerShellError(
                    "Add-Printer failed", 1, "", "The request timed out."
                )
            name = v["name"]
            if name not in self.queues:
                port = "IPP_" + v["ippurl"].replace("://", "_").replace("/", "_")
                self.queues[name] = (port, ws.IPP_CLASS_DRIVER)
                self.ports[port] = "Internet Port"
            port, driver = self.queues[name]
            return "OK|{}|{}".format(port, driver)

        if "Get-PrinterPort -Name $env:PN_PORT" in script and "Description" in script:
            desc = self.ports.get(v.get("port"))
            if desc is None:
                return "ABSENT"
            return "PRESENT|{}|{}".format(desc, "")

        # BEFORE the Get-Printer branch: _SCRIPT_REMOVE_QUEUE contains BOTH
        # "Get-Printer -Name" (its existence check) and "Remove-Printer". Matched
        # the other way round the fake answers the existence check and never
        # removes anything -- so a removal test passes while nothing is removed.
        if "Remove-Printer" in script:
            self.queues.pop(v.get("name"), None)
            return "OK"

        if "Get-Printer -Name" in script:
            row = self.queues.get(v.get("name"))
            return "ABSENT" if row is None else "PRESENT|{}|{}".format(*row)

        if "Get-Printer |" in script:
            return "\n".join(
                "{}|{}|{}".format(n, p, d) for n, (p, d) in sorted(self.queues.items())
            )

        if "Get-PrinterDriver" in script:
            return "PRESENT" if v.get("driver") in self.drivers else "ABSENT"

        if "Add-PrinterPort" in script:
            # The only port Add-PrinterPort can make is a Standard TCP/IP one --
            # that is the whole reason tier 1 no longer goes anywhere near it.
            self.ports[v["port"]] = "Standard TCP/IP Port"
            return "OK"

        if "pnputil" in script:
            self.drivers.add(v["driver"])
            return "OK"

        if "Add-Printer " in script:
            if v["name"] in self.queues:
                raise ws.PowerShellError("printer already exists", 1, "", "")
            self.queues[v["name"]] = (v["port"], v["driver"])
            return "OK"

        if "Set-Printer -Name $env:PN_NAME -DriverName" in script:
            self.queues[v["name"]] = (v["port"], v["driver"])
            return "OK"

        return "OK"

    def scripts_containing(self, needle):
        return [s for s, _ in self.calls if needle in s]

    def vars_for(self, needle):
        return [v for s, v in self.calls if needle in s]


# --- shell safety -----------------------------------------------------------

HOSTILE = [
    'x"; Remove-Item -Recurse C:\\ #',
    "'; Stop-Service spooler; '",
    "$(Invoke-WebRequest evil.example)",
    "`nWrite-Host pwned",
    "name with spaces & ampersand | pipe",
    "%PN_NAME%",
    "../../etc/passwd",
]


@pytest.mark.parametrize("nasty", HOSTILE)
def test_hostile_values_never_enter_the_script_text(nasty):
    """The invariant that makes injection moot rather than merely handled.

    Printer names arrive from devices on customer LANs and from operator
    free-text. If any of them reached the script body, escaping would have to be
    perfect forever. Instead the script is a constant and the value is an
    environment variable.
    """
    runner = FakeRunner()
    ws.ensure_driverless_queue(runner, nasty, "ipp://10.0.0.5:631/ipp/print")
    for script, variables in runner.calls:
        assert nasty not in script, "caller value leaked into PowerShell text"
        # It must still have been *delivered*, just by the safe channel.
    assert any(nasty in v for vars_ in runner.vars_for("Add-Printer") for v in vars_.values())


def test_scripts_contain_no_format_placeholders():
    """A future edit that reintroduces interpolation should be obvious."""
    for name in dir(ws):
        if not name.startswith("_SCRIPT_"):
            continue
        script = getattr(ws, name)
        assert "{}" not in script, name
        assert "%s" not in script, name
        assert "$(" not in script or "Invoke" not in script, name


def test_wrapper_turns_a_thrown_error_into_a_failure():
    """`powershell -Command -` exits 0 even when a cmdlet throws.

    Trusting the process exit code meant a failed Add-Printer was reported as
    success and the caller recorded "created" for a queue that did not exist.
    The Windows CI job caught it; a fake runner never could. The wrapper is what
    closes it, so its shape is pinned here.
    """
    wrapped = ws.wrap_script("Add-Printer -Name $env:PN_NAME")
    assert "try {" in wrapped and "catch {" in wrapped
    assert "exit 1" in wrapped
    assert ws._FAILURE_MARKER in wrapped
    # A native command that fails does not throw, so it needs its own check.
    assert "$LASTEXITCODE" in wrapped
    assert "Add-Printer -Name $env:PN_NAME" in wrapped


def test_wrapper_does_not_interpolate_caller_data():
    """The wrapper composes our constants only; values still travel by env."""
    for name in dir(ws):
        if name.startswith("_SCRIPT_"):
            wrapped = ws.wrap_script(getattr(ws, name))
            assert "{}" not in wrapped and "%s" not in wrapped


def _script_constants():
    return sorted(n for n in dir(ws) if n.startswith("_SCRIPT_"))


@pytest.mark.parametrize("name", _script_constants())
def test_encode_command_round_trips_every_script(name):
    """Byte-for-byte survival, newlines included -- what stdin destroyed.

    ``powershell -Command -`` parses stdin **line by line**, so a construct that
    spans lines is incomplete on its first line and the script silently produces
    nothing. Two failures on the Windows runner were this: the try/catch wrapper
    made *every* call return empty output, and before that ``_SCRIPT_ADD_TCP_PORT``
    lost its ``if {}`` body, so the port was never created and the Add-Printer
    that followed had no port to bind. -EncodedCommand sidesteps command-line
    parsing entirely, so the property to pin is exact round-trip.
    """
    script = ws.wrap_script(getattr(ws, name))
    decoded = base64.b64decode(ws.encode_command(script)).decode("utf-16-le")
    assert decoded == script
    assert decoded.count("\n") == script.count("\n") > 1


def test_some_scripts_span_lines_so_stdin_delivery_can_never_come_back():
    """Pin the reason -EncodedCommand is not merely a stylistic choice.

    If these bodies ever became single-line, the hazard would look theoretical
    and someone would reasonably "simplify" the runner back to stdin. They are
    not single-line, and this says which ones and why it matters.
    """
    spanning = [
        n for n in _script_constants()
        if any(line.rstrip().endswith("{") for line in getattr(ws, n).splitlines())
    ]
    assert spanning, "no multi-line blocks left -- re-read the runner's comment before changing it"
    assert "_SCRIPT_ADD_TCP_PORT" in spanning


def test_run_passes_the_script_as_one_encoded_argv_element(monkeypatch):
    """Pin the delivery mechanism: encoded argv, no shell, no stdin.

    Two regressions this catches. Reverting to ``-Command -`` (which silently
    truncates multi-line scripts), and ever passing the script as plain argv
    text where a command-line parser would get a say in what it means.
    """
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(ws.subprocess, "run", fake_run)
    runner = ws.PowerShellRunner(executable="powershell")
    assert runner.run(ws._SCRIPT_QUEUE_EXISTS, {"name": "Lobby"}) == "ok"

    argv = seen["argv"]
    assert argv[-2] == "-EncodedCommand"
    assert "-Command" not in argv, "-Command - truncates multi-line scripts"
    assert seen["kwargs"]["shell"] is False
    assert "input" not in seen["kwargs"], "the script no longer travels on stdin"

    # The one argv element carries the wrapped script intact...
    payload = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert payload == ws.wrap_script(ws._SCRIPT_QUEUE_EXISTS)
    # ...and the caller's value is nowhere in it -- it went by environment.
    assert "Lobby" not in payload
    assert seen["kwargs"]["env"]["PN_NAME"] == "Lobby"


def test_run_raises_when_the_script_reports_failure(monkeypatch):
    """Exit code 0 with the marker present is still a failure.

    This is the real Windows behaviour the fake cannot reproduce: PowerShell
    exits 0 even when a cmdlet throws, so the marker is what makes a failed
    Add-Printer stop being reported as a created queue.
    """
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "junk\n" + ws._FAILURE_MARKER + "\n", "boom")

    monkeypatch.setattr(ws.subprocess, "run", fake_run)
    with pytest.raises(ws.PowerShellError) as exc:
        ws.PowerShellRunner(executable="powershell").run(ws._SCRIPT_QUEUE_EXISTS)
    assert exc.value.stderr == "boom"
    assert ws._FAILURE_MARKER not in exc.value.stdout


def test_build_env_namespaces_and_stringifies():
    env = ws.build_env({"name": "Lobby", "port": 631})
    assert env == {"PN_NAME": "Lobby", "PN_PORT": "631"}


def test_build_env_drops_none_and_strips_nul():
    env = ws.build_env({"a": None, "b": "x\x00y"})
    assert "PN_A" not in env
    assert env["PN_B"] == "xy", "NUL cannot appear in an environment value"


# --- port naming ------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("ipp://10.0.0.5:631/ipp/print", "PN_10.0.0.5_631_ipp_print"),
        ("ipps://10.0.0.5:631/ipp/print", "PN_10.0.0.5_631_ipp_print"),
        ("http://10.0.0.5:631/ipp/print", "PN_10.0.0.5_631_ipp_print"),
        ("10.0.0.5", "PN_10.0.0.5"),
    ],
)
def test_port_name_is_derived_and_stable(value, expected):
    assert ws.port_name_for(value) == expected


def test_port_name_is_deterministic():
    """Re-runs must find the port they made, not accumulate one per run.

    Forty dead ports on a workstation is the classic symptom of naive
    provisioning; a deterministic name is what prevents it.
    """
    a = ws.port_name_for("ipp://10.0.0.5:631/ipp/print")
    b = ws.port_name_for("ipp://10.0.0.5:631/ipp/print")
    assert a == b


# --- parsing ----------------------------------------------------------------


def test_parse_queue_state():
    absent = ws.parse_queue_state("ABSENT")
    assert not absent.present

    present = ws.parse_queue_state("PRESENT|PN_10.0.0.5|Microsoft IPP Class Driver")
    assert present.present
    assert present.port == "PN_10.0.0.5"
    assert present.driver == "Microsoft IPP Class Driver"
    assert present.matches("PN_10.0.0.5", "Microsoft IPP Class Driver")
    assert not present.matches("PN_other", "Microsoft IPP Class Driver")


@pytest.mark.parametrize("junk", ["", "   ", "unexpected output", "PRESENT"])
def test_parse_queue_state_tolerates_junk(junk):
    state = ws.parse_queue_state(junk)
    assert isinstance(state, ws.QueueState)


def test_parse_queue_list_skips_malformed_lines():
    rows = ws.parse_queue_list("A|p1|d1\n\ngarbage\nB|p2|d2\n")
    assert [r["name"] for r in rows] == ["A", "B"]


# --- convergence ------------------------------------------------------------


def test_absent_queue_is_created():
    runner = FakeRunner()
    assert ws.ensure_driverless_queue(runner, "Lobby", "ipp://10.0.0.5:631/ipp/print") == "created"
    assert runner.scripts_containing("Add-Printer ")
    assert not runner.scripts_containing("Set-Printer -Name $env:PN_NAME -DriverName")


def test_a_converged_queue_costs_no_network_round_trip():
    """An already-good queue must NOT re-run Add-Printer -IppURL.

    That cmdlet performs a live IPP query. This runs on every assignment change,
    service start and poll, so re-querying would mean a round trip per printer
    per poll -- and would make a sleeping printer fail a queue that already
    works. Convergence is decided from local state alone.
    """
    runner = FakeRunner(
        {"Lobby": ("IPP_ipp_10.0.0.5_631_ipp_print", ws.IPP_CLASS_DRIVER)}
    )
    runner.ports["IPP_ipp_10.0.0.5_631_ipp_print"] = "Internet Port"
    assert ws.ensure_driverless_queue(runner, "Lobby", "ipp://10.0.0.5:631/ipp/print") == "unchanged"
    assert not runner.scripts_containing("-IppURL")
    assert not runner.scripts_containing("Add-PrinterPort")


def test_a_queue_on_a_raw_tcp_port_is_rebuilt():
    """The migration case: queues the OLD broken code left behind.

    The previous implementation bound the IPP class driver to a Standard TCP/IP
    (RAW 9100) port, producing a queue that looks provisioned everywhere and
    cannot print. Those exist on real machines now. There is no Set-Printer that
    repairs one -- for tier 1 Windows owns the port -- so it is removed and
    rebuilt through discovery.
    """
    runner = FakeRunner({"Lobby": ("PN_10.0.0.5_631_ipp_print", ws.IPP_CLASS_DRIVER)})
    runner.ports["PN_10.0.0.5_631_ipp_print"] = "Standard TCP/IP Port"

    assert ws.ensure_driverless_queue(runner, "Lobby", "ipp://10.0.0.5:631/ipp/print") == "updated"
    assert runner.scripts_containing("Remove-Printer")
    assert runner.scripts_containing("-IppURL")
    # And what it ends up on is a port Windows chose, not one we derived.
    port, driver = runner.queues["Lobby"]
    assert not port.startswith("PN_"), "a PN_ port means the derived-name bug came back"
    assert driver == ws.IPP_CLASS_DRIVER


def test_a_queue_with_the_wrong_driver_is_rebuilt():
    runner = FakeRunner({"Lobby": ("PN_old_address", "Some Old Driver")})
    runner.ports["PN_old_address"] = "Standard TCP/IP Port"
    assert ws.ensure_driverless_queue(runner, "Lobby", "ipp://10.0.0.9:631/ipp/print") == "updated"
    assert runner.scripts_containing("Remove-Printer")


def test_repeated_runs_are_idempotent():
    """The client re-runs constantly; a second pass must be a no-op."""
    runner = FakeRunner()
    first = ws.ensure_driverless_queue(runner, "Lobby", "ipp://10.0.0.5:631/ipp/print")
    second = ws.ensure_driverless_queue(runner, "Lobby", "ipp://10.0.0.5:631/ipp/print")
    third = ws.ensure_driverless_queue(runner, "Lobby", "ipp://10.0.0.5:631/ipp/print")
    assert (first, second, third) == ("created", "unchanged", "unchanged")


def test_driverless_hands_windows_the_url_and_installs_nothing():
    """Tier 1 no longer names a driver or a port -- Windows picks both.

    -IppURL sits in a parameter set disjoint from -DriverName/-PortName, so
    passing either alongside it does not get ignored: PowerShell fails to bind
    the parameters at all. What we send is the URL and nothing else.
    """
    runner = FakeRunner()
    ws.ensure_driverless_queue(runner, "Lobby", "ipp://10.0.0.5:631/ipp/print")

    add_vars = runner.vars_for("-IppURL")[0]
    assert add_vars["ippurl"] == "ipp://10.0.0.5:631/ipp/print"
    assert "driver" not in add_vars, "-IppURL cannot be combined with -DriverName"
    assert "port" not in add_vars, "-IppURL cannot be combined with -PortName"

    assert not runner.scripts_containing("pnputil"), "tier 1 must never stage a driver"
    assert not runner.scripts_containing("Add-PrinterPort"), (
        "Add-PrinterPort can only make a RAW/LPR port, which cannot carry IPP"
    )


def test_comment_and_location_are_only_set_when_supplied():
    runner = FakeRunner()
    ws.ensure_driverless_queue(runner, "Lobby", "ipp://10.0.0.5:631/ipp/print")
    assert not runner.scripts_containing("-Comment")

    runner = FakeRunner()
    ws.ensure_driverless_queue(
        runner, "Lobby", "ipp://10.0.0.5:631/ipp/print", comment="Floor 2", location="HQ"
    )
    assert runner.scripts_containing("-Comment")


# --- vendor driver path -----------------------------------------------------


def test_vendor_driver_is_staged_only_when_missing():
    runner = FakeRunner()  # driver not registered
    ws.ensure_vendor_queue(runner, "Lab", "10.0.0.7", "Acme PCL6", inf_path="C:\\d\\acme.inf")
    assert runner.scripts_containing("pnputil")

    runner = FakeRunner(drivers={"Acme PCL6"})       # driver already registered
    ws.ensure_vendor_queue(runner, "Lab", "10.0.0.7", "Acme PCL6", inf_path="C:\\d\\acme.inf")
    assert not runner.scripts_containing("pnputil"), "re-staging churns the DriverStore for nothing"


def test_missing_driver_without_inf_is_a_clear_error():
    runner = FakeRunner()
    with pytest.raises(ws.PowerShellError) as exc:
        ws.ensure_vendor_queue(runner, "Lab", "10.0.0.7", "Acme PCL6")
    assert "no INF" in str(exc.value)


def test_inf_path_travels_by_environment_too():
    runner = FakeRunner()
    nasty_inf = 'C:\\d\\a.inf"; calc.exe #'
    ws.ensure_vendor_queue(runner, "Lab", "10.0.0.7", "Acme PCL6", inf_path=nasty_inf)
    for script, _ in runner.calls:
        assert nasty_inf not in script


# --- removal and reconcile --------------------------------------------------


def test_remove_is_safe_when_absent():
    runner = FakeRunner()
    ws.remove_queue(runner, "Gone")
    # The script itself guards on existence, so this is a single call that
    # cannot fail on a queue an operator already deleted by hand.
    assert runner.scripts_containing("Remove-Printer")


def test_reconcile_reports_per_queue_and_one_failure_does_not_abort():
    runner = FakeRunner()
    outcomes = ws.reconcile(
        runner,
        [
            {"name": "A", "tier": "driverless", "uri": "ipp://10.0.0.5:631/ipp/print"},
            {"name": "B", "tier": "driverless", "uri": "ipp://10.0.0.6:631/ipp/print"},
        ],
    )
    assert set(outcomes) == {"A", "B"}


def test_reconcile_records_the_failing_queue_and_continues():
    class Boom(FakeRunner):
        """Fails queue A only. Everything else delegates to the real fake.

        Delegating matters: a blanket `return "OK"` also answers the -IppURL
        capability probe with "OK", which is not "YES", so tier 1 would refuse
        to provision anything and the test would pass for the wrong reason.
        """

        def run(self, script, variables=None):
            if variables and variables.get("name") == "A" and "-IppURL" in script:
                self.calls.append((script, dict(variables or {})))
                raise ws.PowerShellError("spooler said no", 1, "", "")
            return super().run(script, variables)

    outcomes = ws.reconcile(
        Boom(),
        [
            {"name": "A", "tier": "driverless", "uri": "ipp://10.0.0.5:631/ipp/print"},
            {"name": "B", "tier": "driverless", "uri": "ipp://10.0.0.6:631/ipp/print"},
        ],
    )
    assert outcomes["A"].startswith("error:")
    assert outcomes["B"] == "created", "one bad queue must not abort the rest"


def test_reconcile_without_a_prefix_removes_nothing():
    """Guard against deleting the user's own printers.

    An empty prefix disables removal rather than defaulting to 'delete
    everything unrecognised' -- which is the behaviour that gets a print
    management tool uninstalled.
    """
    runner = FakeRunner()
    ws.reconcile(runner, [{"name": "A", "tier": "driverless", "uri": "ipp://10.0.0.5:631/ipp/print"}])
    assert not runner.scripts_containing("Remove-Printer")


def test_reconcile_only_removes_queues_it_manages():
    class Listing(FakeRunner):
        """Controls only the queue listing; everything else delegates."""

        def run(self, script, variables=None):
            if "Get-Printer |" in script:
                self.calls.append((script, dict(variables or {})))
                return "\n".join([
                    "PN-Stale|p|d",          # ours, no longer wanted -> removed
                    "PN-A|p|d",              # ours and wanted -> kept
                    "Bob's Home Printer|p|d",  # not ours -> never touched
                ])
            return super().run(script, variables)

    runner = Listing()
    outcomes = ws.reconcile(
        runner,
        [{"name": "PN-A", "tier": "driverless", "uri": "ipp://10.0.0.5:631/ipp/print"}],
        managed_prefix="PN-",
    )
    removed = [v.get("name") for v in runner.vars_for("Remove-Printer")]
    assert removed == ["PN-Stale"]
    assert outcomes["PN-Stale"] == "removed"
    assert "Bob's Home Printer" not in outcomes


# --- tier 1 refuses rather than creating a queue that cannot print ----------


def test_a_windows_without_ippurl_is_refused_not_worked_around():
    """Server 2019's Add-Printer has no -IppURL.

    The tempting fallback -- make a TCP port and bind the class driver -- is
    exactly the defect this rework removed: it produces a queue that is created,
    listed, and unable to print. Refusing is the correct behaviour, and it must
    stay correct, so it is pinned.
    """
    runner = FakeRunner(ippurl=False)
    with pytest.raises(ws.IppProvisionError) as exc:
        ws.ensure_driverless_queue(runner, "Lobby", "ipp://10.0.0.5:631/ipp/print")
    assert exc.value.retryable is False
    assert not runner.scripts_containing("Add-PrinterPort"), "no RAW-port fallback"


def test_a_sleeping_printer_is_retryable_and_a_broken_build_is_not():
    """Central must be able to tell "printer was off" from "this will never work".

    Directed discovery is a live network query, so a powered-off device is a new
    failure mode the old code never had. Conflating it with a permanent failure
    either dispatches a technician for nothing or gives up on a printer that is
    merely asleep.
    """
    runner = FakeRunner(ipp_reachable=False)
    with pytest.raises(ws.IppProvisionError) as exc:
        ws.ensure_driverless_queue(runner, "Lobby", "ipp://10.0.0.5:631/ipp/print")
    assert exc.value.retryable is True


def test_a_bare_host_is_rejected():
    """Tier 1 needs a URL. A bare host silently became a RAW port before."""
    runner = FakeRunner()
    with pytest.raises(ws.IppProvisionError):
        ws.ensure_driverless_queue(runner, "Lobby", "10.0.0.5")


def test_a_missing_inbox_driver_is_reported_as_state_not_a_cmdlet_error():
    """Tier 3 checked its driver; tier 1 never did."""
    runner = FakeRunner(drivers=[])
    with pytest.raises(ws.IppProvisionError) as exc:
        ws.ensure_driverless_queue(runner, "Lobby", "ipp://10.0.0.5:631/ipp/print")
    assert exc.value.retryable is False
    assert "IPP Class Driver" in str(exc.value)


def test_a_queue_that_lands_on_a_raw_port_is_not_reported_as_provisioned():
    """The backstop against the whole class of defect returning.

    If anything ever produces a tier-1 queue on a Standard TCP/IP port again,
    it is an error -- never "created". Reporting success there is what let a
    non-printing queue look provisioned in central.
    """
    class LandsOnTcp(FakeRunner):
        def run(self, script, variables=None):
            if "Add-Printer -Name $env:PN_NAME -IppURL" in script:
                self.calls.append((script, dict(variables or {})))
                name = variables["name"]
                self.queues[name] = ("PN_bad", ws.IPP_CLASS_DRIVER)
                self.ports["PN_bad"] = "Standard TCP/IP Port"
                return "OK|PN_bad|" + ws.IPP_CLASS_DRIVER
            return super().run(script, variables)

    with pytest.raises(ws.IppProvisionError) as exc:
        ws.ensure_driverless_queue(LandsOnTcp(), "Lobby", "ipp://10.0.0.5:631/ipp/print")
    assert "Standard TCP/IP" in str(exc.value)


def test_reconcile_survives_an_unreachable_printer():
    """One sleeping device must not abort provisioning for every other queue."""
    runner = FakeRunner(ipp_reachable=False)
    outcomes = ws.reconcile(
        runner,
        [{"name": "A", "tier": "driverless", "uri": "ipp://10.0.0.5:631/ipp/print"}],
        managed_prefix="",
    )
    assert outcomes["A"].startswith("error:")
