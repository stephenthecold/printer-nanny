"""The workstation service: identity, enrollment, spec mapping, the poll loop.

Everything here runs on any platform against a fake PowerShell runner and a fake
central. What that CANNOT prove is stated in the module under test and in
CLAUDE.md: a green run here says nothing about a real spooler, a real printer,
or the LocalSystem context. That is what tests/windows/ and
scripts/windows_provision_check.py are for.

The one thing worth being explicit about: `console_user` is Windows-only and
deliberately untested here. Faking ctypes would assert that our mock matches our
mock. Its failure modes are all "return None", which the loop already handles as
the login-screen case.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time

import pytest

from printer_nanny_agent import fsperm
from printer_nanny_agent import workstation as ws
from printer_nanny_agent import workstation_service as svc
from printer_nanny_agent.platforms import windows as windows_backend

# Windows has no POSIX mode bits -- os.chmod there only toggles the read-only
# attribute, so a file written 0600 reads back 0666 and the assertion fails for
# a reason that has nothing to do with how the credential is handled. Windows
# restricts this file by ACL instead (LockPermissions, SYSTEM +
# Administrators), which tests/test_workstation_msi.py asserts.
posix_modes_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX mode bits; Windows restricts this by ACL instead"
)


@pytest.fixture(autouse=True)
def _windows_backend(monkeypatch):
    """Drive the orchestrator through the WINDOWS backend, on whatever host.

    These tests have always exercised the Windows path -- before the platform
    seam existed they reached it by monkeypatching ``ws.reconcile``. Now that
    backend selection is explicit, so is this: without it the suite would pick
    the "unsupported" backend on a Linux runner and assert nothing about either
    real implementation.

    ``platforms.windows.provision_queues`` looks ``ws.reconcile`` up at call
    time, so the existing per-test monkeypatches still take effect through it.
    """
    monkeypatch.setattr(svc, "_platform", lambda: windows_backend)


class FakeRunner:
    """Records what would have run; returns whatever it is told to.

    Same shape as the fake in tests/windows/: no amount of testing above this
    seam can catch a PowerShell-level defect, which is the point of keeping the
    seam narrow.
    """

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def run(self, script, variables=None):
        self.calls.append((script, dict(variables or {})))
        for marker, out in self.responses.items():
            if marker in script:
                return out
        return ""


class FakeCentral:
    """Stands in for the three workstation endpoints."""

    def __init__(self, payload=None, *, enroll=None):
        self.payload = payload or {"printers": []}
        self.enroll_result = enroll or {
            "machine_id": 7, "api_key": "pnm_secret", "client_id": 1, "created": True
        }
        self.machine_id = None
        self.api_key = None
        self.checkins = []
        self.asked_for = []
        self.enroll_calls = []

    def enroll(self, enroll_key, uid, name):
        self.enroll_calls.append((enroll_key, uid, name))
        return self.enroll_result

    def assignments(self, user=None):
        self.asked_for.append(user)
        return self.payload

    def checkin(self, name):
        self.checkins.append(name)
        return {"ok": True}

    def adopt(self, machine_id, api_key):
        self.machine_id, self.api_key = machine_id, api_key

    def close(self):
        pass


def _printer(pid=1, name="Front Desk MFP", tier="driverless",
             uri="ipp://10.0.0.5:631/ipp/print", default=False):
    return {"printer_id": pid, "name": name, "ip": "10.0.0.5",
            "is_default": default, "driver_tier": tier, "ipp_endpoint": uri}


# ----------------------------- identity ------------------------------------ #


def test_machine_uid_is_minted_once_and_then_stable(tmp_path):
    first = svc.machine_uid(str(tmp_path))
    second = svc.machine_uid(str(tmp_path))
    assert first == second
    assert len(first) == 32


def test_the_uid_survives_a_rename_because_it_is_not_the_name(tmp_path):
    """The reason a GUID was chosen over the computer name."""
    before = svc.machine_uid(str(tmp_path))
    state = svc.load_state(str(tmp_path))
    state["name"] = "RENAMED-PC"
    svc.save_state(state, str(tmp_path))
    assert svc.machine_uid(str(tmp_path)) == before


def test_a_re_image_is_a_new_machine(tmp_path):
    """State is gone, so the identity is new -- deliberately, so a fresh PC
    does not inherit a departed user's printers."""
    first = svc.machine_uid(str(tmp_path / "a"))
    second = svc.machine_uid(str(tmp_path / "b"))
    assert first != second


