"""Discovery surface: ingest updates subnet status, /discovery renders it, rescan queues a Command."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import schemas as s
from central import services
from central.main import app
from central.security import hash_password


def _login(http: TestClient, username: str, password: str) -> None:
    resp = http.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303


@pytest.fixture()
def env(db):
    """A client + site + agent + one assigned subnet; logged-in tech TestClient."""
    db.add(m.User(
        username="tech", password_hash=hash_password("tech"), role=m.UserRole.tech
    ))
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    agent = m.Agent(site_id=site.id, name="hq-agent", api_key_hash="x")
    db.add(agent)
    db.flush()
    subnet = m.Subnet(
        site_id=site.id, agent_id=agent.id, cidr="10.10.0.0/24",
        snmp_community="public", snmp_version="2c",
    )
    db.add(subnet)
    db.commit()

    http = TestClient(app)
    _login(http, "tech", "tech")
    return {
        "http": http, "site_id": site.id, "agent_id": agent.id, "subnet_id": subnet.id,
    }


def _fresh(db, model, obj_id):
    db.expire_all()
    return db.get(model, obj_id)


def test_update_subnet_discovery_stats_writes_counters(env, db):
    services.update_subnet_discovery_stats(
        db, env["site_id"], "10.10.0.0/24", found=7, new=3
    )
    db.commit()
    sub = _fresh(db, m.Subnet, env["subnet_id"])
    assert sub.last_discovery_at is not None
    assert sub.last_discovery_found_count == 7
    assert sub.last_discovery_new_count == 3


def test_update_subnet_discovery_stats_missing_cidr_is_noop(env, db):
    """Unknown CIDR (agent reporting a subnet not enrolled here) must not error."""
    result = services.update_subnet_discovery_stats(
        db, env["site_id"], "192.168.99.0/24", found=2, new=2
    )
    db.commit()
    assert result is None


def test_apply_discovered_batch_aggregates_per_subnet(env, db):
    """Calling record_discovered for several devices, then a single subnet update,
    captures both new and previously-known counts correctly."""
    agent = db.get(m.Agent, env["agent_id"])
    devices = [
        s.DiscoveredIn(ip="10.10.0.20", subnet_cidr="10.10.0.0/24", hostname="a"),
        s.DiscoveredIn(ip="10.10.0.21", subnet_cidr="10.10.0.0/24", hostname="b"),
        s.DiscoveredIn(ip="10.10.0.22", subnet_cidr="10.10.0.0/24", hostname="c"),
    ]
    new_count = 0
    for d in devices:
        _, was_created = services.record_discovered(db, agent, d)
        new_count += int(was_created)
    services.update_subnet_discovery_stats(
        db, env["site_id"], "10.10.0.0/24", found=len(devices), new=new_count
    )
    db.commit()
    sub = _fresh(db, m.Subnet, env["subnet_id"])
    assert sub.last_discovery_found_count == 3
    assert sub.last_discovery_new_count == 3
    # A second batch where two are already known should keep found=3 but new=1.
    devices2 = [
        s.DiscoveredIn(ip="10.10.0.20", subnet_cidr="10.10.0.0/24"),
        s.DiscoveredIn(ip="10.10.0.21", subnet_cidr="10.10.0.0/24"),
        s.DiscoveredIn(ip="10.10.0.99", subnet_cidr="10.10.0.0/24"),
    ]
    new2 = 0
    for d in devices2:
        _, was_created = services.record_discovered(db, agent, d)
        new2 += int(was_created)
    services.update_subnet_discovery_stats(
        db, env["site_id"], "10.10.0.0/24", found=len(devices2), new=new2
    )
    db.commit()
    sub = _fresh(db, m.Subnet, env["subnet_id"])
    assert sub.last_discovery_found_count == 3
    assert sub.last_discovery_new_count == 1


def test_discovery_page_shows_subnet_status(env, db):
    services.update_subnet_discovery_stats(
        db, env["site_id"], "10.10.0.0/24", found=4, new=2
    )
    db.commit()
    resp = env["http"].get("/discovery")
    assert resp.status_code == 200
    body = resp.text
    assert "hq-agent" in body
    assert "10.10.0.0/24" in body
    # found / new counters and an action button.
    assert ">4<" in body or "tabular-nums\">4<" in body
    assert "Rescan now" in body


def test_discovery_page_forbidden_for_client_readonly(db):
    client = m.Client(name="C")
    db.add(client)
    db.flush()
    db.add(m.User(
        username="ro", password_hash=hash_password("ro"),
        role=m.UserRole.client_readonly, client_id=client.id,
    ))
    db.commit()
    http = TestClient(app)
    _login(http, "ro", "ro")
    resp = http.get("/discovery", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_rescan_enqueues_command(env, db):
    resp = env["http"].post(
        f"/discovery/agents/{env['agent_id']}/rescan", follow_redirects=False
    )
    assert resp.status_code == 303
    cmd = db.scalar(select(m.Command).where(m.Command.agent_id == env["agent_id"]))
    assert cmd is not None
    assert cmd.type == m.CommandType.rescan
    assert cmd.status == m.CommandStatus.pending


def test_subnet_label_edit_persists(env, db):
    resp = env["http"].post(
        f"/manage/subnets/{env['subnet_id']}",
        data={"label": "Office VLAN 10", "snmp_community": "", "snmp_version": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    sub = _fresh(db, m.Subnet, env["subnet_id"])
    assert sub.label == "Office VLAN 10"
    # Empty snmp fields don't overwrite stored creds.
    assert sub.snmp_community == "public"
    assert sub.snmp_version == "2c"


def test_subnet_label_edit_changes_snmp_when_present(env, db):
    env["http"].post(
        f"/manage/subnets/{env['subnet_id']}",
        data={"label": "X", "snmp_community": "newcomm", "snmp_version": "1"},
        follow_redirects=False,
    )
    sub = _fresh(db, m.Subnet, env["subnet_id"])
    assert sub.snmp_community == "newcomm"
    assert sub.snmp_version == "1"


def test_approvals_page_shows_discovered_by(env, db):
    pending = m.Printer(
        client_id=db.scalar(select(m.Client.id)),
        site_id=env["site_id"],
        ip="10.10.0.55",
        discovery_state=m.DiscoveryState.pending,
        discovered_by_agent_id=env["agent_id"],
    )
    db.add(pending)
    db.commit()
    resp = env["http"].get("/approvals")
    assert resp.status_code == 200
    # Surfaces the agent name and the "Discovered by" header.
    assert "hq-agent" in resp.text
    assert "Discovered by" in resp.text
