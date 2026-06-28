"""Self-update: operator clicks Update on /manage/agents, central enqueues an
update_agent command with the configured pip_source, agent picks it up on
heartbeat, runs pip install, exits cleanly so the service manager restarts.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import runtime
from central.main import app
from central.security import hash_api_key, hash_password
from printer_nanny_agent.runner import handle_commands


def _admin_http(db) -> TestClient:
    db.add(m.User(
        username="admin", password_hash=hash_password("admin"), role=m.UserRole.admin,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    return cli


def _seed_agent(db):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    agent = m.Agent(
        site_id=site.id, name="hq-agent",
        api_key_hash=hash_api_key("pn_key"),
    )
    db.add(agent)
    db.commit()
    return agent


# ---- Central UI ----

def test_update_endpoint_requires_real_pip_source(db):
    """The 'your-org' placeholder must be rejected -- pip would otherwise fail
    on every agent simultaneously and the operator would have no idea why."""
    agent = _seed_agent(db)
    runtime.save_settings(db, {
        "agent.pip_source": "git+https://github.com/your-org/printer-nanny.git#subdirectory=agent",
    })
    http = _admin_http(db)
    resp = http.post(f"/manage/agents/{agent.id}/update", follow_redirects=False)
    assert resp.status_code == 303
    # No command got queued.
    cmds = list(db.scalars(select(m.Command).where(m.Command.agent_id == agent.id)))
    assert cmds == []


def test_update_endpoint_enqueues_command(db):
    agent = _seed_agent(db)
    pip_src = "git+https://github.com/stephenthecold/printer-nanny.git#subdirectory=agent"
    runtime.save_settings(db, {"agent.pip_source": pip_src})
    http = _admin_http(db)
    resp = http.post(f"/manage/agents/{agent.id}/update", follow_redirects=False)
    assert resp.status_code == 303
    cmds = list(db.scalars(select(m.Command).where(m.Command.agent_id == agent.id)))
    assert len(cmds) == 1
    assert cmds[0].type == m.CommandType.update_agent
    assert cmds[0].status == m.CommandStatus.pending
    assert cmds[0].payload == {"pip_source": pip_src}


def test_update_all_enqueues_per_outdated_agent(db):
    """The bulk action queues one update_agent per OUTDATED agent. Agents that
    are already current (or never reported a version) are skipped -- see
    test_agent_update_ui for the full scoping matrix. Both agents here report
    an old base, so both get queued (one command each)."""
    a1 = _seed_agent(db)
    a1.version = "0.1.0+20250101-000000"  # outdated
    # second agent under same site, also outdated
    a2 = m.Agent(
        site_id=a1.site_id, name="branch-agent",
        api_key_hash=hash_api_key("pn_key2"),
        version="0.2.0+20250101-000000",
    )
    db.add(a2)
    db.commit()
    pip_src = "git+https://github.com/stephenthecold/printer-nanny.git#subdirectory=agent"
    runtime.save_settings(db, {"agent.pip_source": pip_src})
    http = _admin_http(db)
    resp = http.post("/manage/agents/update-all", follow_redirects=False)
    assert resp.status_code == 303
    cmds = list(db.scalars(select(m.Command).where(m.Command.type == m.CommandType.update_agent)))
    assert sorted(c.agent_id for c in cmds) == sorted([a1.id, a2.id])


def test_update_all_requires_admin(db):
    """Tech users can manage day-to-day but pushing code to every agent at
    once is admin-only -- one fat-finger could brick a fleet."""
    _seed_agent(db)
    # Re-seed a tech user; admin from _admin_http is fine for routing but we
    # want a tech-role session here.
    db.add(m.User(
        username="techie", password_hash=hash_password("t"), role=m.UserRole.tech,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "techie", "password": "t"}, follow_redirects=False)
    resp = cli.post("/manage/agents/update-all", follow_redirects=False)
    assert resp.status_code == 303
    cmds = list(db.scalars(select(m.Command).where(m.Command.type == m.CommandType.update_agent)))
    assert cmds == [], "tech must not be able to mass-update agents"


# ---- Agent dispatch ----

async def test_agent_handle_update_agent_calls_perform_self_update(monkeypatch):
    """handle_commands routes update_agent to perform_self_update with the
    pip_source from the command payload."""
    from printer_nanny_agent import updater

    captured: list[str] = []

    async def fake(pip_source):
        captured.append(pip_source)
        # Don't actually exit/install.

    monkeypatch.setattr(updater, "perform_self_update", fake)
    # The handle_commands branch imports lazily, so patching the module
    # attribute is enough.

    class _StubClient:
        async def aclose(self): pass

    class _StubBackend:
        async def close(self): pass

    cmds = [
        {"id": 1, "type": "update_agent",
         "payload": {"pip_source": "git+https://x/y"}}
    ]
    await handle_commands(_StubClient(), _StubBackend(), object(), cmds)
    assert captured == ["git+https://x/y"]


async def test_perform_self_update_skips_when_no_pip_source(monkeypatch, tmp_path):
    """A malformed command (payload without pip_source) must not crash the
    agent -- it logs, records a clear failure result, and returns."""
    from printer_nanny_agent import updater

    marker = tmp_path / "m.json"
    monkeypatch.setattr(updater, "_result_path", lambda: marker)
    exited = []
    monkeypatch.setattr(updater, "restart_for_service_manager", lambda code=0: exited.append(code))

    # Should be a no-op; no exception, no exit -- but a recorded failure so the
    # operator can see WHY the update did nothing.
    await updater.perform_self_update(None)
    assert exited == []
    res = updater.read_last_update_result()
    assert res["ok"] is False
    assert res["status"] == "no_pip_source"

    await updater.perform_self_update("")
    assert exited == []
    res = updater.read_last_update_result()
    assert res["status"] == "no_pip_source"


async def test_perform_self_update_does_not_exit_on_pip_failure(monkeypatch, tmp_path):
    """When pip fails (network blip, broken package), the agent stays up on
    the OLD code rather than exiting and getting stuck in a restart loop on
    a venv that no longer installs.

    Also records the failure detail in the result marker so the next heartbeat
    surfaces it on the dashboard.
    """
    from printer_nanny_agent import updater

    async def fake_run(pip_source, timeout_seconds=300.0):
        return False, "pip exit 1: connection refused"

    exited = []

    def fake_exit(code=0):
        exited.append(code)
        raise SystemExit(code)  # would normally be os._exit

    async def pass_preflight(pip_source):
        return True, ""

    marker = tmp_path / "m.json"
    monkeypatch.setattr(updater, "_result_path", lambda: marker)
    # Pre-flight is exercised separately; here we isolate the INSTALL path so a
    # CI host without pip next to its python doesn't short-circuit before pip.
    monkeypatch.setattr(updater, "preflight", pass_preflight)
    monkeypatch.setattr(updater, "run_self_update", fake_run)
    monkeypatch.setattr(updater, "restart_for_service_manager", fake_exit)

    await updater.perform_self_update("git+https://x/y")
    assert exited == []  # didn't restart
    result = updater.read_last_update_result()
    assert result["status"] == "pip_failed"
    assert result["ok"] is False
    assert "connection refused" in result["detail"]
    assert "connection refused" in result["error"]


async def test_perform_self_update_exits_on_pip_success(monkeypatch, tmp_path):
    """Happy path: pip install succeeded -> marker records 'ok' -> agent
    calls restart_for_service_manager."""
    from printer_nanny_agent import updater

    async def fake_run(pip_source, timeout_seconds=300.0):
        return True, ""

    exited = []

    def fake_exit(code=0):
        exited.append(code)

    async def pass_preflight(pip_source):
        return True, ""

    marker = tmp_path / "m.json"
    monkeypatch.setattr(updater, "_result_path", lambda: marker)
    monkeypatch.setattr(updater, "preflight", pass_preflight)
    monkeypatch.setattr(updater, "run_self_update", fake_run)
    # Simulate a version bump landing on disk so the post-install check sees a
    # genuine update (new != old) and records 'ok' rather than 'no_op'.
    monkeypatch.setattr(updater, "_current_base_version", lambda: "0.3.0")
    monkeypatch.setattr(updater, "_installed_base_version", lambda: "0.4.0")
    monkeypatch.setattr(updater, "restart_for_service_manager", fake_exit)
    await updater.perform_self_update("git+https://x/y")
    assert exited == [0]
    result = updater.read_last_update_result()
    assert result["status"] == "ok"
    assert result["ok"] is True
    assert result["old_version"] == "0.3.0"
    assert result["new_version"] == "0.4.0"
    assert result["error"] is None
