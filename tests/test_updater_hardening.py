"""Hardening of the agent self-updater.

Covers the safety guarantees added so a bad config / flaky network can't
crash-loop the service, and so the dashboard can confirm an update actually
landed:

* Pre-flight gating (empty / placeholder / junk source, dead pip) -- recorded
  failure, NO pip call, NO restart.
* Structured result fields ({ok, status, old_version, new_version, error, ts}
  plus the legacy `detail`).
* Atomic write + safe re-run (an interrupted write never corrupts the marker;
  running the path twice is coherent).
* Success vs no-op detection (pip exits 0 but the installed base version is
  unchanged -> ok=False, status='no_op').

Everything mocks the pip subprocess -- no real install ever runs.
"""

from __future__ import annotations

import json

import pytest

from printer_nanny_agent import updater


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "source",
    [
        None,
        "",
        "   ",
        "git+https://github.com/your-org/printer-nanny.git#subdirectory=agent",
        "--upgrade",  # leading dash: pip would read it as a flag
        "git+https://x/y with spaces",
    ],
)
async def test_preflight_rejects_bad_sources(monkeypatch, source):
    """Empty / placeholder / junk sources fail pre-flight WITHOUT ever
    touching pip (the binary check is the last step and must not be reached)."""
    pip_called = []

    async def boom(pip, timeout_seconds=30.0):
        pip_called.append(True)
        return True, ""

    monkeypatch.setattr(updater, "_pip_invokable", boom)
    ok, reason = await updater.preflight(source)
    assert ok is False
    assert reason
    # The cheap string checks short-circuit before the pip-invokable probe.
    assert pip_called == []


async def test_preflight_passes_real_git_source(monkeypatch):
    """A real git+https source + invokable pip -> pre-flight passes."""
    async def ok_pip(pip, timeout_seconds=30.0):
        return True, ""

    monkeypatch.setattr(updater, "_pip_invokable", ok_pip)
    ok, reason = await updater.preflight(
        "git+https://github.com/stephenthecold/printer-nanny.git#subdirectory=agent"
    )
    assert ok is True
    assert reason == ""


async def test_preflight_fails_when_pip_not_invokable(monkeypatch):
    """Source is fine but pip itself is dead -> pre-flight fails with pip's
    reason (so the operator knows it's the venv, not the URL)."""
    async def dead_pip(pip, timeout_seconds=30.0):
        return False, "pip not found at /opt/agent/.venv/bin/pip"

    monkeypatch.setattr(updater, "_pip_invokable", dead_pip)
    ok, reason = await updater.preflight("git+https://x/y")
    assert ok is False
    assert "pip not found" in reason


async def test_preflight_failure_records_result_no_pip_no_restart(monkeypatch, tmp_path):
    """End-to-end pre-flight failure: a recorded failure result, run_self_update
    NEVER called, restart NEVER called -- the agent stays up on old code."""
    marker = tmp_path / "m.json"
    monkeypatch.setattr(updater, "_result_path", lambda: marker)

    ran = []
    exited = []

    async def fake_run(source, timeout_seconds=300.0):
        ran.append(source)
        return True, ""

    async def fail_pre(source):
        return False, "no pip_source configured"

    monkeypatch.setattr(updater, "preflight", fail_pre)
    monkeypatch.setattr(updater, "run_self_update", fake_run)
    monkeypatch.setattr(updater, "restart_for_service_manager", lambda code=0: exited.append(code))

    await updater.perform_self_update("git+https://x/y")

    assert ran == [], "pip install must not run when pre-flight fails"
    assert exited == [], "service must not restart on a pre-flight failure"
    res = updater.read_last_update_result()
    assert res["ok"] is False
    assert res["status"] == "preflight_failed"
    assert "no pip_source" in res["error"]


async def test_empty_source_records_no_pip_source_status(monkeypatch, tmp_path):
    """An explicitly-empty source gets the dedicated 'no_pip_source' status
    (distinct from a junk-but-present source) so the dashboard can phrase it."""
    marker = tmp_path / "m.json"
    monkeypatch.setattr(updater, "_result_path", lambda: marker)
    monkeypatch.setattr(updater, "restart_for_service_manager", lambda code=0: None)
    await updater.perform_self_update("")
    res = updater.read_last_update_result()
    assert res["status"] == "no_pip_source"
    assert res["ok"] is False