@posix_modes_only
def test_state_holding_a_credential_is_owner_only(tmp_path):
    svc.save_state({"api_key": "pnm_secret"}, str(tmp_path))
    mode = os.stat(tmp_path / "machine.json").st_mode
    assert not (mode & stat.S_IRGRP), "group must not read a live API key"
    assert not (mode & stat.S_IROTH), "world must not read a live API key"


def test_a_corrupt_state_file_re_enrolls_rather_than_bricking(tmp_path):
    (tmp_path / "machine.json").write_text("{ this is not json")
    assert svc.load_state(str(tmp_path)) == {}
    assert svc.machine_uid(str(tmp_path))  # mints a fresh one instead of raising


def test_state_is_written_atomically(tmp_path, monkeypatch):
    """A crash mid-write must not leave a half-file that reads as no credential."""
    svc.save_state({"machine_id": 7, "api_key": "pnm_a"}, str(tmp_path))
    real_replace = os.replace

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        svc.save_state({"machine_id": 9, "api_key": "pnm_b"}, str(tmp_path))
    monkeypatch.setattr(os, "replace", real_replace)

    # The original survived, and no temp files were left behind.
    assert svc.load_state(str(tmp_path))["api_key"] == "pnm_a"
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".pn-state.")]


# ---------------------------- enrollment ----------------------------------- #


def test_credentials_are_persisted_before_they_are_used(tmp_path):
    """The site agent's rule: an agent that redeems and forgets is bricked."""
    central = FakeCentral()
    svc.ensure_enrolled(
        central, enroll_key="pnw_k", computer_name="PC1", state_dir=str(tmp_path)
    )
    state = json.loads((tmp_path / "machine.json").read_text(encoding="utf-8"))
    assert state["machine_id"] == 7
    assert state["api_key"] == "pnm_secret"
    assert central.machine_id == 7


def test_enrolling_twice_reuses_the_stored_credential(tmp_path):
    central = FakeCentral()
    svc.ensure_enrolled(
        central, enroll_key="pnw_k", computer_name="PC1", state_dir=str(tmp_path)
    )
    second = FakeCentral()
    svc.ensure_enrolled(
        second, enroll_key="pnw_k", computer_name="PC1", state_dir=str(tmp_path)
    )
    assert second.enroll_calls == [], "a stored credential must not re-enroll"
    assert second.machine_id == 7


def test_the_same_uid_is_sent_on_re_enrollment(tmp_path):
    """Central rotates rather than duplicating -- but only if we send the same
    uid, so the uid must come from state, not be regenerated."""
    uid = svc.machine_uid(str(tmp_path))
    central = FakeCentral()
    svc.ensure_enrolled(
        central, enroll_key="pnw_k", computer_name="PC1", state_dir=str(tmp_path)
    )
    assert central.enroll_calls[0][1] == uid


# ---------------- the default printer writes the RIGHT user's hive ---------- #
#
# _stop_windows_managing_default cannot be exercised off Windows, so these are
# structural assertions in the same spirit as the _SCRIPT_* placeholder tests:
# they pin the one call that made the difference. HKEY_CURRENT_USER resolves
# against the PROCESS token, so in a session-0 service running as LocalSystem it
# stays on SYSTEM's hive however the thread is impersonating. Measured on a real
# Windows 11 box under NSSM: [WinError 5], raised before SetDefaultPrinter, so
# with the shipped default an assigned default never applied at all.


def test_the_default_printer_write_follows_the_impersonated_user():
    import ast
    import inspect

    src = inspect.getsource(svc._stop_windows_managing_default)
    # Compare the CODE, not the prose: the docstring names the wrong constant on
    # purpose, to explain why it is wrong.
    tree = ast.parse(src.strip())
    body = tree.body[0].body
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # drop the docstring
    code = "\n".join(ast.unparse(n) for n in body)

    assert "RegOpenCurrentUser" in code, (
        "the LegacyDefaultPrinterMode write must resolve the THREAD token's hive"
    )
    assert "HKEY_CURRENT_USER" not in code, (
        "HKEY_CURRENT_USER follows the process token, not the impersonation -- "
        "in a session-0 service it is SYSTEM's hive, and the write is refused"
    )


