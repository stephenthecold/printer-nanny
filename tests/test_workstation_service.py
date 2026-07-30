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

import pytest

from printer_nanny_agent import workstation as ws
from printer_nanny_agent import workstation_service as svc
from printer_nanny_agent.platforms import windows as windows_backend


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
    state = json.loads((tmp_path / "machine.json").read_text())
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


def test_setting_a_default_off_windows_reports_rather_than_raising():
    """The real setter on a non-Windows host: a stated reason, not a traceback."""
    with pytest.raises(svc.DefaultPrinterError, match="Windows"):
        svc.set_default_printer("PN Something")


def test_an_empty_default_name_is_refused():
    with pytest.raises(svc.DefaultPrinterError):
        svc.set_default_printer("")
