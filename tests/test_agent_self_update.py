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


def test_update_all_enqueues_per_agent(db):
    a1 = _seed_agent(db)
    # second agent under same site
    a2 = m.Agent(
        site_id=a1.site_id, name="branch-agent",
        api_key_hash=hash_api_key("pn_key2"),
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


async def test_perform_self_update_skips_when_no_pip_source(monkeypatch):
    """A malformed command (payload without pip_source) must not crash the
    agent -- it logs and returns."""
    from printer_nanny_agent.updater import perform_self_update
    # Should be a no-op; no exception, no exit.
    await perform_self_update(None)
    await perform_self_update("")


async def test_perform_self_update_does_not_exit_on_pip_failure(monkeypatch):
    """When pip fails (network blip, broken package), the agent stays up on
    the OLD code rather than exiting and getting stuck in a restart loop on
    a venv that no longer installs."""
    from printer_nanny_agent import updater

    async def fake_run(pip_source, timeout_seconds=300.0):
        return False  # pip failed

    exited = []

    def fake_exit(code=0):
        exited.append(code)
        raise SystemExit(code)  # would normally be os._exit

    monkeypatch.setattr(updater, "run_self_update", fake_run)
    monkeypatch.setattr(updater, "restart_for_service_manager", fake_exit)

    # Should NOT call restart_for_service_manager.
    await updater.perform_self_update("git+https://x/y")
    assert exited == []


async def test_perform_self_update_exits_on_pip_success(monkeypatch):
    """Happy path: pip install succeeded, agent calls restart_for_service_manager."""
    from printer_nanny_agent import updater

    async def fake_run(pip_source, timeout_seconds=300.0):
        return True

    exited = []

    def fake_exit(code=0):
        exited.append(code)

    monkeypatch.setattr(updater, "run_self_update", fake_run)
    monkeypatch.setattr(updater, "restart_for_service_manager", fake_exit)
    await updater.perform_self_update("git+https://x/y")
    assert exited == [0]