# ---------------------------------------------------------------------------
# Structured result fields
# ---------------------------------------------------------------------------

def test_write_result_has_all_structured_fields(monkeypatch, tmp_path):
    """Every required field is present and typed correctly."""
    marker = tmp_path / "m.json"
    monkeypatch.setattr(updater, "_result_path", lambda: marker)
    payload = updater._write_result(
        "ok", ok=True, old_version="0.3.0", new_version="0.4.0",
        error=None, detail="done",
    )
    for key in ("ok", "status", "old_version", "new_version", "error", "detail", "ts"):
        assert key in payload, key
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["old_version"] == "0.3.0"
    assert payload["new_version"] == "0.4.0"
    assert payload["error"] is None
    assert payload["ts"].endswith("Z")
    # Round-trips through the marker file unchanged.
    assert updater.read_last_update_result() == payload


def test_write_result_caps_error_and_detail(monkeypatch, tmp_path):
    """A multi-MB pip stderr must not bloat the heartbeat payload."""
    monkeypatch.setattr(updater, "_result_path", lambda: tmp_path / "m.json")
    payload = updater._write_result(
        "pip_failed", ok=False, error="x" * 5000, detail="y" * 5000,
    )
    assert len(payload["error"]) <= 1024
    assert len(payload["detail"]) <= 1024


async def test_failure_populates_error_and_detail(monkeypatch, tmp_path):
    """On a pip failure both error (machine) and detail (legacy dashboards)
    carry the reason; old/new versions are equal because nothing landed."""
    marker = tmp_path / "m.json"
    monkeypatch.setattr(updater, "_result_path", lambda: marker)
    monkeypatch.setattr(updater, "_current_base_version", lambda: "0.3.0")

    async def pass_pre(source):
        return True, ""

    async def fail_run(source, timeout_seconds=300.0):
        return False, "ERROR: Could not find a version"

    monkeypatch.setattr(updater, "preflight", pass_pre)
    monkeypatch.setattr(updater, "run_self_update", fail_run)
    monkeypatch.setattr(updater, "restart_for_service_manager", lambda code=0: None)

    await updater.perform_self_update("git+https://x/y")
    res = updater.read_last_update_result()
    assert res["ok"] is False
    assert res["status"] == "pip_failed"
    assert "Could not find a version" in res["error"]
    assert "Could not find a version" in res["detail"]
    assert res["old_version"] == "0.3.0"
    assert res["new_version"] == "0.3.0"  # unchanged: install didn't land


# ---------------------------------------------------------------------------
# Atomic write + safe re-run
# ---------------------------------------------------------------------------

def test_atomic_write_leaves_no_temp_files(monkeypatch, tmp_path):
    """Temp files used for the atomic rename are cleaned up -- only the marker
    remains in the directory."""
    marker = tmp_path / updater._RESULT_FILE_NAME
    monkeypatch.setattr(updater, "_result_path", lambda: marker)
    updater._write_result("ok", ok=True, old_version="0.3.0", new_version="0.4.0")
    names = [p.name for p in tmp_path.iterdir()]
    assert names == [updater._RESULT_FILE_NAME]


def test_safe_rerun_overwrites_cleanly(monkeypatch, tmp_path):
    """Running the result-write twice yields a single, coherent marker -- the
    second result fully replaces the first (no append / no corruption)."""
    marker = tmp_path / updater._RESULT_FILE_NAME
    monkeypatch.setattr(updater, "_result_path", lambda: marker)
    updater._write_result("pip_failed", ok=False, old_version="0.3.0", error="first")
    updater._write_result("ok", ok=True, old_version="0.3.0", new_version="0.4.0")
    # File parses as exactly one JSON object (not concatenated writes).
    text = marker.read_text(encoding="utf-8")
    obj = json.loads(text)
    assert obj["status"] == "ok"
    assert obj["ok"] is True
    # Only the marker remains; no stray temp file from the second write.
    assert [p.name for p in tmp_path.iterdir()] == [updater._RESULT_FILE_NAME]


