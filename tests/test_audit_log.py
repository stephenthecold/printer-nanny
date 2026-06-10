"""Audit trail: rows written at security boundaries, admin-only viewer.

Invariant checked throughout: secret VALUES never appear in audit rows --
only key names / object references.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import runtime
from central.audit import record
from central.main import app
from central.security import hash_password


def _mk_user(db, username: str, role: m.UserRole, password: str = "pw12345678") -> m.User:
    user = m.User(username=username, password_hash=hash_password(password), role=role)
    db.add(user)
    db.commit()
    return user


def _login(username: str, password: str = "pw12345678") -> TestClient:
    cli = TestClient(app)
    cli.post("/login", data={"username": username, "password": password},
             follow_redirects=False)
    return cli


def _rows(db, action: str) -> list[m.AuditLog]:
    return list(db.scalars(select(m.AuditLog).where(m.AuditLog.action == action)))


# ---------- login / logout ----------

def test_login_success_and_failure_are_audited(db):
    _mk_user(db, "admin", m.UserRole.admin)
    cli = TestClient(app)
    # Failure first: wrong password.
    cli.post("/login", data={"username": "admin", "password": "wrong"},
             follow_redirects=False)
    failed = _rows(db, "login.failed")
    assert len(failed) == 1
    assert failed[0].username == "admin"
    assert failed[0].user_id is None  # pre-auth: no user attached
    # The submitted password must never be recorded anywhere.
    assert "wrong" not in (failed[0].detail or "")
    # Success.
    cli.post("/login", data={"username": "admin", "password": "pw12345678"},
             follow_redirects=False)
    ok = _rows(db, "login")
    assert len(ok) == 1
    assert ok[0].username == "admin"
    assert ok[0].user_id is not None


def test_logout_is_audited(db):
    _mk_user(db, "admin", m.UserRole.admin)
    cli = _login("admin")
    cli.get("/logout", follow_redirects=False)
    assert len(_rows(db, "logout")) == 1


# ---------- settings ----------

def test_settings_save_logs_changed_keys_but_never_values(db):
    _mk_user(db, "admin", m.UserRole.admin)
    cli = _login("admin")
    # The grouped settings page posts one group at a time; SMTP lives under
    # the notifications group.
    resp = cli.post("/settings", data={
        "_group": "notifications",
        "smtp.host": "mail.example.com",
        "smtp.password": "super-secret-value",
    }, follow_redirects=False)
    assert resp.status_code == 303
    rows = _rows(db, "settings.update")
    assert len(rows) == 1
    detail = rows[0].detail or ""
    assert "smtp.host" in detail
    assert "smtp.password" in detail        # key NAME is fine
    assert "super-secret-value" not in detail  # value is not
    assert "mail.example.com" not in detail    # no values at all


def test_settings_save_without_changes_logs_nothing(db):
    _mk_user(db, "admin", m.UserRole.admin)
    cli = _login("admin")
    # Save twice with the same content: second save changes nothing.
    cli.post("/settings", data={"_group": "notifications",
                                "smtp.host": "mail.example.com"}, follow_redirects=False)
    cli.post("/settings", data={"_group": "notifications",
                                "smtp.host": "mail.example.com"}, follow_redirects=False)
    assert len(_rows(db, "settings.update")) == 1


# ---------- user management ----------

def test_user_lifecycle_is_audited(db):
    _mk_user(db, "admin", m.UserRole.admin)
    cli = _login("admin")
    cli.post("/manage/users", data={
        "username": "tech2", "email": "", "role": "tech", "client_id": "",
        "password": "techpass123",
    }, follow_redirects=False)
    created = _rows(db, "user.create")
    assert len(created) == 1
    assert created[0].target == "user:tech2"
    assert created[0].detail == "role=tech"
    assert "techpass123" not in str(created[0].detail)

    target = db.scalar(select(m.User).where(m.User.username == "tech2"))
    cli.post(f"/manage/users/{target.id}/reset-password",
             data={"new_password": "newpass12345"}, follow_redirects=False)
    reset = _rows(db, "user.reset_password")
    assert len(reset) == 1
    assert "newpass12345" not in str(reset[0].detail or "")

    cli.post(f"/manage/users/{target.id}/delete", follow_redirects=False)
    deleted = _rows(db, "user.delete")
    assert len(deleted) == 1
    assert deleted[0].target == "user:tech2"
    assert deleted[0].username == "admin"  # the actor, not the victim


# ---------- agents / printers ----------

def _seed_client_site(db):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.commit()
    return client, site


def test_agent_create_and_update_queue_are_audited(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _client, site = _seed_client_site(db)
    cli = _login("admin")
    cli.post("/manage/agents", data={"site_id": str(site.id), "name": "hq-agent"},
             follow_redirects=False)
    assert len(_rows(db, "agent.create")) == 1

    agent = db.scalar(select(m.Agent))
    runtime.save_settings(db, {
        "agent.pip_source": "git+https://github.com/stephenthecold/printer-nanny.git#subdirectory=agent",
    })
    cli.post(f"/manage/agents/{agent.id}/update", follow_redirects=False)
    queued = _rows(db, "agent.update_queued")
    assert len(queued) == 1
    assert "github.com" in (queued[0].detail or "")


def test_approval_action_is_audited(db):
    _mk_user(db, "admin", m.UserRole.admin)
    client, site = _seed_client_site(db)
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.9",
        discovery_state=m.DiscoveryState.pending,
    )
    db.add(printer)
    db.commit()
    cli = _login("admin")
    cli.post(f"/approvals/{printer.id}/approve", follow_redirects=False)
    rows = _rows(db, "printer.approve")
    assert len(rows) == 1
    assert "10.0.0.9" in rows[0].target


def test_subnet_create_audited_without_passwords(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _client, site = _seed_client_site(db)
    agent = m.Agent(site_id=site.id, name="hq", api_key_hash="x")
    db.add(agent)
    db.commit()
    cli = _login("admin")
    cli.post(f"/manage/agents/{agent.id}/subnets", data={
        "cidr": "10.0.5.0/24", "snmp_community": "s3cret-community",
        "snmp_version": "3",
        "snmp_v3_user": "noc", "snmp_v3_security_level": "authPriv",
        "snmp_v3_auth_protocol": "SHA256", "snmp_v3_auth_password": "usm-auth-pw",
        "snmp_v3_priv_protocol": "AES128", "snmp_v3_priv_password": "usm-priv-pw",
    }, follow_redirects=False)
    rows = _rows(db, "subnet.create")
    assert len(rows) == 1
    blob = f"{rows[0].target} {rows[0].detail}"
    assert "10.0.5.0/24" in blob
    # Credentials must never appear in audit rows.
    assert "usm-auth-pw" not in blob
    assert "usm-priv-pw" not in blob
    assert "s3cret-community" not in blob


# ---------- viewer page ----------

def test_audit_page_admin_only(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _mk_user(db, "techie", m.UserRole.tech)
    admin_cli = _login("admin")
    resp = admin_cli.get("/manage/audit", follow_redirects=False)
    assert resp.status_code == 200
    assert "Audit trail" in resp.text

    tech_cli = _login("techie")
    resp = tech_cli.get("/manage/audit", follow_redirects=False)
    assert resp.status_code == 303  # bounced away


def test_audit_page_renders_rows_and_filters(db):
    admin = _mk_user(db, "admin", m.UserRole.admin)
    record(db, None, admin, "user.create", target="user:alice")
    record(db, None, admin, "agent.delete", target="agent:7 old-agent")
    db.commit()
    cli = _login("admin")
    page = cli.get("/manage/audit", follow_redirects=False).text
    assert "user.create" in page
    assert "agent.delete" in page
    filtered = cli.get("/manage/audit?q=agent.delete", follow_redirects=False).text
    assert "agent.delete" in filtered
    assert "user:alice" not in filtered


# ---------- helper robustness ----------

def test_record_never_raises_on_bad_input(db):
    # No request, no user, oversized strings -- still must not raise.
    record(db, None, None, "x" * 500, target="t" * 1000, detail="d" * 10000)
    db.commit()
    row = db.scalar(select(m.AuditLog))
    assert len(row.action) <= 80
    assert len(row.target) <= 300
    assert len(row.detail) <= 4000