def test_the_registry_write_runs_before_the_default_is_set():
    """Ordering is why this defect was total rather than partial: anything the
    registry write raises takes the whole feature down with it."""
    import inspect

    src = inspect.getsource(svc._windows_set_default_printer)
    assert "if manage_windows_default:" in src
    assert src.index("if manage_windows_default:") < src.index("SetDefaultPrinterW")


# ------------------- enrollment is inside the retry loop -------------------- #
#
# These cover a defect found by installing on a real Mac and nothing else: the
# poll loop honoured run()'s "transport failures are retried, not fatal"
# contract while ``ensure_enrolled`` sat above it with no handler. A central
# that could not be resolved therefore killed the process, launchd respawned it
# every 60s, and the log grew ~9.8 MB/day of tracebacks stating no reason -- at
# exactly the moment a freshly imaged machine is least likely to have network.


class _UnreachableCentral(FakeCentral):
    """Enrolls only after ``fails`` attempts, like a box behind a captive portal."""

    def __init__(self, fails=99, **kw):
        super().__init__(**kw)
        self.fails = fails
        self.attempts = 0

    def enroll(self, enroll_key, uid, name):
        self.attempts += 1
        if self.attempts <= self.fails:
            raise OSError("[Errno 8] nodename nor servname provided, or not known")
        return super().enroll(enroll_key, uid, name)


def _run_with(monkeypatch, central, **kw):
    monkeypatch.setattr(svc, "WorkstationClient", lambda *a, **k: central)
    monkeypatch.setattr(svc, "console_user", lambda: None)
    return svc.run("https://central.invalid", "pnw_k", once=True, **kw)


def test_an_unreachable_central_at_enrollment_does_not_kill_the_service(
    monkeypatch, tmp_path
):
    central = _UnreachableCentral()
    # The point: this returns rather than raising. Before the fix the transport
    # error propagated out of run() and out of main(), and the service died.
    report = _run_with(monkeypatch, central, state_dir=str(tmp_path))
    assert report.cycle_error
    assert "nodename nor servname" in report.cycle_error


def test_a_cycle_that_never_reached_central_is_not_reported_as_ok(
    monkeypatch, tmp_path
):
    """--once must not exit 0 for work it did not do -- the `sent=False` rule."""
    report = _run_with(monkeypatch, _UnreachableCentral(), state_dir=str(tmp_path))
    assert report.ok is False


def test_enrollment_is_retried_on_the_next_tick(monkeypatch, tmp_path):
    """The whole point of moving it inside the loop: it recovers by itself."""
    central = _UnreachableCentral(fails=2)
    monkeypatch.setattr(svc, "WorkstationClient", lambda *a, **k: central)
    monkeypatch.setattr(svc, "console_user", lambda: None)

    ticks = []

    def _stop_after_three(seconds):
        ticks.append(seconds)
        if len(ticks) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(svc.time, "sleep", _stop_after_three)
    with pytest.raises(KeyboardInterrupt):
        svc.run("https://central.invalid", "pnw_k", state_dir=str(tmp_path),
                interval=1)

    assert central.attempts == 3, "it must keep trying, not give up after one"
    assert central.machine_id == 7, "and adopt the credential once it succeeds"


def test_a_refused_key_is_still_terminal(monkeypatch, tmp_path):
    """A key central has rejected must NOT be retried forever: the CLI's
    distinct exit code exists so the reason is reported once, not buried."""

    class _RefusingCentral(FakeCentral):
        def enroll(self, enroll_key, uid, name):
            raise svc.ServiceError("enrollment refused: the key is not valid")

    monkeypatch.setattr(svc, "WorkstationClient", lambda *a, **k: _RefusingCentral())
    monkeypatch.setattr(svc, "console_user", lambda: None)
    with pytest.raises(svc.ServiceError):
        svc.run("https://central.invalid", "pnw_k", once=True,
                state_dir=str(tmp_path))


def test_an_enrolled_client_makes_no_enroll_request_per_cycle(
    monkeypatch, tmp_path
):
    """Moving it into the loop must stay free: once enrolled it only adopts."""
    central = FakeCentral()
    svc.ensure_enrolled(
        central, enroll_key="pnw_k", computer_name="PC1", state_dir=str(tmp_path)
    )
    central.enroll_calls.clear()
    _run_with(monkeypatch, central, state_dir=str(tmp_path))
    assert central.enroll_calls == []