def test_interrupted_write_keeps_previous_result(monkeypatch, tmp_path):
    """If os.replace fails mid-write (simulated), the PREVIOUS coherent marker
    survives -- an interrupted update never leaves a half-written file."""
    marker = tmp_path / updater._RESULT_FILE_NAME
    monkeypatch.setattr(updater, "_result_path", lambda: marker)
    # First write lands cleanly.
    updater._write_result("ok", ok=True, old_version="0.3.0", new_version="0.4.0")
    good = updater.read_last_update_result()

    # Second write is interrupted at the rename step.
    import os as _os
    real_replace = _os.replace

    def boom_replace(src, dst):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(_os, "replace", boom_replace)
    updater._write_result("pip_failed", ok=False, error="interrupted")  # swallowed
    monkeypatch.setattr(_os, "replace", real_replace)

    # Old result is intact and parseable; no corruption, no leftover temp file.
    after = updater.read_last_update_result()
    assert after == good
    assert [p.name for p in tmp_path.iterdir()] == [updater._RESULT_FILE_NAME]


# ---------------------------------------------------------------------------
# Success vs no-op detection
# ---------------------------------------------------------------------------

async def test_success_when_version_advances(monkeypatch, tmp_path):
    """pip exits 0 AND the on-disk base version advanced -> ok=True, status=ok,
    old/new reflect the bump, and the service restarts."""
    marker = tmp_path / "m.json"
    monkeypatch.setattr(updater, "_result_path", lambda: marker)
    monkeypatch.setattr(updater, "_current_base_version", lambda: "0.3.0")
    monkeypatch.setattr(updater, "_installed_base_version", lambda: "0.4.0")

    async def pass_pre(source):
        return True, ""

    async def ok_run(source, timeout_seconds=300.0):
        return True, ""

    exited = []
    monkeypatch.setattr(updater, "preflight", pass_pre)
    monkeypatch.setattr(updater, "run_self_update", ok_run)
    monkeypatch.setattr(updater, "restart_for_service_manager", lambda code=0: exited.append(code))

    await updater.perform_self_update("git+https://x/y")
    assert exited == [0]
    res = updater.read_last_update_result()
    assert res["ok"] is True
    assert res["status"] == "ok"
    assert res["old_version"] == "0.3.0"
    assert res["new_version"] == "0.4.0"


async def test_no_op_when_version_unchanged(monkeypatch, tmp_path):
    """pip exits 0 but the installed base version is UNCHANGED -> ok=False,
    status='no_op' (so the dashboard doesn't claim a phantom update), yet the
    service still restarts onto the freshly-reinstalled files."""
    marker = tmp_path / "m.json"
    monkeypatch.setattr(updater, "_result_path", lambda: marker)
    monkeypatch.setattr(updater, "_current_base_version", lambda: "0.3.0")
    monkeypatch.setattr(updater, "_installed_base_version", lambda: "0.3.0")

    async def pass_pre(source):
        return True, ""

    async def ok_run(source, timeout_seconds=300.0):
        return True, ""

    exited = []
    monkeypatch.setattr(updater, "preflight", pass_pre)
    monkeypatch.setattr(updater, "run_self_update", ok_run)
    monkeypatch.setattr(updater, "restart_for_service_manager", lambda code=0: exited.append(code))

    await updater.perform_self_update("git+https://x/y")
    res = updater.read_last_update_result()
    assert res["ok"] is False
    assert res["status"] == "no_op"
    assert res["old_version"] == res["new_version"] == "0.3.0"
    # A force-reinstall still rewrote the files, so we DO restart.
    assert exited == [0]


def test_installed_base_version_parses_init(monkeypatch, tmp_path):
    """_installed_base_version reads __base_version__ straight off the on-disk
    __init__.py (not the stale in-process import)."""
    fake_init = tmp_path / "__init__.py"
    fake_init.write_text('__base_version__ = "9.9.9"\n', encoding="utf-8")
    # Point the resolver at our fake package file.
    monkeypatch.setattr(updater, "__file__", str(tmp_path / "updater.py"))
    assert updater._installed_base_version() == "9.9.9"


def test_installed_base_version_empty_when_unreadable(monkeypatch, tmp_path):
    """A missing / unparseable __init__.py -> '' (couldn't confirm), not a
    crash -- callers fall back to the captured old version."""
    monkeypatch.setattr(updater, "__file__", str(tmp_path / "nope" / "updater.py"))
    assert updater._installed_base_version() == ""
