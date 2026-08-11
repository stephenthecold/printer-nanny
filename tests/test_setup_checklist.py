"""Guided setup reports real state and keeps exceptions honest."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import runtime
from central import setup_checklist
from central.main import app
from central.security import hash_password


NOW = datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc)


def _user(db, username="admin", role=m.UserRole.admin):
    row = m.User(
        username=username,
        password_hash=hash_password("pw12345678"),
        role=role,
    )
    db.add(row)
    db.commit()
    return row


def _login(username="admin"):
    cli = TestClient(app)
    cli.post(
        "/login",
        data={"username": username, "password": "pw12345678"},
        follow_redirects=False,
    )
    return cli


def _location(db):
    client = m.Client(name="LCR")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="Downtown")
    db.add(site)
    db.commit()
    return client, site


def test_empty_install_points_to_the_first_client_and_notifications(db):
    status = setup_checklist.build_setup_status(db, NOW)
    assert status["required_total"] == 2
    assert status["open_count"] == 2
    assert not status["has_clients"]
    assert status["global_steps"][0]["state"] == "open"


def test_notification_step_requires_a_real_destination_not_a_dry_run(db):
    runtime.save_settings(
        db,
        {
            "email.enabled": True,
            "email.default_recipients": "tech@example.com",
            "smtp.host": "",
            "smtp.from": "",
        },
    )
    db.commit()
    assert setup_checklist.build_setup_status(db, NOW)["global_steps"][0]["state"] == "open"

    runtime.save_settings(
        db,
        {"smtp.host": "smtp.example.com", "smtp.from": "alerts@example.com"},
    )
    db.commit()
    assert (
        setup_checklist.build_setup_status(db, NOW)["global_steps"][0]["state"]
        == "complete"
    )


def test_site_requirements_come_from_real_fleet_objects(db):
    client, site = _location(db)
    agent = m.Agent(
        site_id=site.id,
        name="Downtown agent",
        api_key_hash="x" * 64,
        status=m.AgentStatus.online,
        last_heartbeat=NOW - timedelta(minutes=1),
    )
    db.add(agent)
    db.flush()
    db.add(m.Subnet(site_id=site.id, agent_id=agent.id, cidr="10.20.0.0/24"))
    db.add(m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip="10.20.0.10",
        discovery_state=m.DiscoveryState.approved,
        last_seen=NOW - timedelta(minutes=2),
    ))
    db.commit()

    status = setup_checklist.build_setup_status(db, NOW)
    row = status["clients"][0]["sites"][0]
    assert [step["state"] for step in row["steps"]] == [
        "complete", "complete", "complete"
    ]
    assert row["verified"] is True
    assert status["verification_attention"] == 0


def test_snmpv3_subnet_stays_required_until_its_credentials_are_complete(db):
    _, site = _location(db)
    subnet = m.Subnet(
        site_id=site.id,
        cidr="10.30.0.0/24",
        snmp_version="3",
        snmp_v3=None,
    )
    db.add(subnet)
    db.commit()

    row = setup_checklist.build_setup_status(db, NOW)["clients"][0]["sites"][0]
    network = row["steps"][0]
    assert network["state"] == "open"
    assert network["label"] == "Finish network credentials"

    subnet.snmp_v3 = {"user": "poller", "security_level": "noAuthNoPriv"}
    db.commit()
    row = setup_checklist.build_setup_status(db, NOW)["clients"][0]["sites"][0]
    assert row["steps"][0]["state"] == "complete"


def test_bypass_advances_configuration_but_never_fakes_verification(db):
    _, site = _location(db)
    db.add(m.SetupBypass(
        key=setup_checklist.bypass_key(setup_checklist.STEP_AGENT, site.id),
        site_id=site.id,
        step=setup_checklist.STEP_AGENT,
        reason="Temporary manual polling",
    ))
    db.commit()

    status = setup_checklist.build_setup_status(db, NOW)
    row = status["clients"][0]["sites"][0]
    agent_step = next(step for step in row["steps"] if step["key"] == "agent")
    assert agent_step["state"] == "bypassed"
    assert row["verified"] is False
    assert status["bypassed_count"] == 1
    assert status["verification_attention"] == 1


def test_manager_can_bypass_and_clear_a_site_requirement(db):
    _user(db)
    _, site = _location(db)
    cli = _login()
    response = cli.post(
        "/manage/setup/bypass",
        data={"step": "printer", "site_id": site.id, "reason": "Not delivered yet"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = db.scalar(select(m.SetupBypass))
    assert row is not None and row.reason == "Not delivered yet"

    response = cli.post(
        f"/manage/setup/bypasses/{row.id}/clear", follow_redirects=False
    )
    assert response.status_code == 303
    assert not list(db.scalars(select(m.SetupBypass)))
    actions = [a.action for a in db.scalars(select(m.AuditLog))]
    assert "setup.bypass" in actions
    assert "setup.bypass_clear" in actions


def test_invalid_scope_and_readonly_user_cannot_create_bypasses(db):
    _user(db, "viewer", m.UserRole.client_readonly)
    _, site = _location(db)
    viewer = _login("viewer")
    viewer.post(
        "/manage/setup/bypass",
        data={"step": "agent", "site_id": site.id, "reason": "No"},
        follow_redirects=False,
    )
    assert not list(db.scalars(select(m.SetupBypass)))

    _user(db, "admin")
    admin = _login("admin")
    admin.post(
        "/manage/setup/bypass",
        data={"step": "notifications", "site_id": site.id, "reason": "Wrong scope"},
        follow_redirects=False,
    )
    admin.post(
        "/manage/setup/bypass",
        data={"step": "agent", "site_id": 999999, "reason": "Missing site"},
        follow_redirects=False,
    )
    assert not list(db.scalars(select(m.SetupBypass)))


def test_dashboard_reminder_links_to_guided_setup(db):
    _user(db)
    body = _login().get("/").text
    assert "Setup needs attention" in body
    assert 'href="/manage/onboard"' in body