# --------------------------- spec mapping ---------------------------------- #


def test_only_driverless_printers_are_provisioned():
    payload = {"printers": [
        _printer(1, "Driverless", "driverless"),
        _printer(2, "Vendor", "driver_required"),
        _printer(3, "NoIpp", "ipp_disabled"),
        _printer(4, "Dead", "unreachable"),
        _printer(5, "Unknown", None),
    ]}
    specs, skipped, _ = svc.build_specs(payload)
    assert len(specs) == 1
    assert specs[0]["tier"] == "driverless"
    assert len(skipped) == 4


def test_every_skip_carries_a_reason():
    """A printer that was assigned and did not appear is what an operator gets
    asked about; a silent absence gives them nothing to answer with."""
    payload = {"printers": [
        _printer(2, "Vendor", "driver_required"),
        _printer(3, "NoIpp", "ipp_disabled"),
        _printer(5, "Unknown", None),
    ]}
    _, skipped, _ = svc.build_specs(payload)
    assert all(why.strip() for why in skipped.values())
    assert any("driver" in w for w in skipped.values())
    # ipp_disabled must not be reported as a driver problem -- that sends a
    # technician to the device's web UI vs the driver store, and they are
    # different places.
    ipp = [w for w in skipped.values() if "631" in w or "IPP is disabled" in w]
    assert ipp and "driver" not in ipp[0].lower()


def test_a_driverless_printer_with_no_endpoint_is_skipped_not_guessed():
    payload = {"printers": [_printer(1, "X", "driverless", uri=None)]}
    specs, skipped, _ = svc.build_specs(payload)
    assert specs == []
    assert "no IPP endpoint" in list(skipped.values())[0]


def test_queue_names_carry_the_managed_prefix():
    name = svc.queue_name_for(_printer(1, "Front Desk MFP"))
    assert name.startswith(svc.MANAGED_PREFIX)


def test_two_printers_sharing_a_display_name_get_distinct_queues():
    """A collision would make one silently replace the other on every poll."""
    a = svc.queue_name_for(_printer(1, "MFP"))
    b = svc.queue_name_for(_printer(2, "MFP"))
    assert a != b


def test_queue_names_drop_characters_windows_rejects():
    hostile = svc.queue_name_for(_printer(1, 'Bad\\Name,With"Chars!'))
    assert not set(hostile) & set('\\/,!"')


def test_a_very_long_name_is_bounded():
    long_name = svc.queue_name_for(_printer(1, "A" * 500))
    assert len(long_name) < 250


def test_the_desired_default_is_recorded_but_not_claimed():
    """Setting a per-user default from LocalSystem needs impersonation and is
    not built. Reporting it as done would be the exact failure this codebase
    keeps warning about."""
    payload = {"printers": [_printer(1, "A"), _printer(2, "B", default=True)]}
    _, _, desired = svc.build_specs(payload)
    assert desired == svc.queue_name_for(_printer(2, "B"))


# ------------------------------ the loop ----------------------------------- #


def test_provision_only_removes_queues_carrying_the_prefix(monkeypatch):
    """reconcile's contract, asserted from this layer: deleting a user's own
    printer is how a print tool gets uninstalled."""
    seen = {}

    def fake_reconcile(runner, desired, managed_prefix=""):
        seen["prefix"] = managed_prefix
        return {d["name"]: "created" for d in desired}

    monkeypatch.setattr(ws, "reconcile", fake_reconcile)
    report = svc.provision(FakeRunner(), {"printers": [_printer(1)]})
    assert seen["prefix"] == svc.MANAGED_PREFIX
    assert report.ok


def test_a_failing_queue_does_not_abort_the_others(monkeypatch):
    def fake_reconcile(runner, desired, managed_prefix=""):
        return {"PN A (1)": "error: boom", "PN B (2)": "created"}

    monkeypatch.setattr(ws, "reconcile", fake_reconcile)
    report = svc.provision(FakeRunner(), {"printers": [_printer(1), _printer(2)]})
    assert report.ok is False
    assert report.outcomes["PN B (2)"] == "created"


def test_poll_checks_in_even_when_provisioning_fails(monkeypatch):
    """A machine failing to provision is exactly the one that must not look
    offline."""
    def fake_reconcile(runner, desired, managed_prefix=""):
        return {"PN A (1)": "error: boom"}

    monkeypatch.setattr(ws, "reconcile", fake_reconcile)
    central = FakeCentral({"printers": [_printer(1)]})
    svc.poll_once(central, FakeRunner(), computer_name="PC1")
    assert central.checkins == ["PC1"]


def test_a_failing_checkin_does_not_lose_the_provisioning_result(monkeypatch):
    def fake_reconcile(runner, desired, managed_prefix=""):
        return {"PN A (1)": "created"}

    def boom(name):
        raise RuntimeError("network down")

    monkeypatch.setattr(ws, "reconcile", fake_reconcile)
    central = FakeCentral({"printers": [_printer(1)]})
    central.checkin = boom
    report = svc.poll_once(central, FakeRunner(), computer_name="PC1")
    assert report.outcomes["PN A (1)"] == "created"


def test_the_signed_in_user_is_passed_through(monkeypatch):
    monkeypatch.setattr(ws, "reconcile", lambda *a, **k: {})
    central = FakeCentral()
    svc.poll_once(central, FakeRunner(), computer_name="PC1", user="jo@acme.test")
    assert central.asked_for == ["jo@acme.test"]


def test_no_signed_in_user_still_polls(monkeypatch):
    """The login screen is a normal state, not an error -- it is the
    shared-terminal case."""
    monkeypatch.setattr(ws, "reconcile", lambda *a, **k: {})
    central = FakeCentral()
    svc.poll_once(central, FakeRunner(), computer_name="PC1", user=None)
    assert central.asked_for == [None]


def test_a_hostile_printer_name_never_reaches_a_script_body(monkeypatch):
    """Names come from operator free-text and device strings. The scripts are
    constant and values travel by environment, so this asserts the shape that
    makes injection moot rather than merely handled."""
    captured = {}

    def fake_reconcile(runner, desired, managed_prefix=""):
        captured["desired"] = desired
        return {d["name"]: "created" for d in desired}

    monkeypatch.setattr(ws, "reconcile", fake_reconcile)
    nasty = 'x"; Remove-Item -Recurse C:\\ #'
    svc.provision(FakeRunner(), {"printers": [_printer(1, nasty)]})
    name = captured["desired"][0]["name"]
    assert "\\" not in name and '"' not in name


def test_every_powershell_script_is_still_a_constant():
    """Guards the rule from the other direction: if a format placeholder ever
    appears in a script body, values have started being interpolated."""
    for attr in dir(ws):
        if attr.startswith("_SCRIPT_"):
            body = getattr(ws, attr)
            assert "{}" not in body and "%s" not in body, attr


# ------------------------ the user's default printer ------------------------ #


def _default_payload(default_name="Front Desk MFP", manage=True):
    return {
        "printers": [_printer(1, default_name, default=True)],
        "manage_default_printer": manage,
    }


class _Setter:
    """Stands in for the impersonating Win32 path, which cannot run here."""

    def __init__(self, result=None, error=None):
        self.result, self.error = result, error
        self.calls = []

    def __call__(self, name, manage_windows_default=True):
        self.calls.append((name, manage_windows_default))
        if self.error:
            raise self.error
        return self.result if self.result is not None else name


def test_the_default_is_applied_and_reported(monkeypatch):
    monkeypatch.setattr(ws, "reconcile", lambda runner, desired, managed_prefix="":
                        {d["name"]: "created" for d in desired})
    setter = _Setter()
    report = svc.provision(FakeRunner(), _default_payload(), default_setter=setter)

    assert report.default_applied == report.desired_default
    assert report.default_reason is None
    assert setter.calls == [(report.desired_default, True)]


def test_the_operator_setting_reaches_the_setter(monkeypatch):
    """Sent per poll, so turning it off takes effect without reinstalling."""
    monkeypatch.setattr(ws, "reconcile", lambda runner, desired, managed_prefix="":
                        {d["name"]: "created" for d in desired})
    setter = _Setter()
    svc.provision(FakeRunner(), _default_payload(manage=False), default_setter=setter)
    assert setter.calls[0][1] is False


def test_a_default_is_never_pointed_at_a_queue_that_failed(monkeypatch):
    """Setting it to a queue that was not provisioned is exactly the failure
    this module exists to avoid."""
    monkeypatch.setattr(ws, "reconcile", lambda runner, desired, managed_prefix="":
                        {d["name"]: "error: boom" for d in desired})
    setter = _Setter()
    report = svc.provision(FakeRunner(), _default_payload(), default_setter=setter)

    assert setter.calls == [], "must not try to set a default for a broken queue"
    assert report.default_applied is None
    assert "not provisioned" in report.default_reason


def test_a_default_is_never_pointed_at_a_skipped_queue(monkeypatch):
    """A driver_required printer with no package is skipped -- and must not
    become the default anyway."""
    monkeypatch.setattr(ws, "reconcile", lambda runner, desired, managed_prefix="": {})
    payload = {"printers": [_printer(1, "Vendor", "driver_required", default=True)],
               "manage_default_printer": True}
    setter = _Setter()
    report = svc.provision(FakeRunner(), payload, default_setter=setter)

    assert setter.calls == []
    assert report.default_applied is None
    assert report.default_reason


def test_a_failure_to_set_becomes_a_stated_reason_not_a_crash(monkeypatch):
    monkeypatch.setattr(ws, "reconcile", lambda runner, desired, managed_prefix="":
                        {d["name"]: "created" for d in desired})
    setter = _Setter(error=svc.DefaultPrinterError("nobody is signed in"))
    report = svc.provision(FakeRunner(), _default_payload(), default_setter=setter)

    assert report.default_applied is None
    assert report.default_reason == "nobody is signed in"
    assert report.ok, "the queues provisioned; only the default is in question"


def test_a_default_that_did_not_stick_is_not_reported_as_applied(monkeypatch):
    """The whole reason default_applied is separate from desired_default: a
    write that returned success is not evidence the user has that default."""
    monkeypatch.setattr(ws, "reconcile", lambda runner, desired, managed_prefix="":
                        {d["name"]: "created" for d in desired})
    setter = _Setter(error=svc.DefaultPrinterError(
        "default did not stick (wanted X, got Y); Windows may still be managing"))
    report = svc.provision(FakeRunner(), _default_payload(), default_setter=setter)

    assert report.default_applied is None
    assert "did not stick" in report.default_reason


def test_no_desired_default_means_nothing_is_touched(monkeypatch):
    """A machine with assignments but no default must not have its user's
    existing default rewritten."""
    monkeypatch.setattr(ws, "reconcile", lambda runner, desired, managed_prefix="":
                        {d["name"]: "created" for d in desired})
    setter = _Setter()
    payload = {"printers": [_printer(1, "A", default=False)]}
    report = svc.provision(FakeRunner(), payload, default_setter=setter)

    assert setter.calls == []
    assert report.default_applied is None and report.default_reason is None


@pytest.mark.skipif(
    os.name == "nt",
    reason="asserts the non-Windows backend; on Windows this dispatches to the "
           "real ctypes setter, which impersonates and calls the live spooler",
)
def test_setting_a_default_off_windows_reports_rather_than_raising():
    """The real setter on a non-Windows host: a stated reason, not a traceback."""
    with pytest.raises(svc.DefaultPrinterError, match="Windows"):
        svc.set_default_printer("PN Something")


def test_an_empty_default_name_is_refused():
    with pytest.raises(svc.DefaultPrinterError):
        svc.set_default_printer("")


# --------------------------------------------------------------------------- #
# The refused-key sentinel
# --------------------------------------------------------------------------- #
# The contradiction this resolves: workstation_cli returns exit 2 for a refused
# enrollment key so a service manager will not loop, but the LaunchDaemon sets
# KeepAlive{SuccessfulExit=false}, which restarts on ANY non-zero exit. launchd
# cannot express "restart unless the exit code is 2", so the exit code was
# documenting a behaviour that did not occur. These tests pin the mechanism that
# does occur. They do NOT prove launchd's side of it -- that needs a Mac and a
# counted restart, per deploy/HARDWARE-VERIFICATION.md Part 3.
def test_a_refused_key_does_not_block_the_first_start(tmp_path):
    """Nothing recorded yet means nothing suppressed. The first refusal is real
    work that has to happen before anything can be cached about it."""
    assert svc.refusal_blocks_start("pnw_k", state_dir=str(tmp_path)) is None


def test_the_same_refused_key_blocks_the_next_start(tmp_path):
    svc.record_refusal("pnw_k", "enrollment refused: the key is not valid",
                       state_dir=str(tmp_path))
    blocked = svc.refusal_blocks_start("pnw_k", state_dir=str(tmp_path))
    assert blocked is not None
    assert "not valid" in blocked, "the reason must survive, or the loop is quiet AND mute"


def test_the_sentinel_never_stores_the_key(tmp_path):
    """It sits next to machine.json; a second copy of a live credential on disk
    is a second thing to leak. A fingerprint identifies the key without being it."""
    svc.record_refusal("pnw_supersecret", "nope", state_dir=str(tmp_path))
    raw = (tmp_path / svc.REFUSAL_FILENAME).read_text(encoding="utf-8")
    assert "pnw_supersecret" not in raw
    assert json.loads(raw)["key_sha256"] != "pnw_supersecret"


def test_a_new_key_clears_the_sentinel(tmp_path):
    """This is what removes the documented cost of the sentinel option -- that an
    operator must know to delete a state file. Re-minting the key IS the fix, so
    the fix clears it, and nobody has to be told the file exists."""
    svc.record_refusal("pnw_old", "revoked", state_dir=str(tmp_path))
    assert svc.refusal_blocks_start("pnw_new", state_dir=str(tmp_path)) is None
    assert not (tmp_path / svc.REFUSAL_FILENAME).exists(), "stale sentinel must be removed"


def test_the_sentinel_expires_so_an_un_revoked_key_recovers(tmp_path):
    """A key central un-revokes server-side does not change, so a fingerprint
    alone would keep this machine dead forever. The window is what stops the
    mechanism becoming permanent poison."""
    svc.record_refusal("pnw_k", "revoked", state_dir=str(tmp_path))
    just_inside = svc.REFUSAL_RETRY_SECONDS - 60
    assert svc.refusal_blocks_start(
        "pnw_k", state_dir=str(tmp_path), now=time.time() + just_inside
    ) is not None
    assert svc.refusal_blocks_start(
        "pnw_k", state_dir=str(tmp_path), now=time.time() + svc.REFUSAL_RETRY_SECONDS + 1
    ) is None


def test_a_corrupt_sentinel_fails_open(tmp_path):
    """Same call as the state file next to it: the cost of ignoring a corrupt
    sentinel is one more refusal, the cost of trusting one is a machine that
    never enrolls again. So it blocks nothing and is removed."""
    (tmp_path / svc.REFUSAL_FILENAME).write_text("{not json", encoding="utf-8")
    assert svc.refusal_blocks_start("pnw_k", state_dir=str(tmp_path)) is None
    assert not (tmp_path / svc.REFUSAL_FILENAME).exists()


def test_a_sentinel_missing_its_fields_fails_open(tmp_path):
    (tmp_path / svc.REFUSAL_FILENAME).write_text('{"at": 1}', encoding="utf-8")
    assert svc.refusal_blocks_start("pnw_k", state_dir=str(tmp_path)) is None


def test_clearing_a_refusal_that_is_not_there_is_not_an_error(tmp_path):
    svc.clear_refusal(state_dir=str(tmp_path))


def test_only_a_refused_key_records_a_sentinel(tmp_path, monkeypatch):
    """The sentinel is keyed on the enrollment key, so only a refusal OF THAT KEY
    may write one. A sibling ServiceError is terminal too -- it still earns exit
    2 -- but suppressing the next start would blame a key that was never wrong.
    """
    from printer_nanny_agent import workstation_cli
    from printer_nanny_agent import workstation_service as service

    common = ["--server", "https://c.example", "--enroll-key", "pnw_k",
              "--state-dir", str(tmp_path)]

    def raise_unrelated(*a, **k):
        raise service.ServiceError("config is wrong in some terminal way")

    monkeypatch.setattr(service, "run", raise_unrelated)
    assert workstation_cli.main(common) == 2, "still terminal, still exit 2"
    assert not (tmp_path / service.REFUSAL_FILENAME).exists()

    def raise_refused(*a, **k):
        raise service.EnrollmentRefused("enrollment refused: the key is not valid")

    monkeypatch.setattr(service, "run", raise_refused)
    assert workstation_cli.main(common) == 2, "the FIRST refusal exits truthfully"
    assert (tmp_path / service.REFUSAL_FILENAME).exists()

    # ...and the second start with that same key is the one that goes quiet, so
    # launchd's KeepAlive{SuccessfulExit=false} stops after exactly one restart.
    called = []
    monkeypatch.setattr(service, "run", lambda *a, **k: called.append(1))
    assert workstation_cli.main(common) == 0
    assert called == [], "a blocked start must not reach the service at all"


def test_once_neither_reads_nor_writes_the_sentinel(tmp_path, monkeypatch):
    """A diagnostic run must answer "what happens if I run it now", not replay a
    cached refusal -- and must not leave state that silences the service."""
    from printer_nanny_agent import workstation_cli
    from printer_nanny_agent import workstation_service as service

    common = ["--server", "https://c.example", "--enroll-key", "pnw_k",
              "--state-dir", str(tmp_path), "--once"]

    def raise_refused(*a, **k):
        raise service.EnrollmentRefused("enrollment refused: the key is not valid")

    monkeypatch.setattr(service, "run", raise_refused)
    assert workstation_cli.main(common) == 2
    assert not (tmp_path / service.REFUSAL_FILENAME).exists(), "--once wrote a sentinel"

    # A sentinel left by the service must not change what --once reports.
    service.record_refusal("pnw_k", "revoked", state_dir=str(tmp_path))
    assert workstation_cli.main(common) == 2, "--once must still really try"


# --------------------------------------------------------------------------- #
# The state directory's permissions
# --------------------------------------------------------------------------- #
# os.chmod on Windows toggles the read-only attribute and writes NO DACL, so the
# 0600 set on machine.json was never a permission there at all -- the file simply
# inherited BUILTIN\Users:(I)(RX) from C:\ProgramData, and any logged-in user
# could read this machine's live bearer credential. Verified by observation on
# Windows 11 before the fix: the interactive user held (I)(OI)(CI)(F).
def test_the_state_directory_is_restricted_to_its_owner(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    fsperm.reset_cache_for_tests()
    svc._secure_state_dir(str(d))

    if os.name == "nt":
        try:
            out = subprocess.run(
                ["icacls", str(d)], capture_output=True, text=True
            ).stdout
            # SIDs resolve to localised names in icacls OUTPUT, so assert the
            # PROPERTY rather than the names or a fixed count: inheritance is
            # broken, and the group that granted every logged-in user is gone.
            # A surviving "(I)" means /inheritance:r did not take.
            assert "(I)" not in out, f"inheritance was not broken:\n{out}"
            assert "Users:" not in out, f"every logged-in user still has it:\n{out}"
            entries = [ln for ln in out.splitlines()[:20] if ":(" in ln]
            # SYSTEM + Administrators, plus this process's own account. That
            # third grant is deliberate: under the service it resolves to SYSTEM
            # and adds nothing, but for a technician running --once it is the
            # difference between a working diagnostic and a PermissionError on a
            # directory they had just created.
            assert 2 <= len(entries) <= 3, (
                f"expected SYSTEM + Administrators (+ this account):\n{out}"
            )
        finally:
            # Hand it back so pytest's tmp cleanup can remove the directory.
            subprocess.run(
                ["icacls", str(d), "/reset", "/T", "/C", "/Q"],
                capture_output=True, text=True,
            )
    else:
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_securing_the_state_directory_never_takes_the_service_down(tmp_path, monkeypatch):
    """Best-effort by design: a workstation must still provision its queues if
    the ACL cannot be set. It warns rather than raising -- but it does warn,
    because a silent failure is how this stayed invisible."""
    d = tmp_path / "state"
    d.mkdir()
    fsperm.reset_cache_for_tests()

    def boom(*a, **k):
        raise OSError("icacls is missing")

    monkeypatch.setattr(os, "chmod", boom)
    if os.name == "nt":
        import subprocess as _sp
        monkeypatch.setattr(_sp, "run", boom)
    svc._secure_state_dir(str(d))  # must not raise


def test_writing_state_secures_the_directory_it_creates(tmp_path, monkeypatch):
    """The call has to be wired in, not merely available."""
    seen = []
    monkeypatch.setattr(svc, "_secure_state_dir", lambda d: seen.append(d))
    svc.save_state({"api_key": "pnm_secret"}, str(tmp_path))
    assert seen == [str(tmp_path)]
