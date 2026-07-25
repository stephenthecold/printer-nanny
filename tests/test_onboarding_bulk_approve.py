"""Bulk approve/ignore on the discovery queue.

The posted id list is attacker-controllable, so the interesting cases are the
ones where the form and the database disagree: ids that don't exist, ids for
printers somebody already decided on, and a double-submit.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
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


def _pending(db, n: int = 3):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printers = [
        m.Printer(client_id=client.id, site_id=site.id, ip=f"10.0.0.{i + 10}",
                  discovery_state=m.DiscoveryState.pending,
                  status=m.PrinterStatus.unknown)
        for i in range(n)
    ]
    db.add_all(printers)
    db.commit()
    return printers


def test_bulk_approve_moves_every_selected_device(db):
    _mk_user(db, "admin", m.UserRole.admin)
    printers = _pending(db, 3)
    cli = _login("admin")
    cli.post("/approvals/bulk",
             data={"printer_ids": [p.id for p in printers], "action": "approve"},
             follow_redirects=False)
    db.expire_all()
    assert all(
        db.get(m.Printer, p.id).discovery_state == m.DiscoveryState.approved
        for p in printers
    )


def test_bulk_ignore_moves_every_selected_device(db):
    _mk_user(db, "admin", m.UserRole.admin)
    printers = _pending(db, 2)
    cli = _login("admin")
    cli.post("/approvals/bulk",
             data={"printer_ids": [p.id for p in printers], "action": "ignore"},
             follow_redirects=False)
    db.expire_all()
    assert all(
        db.get(m.Printer, p.id).discovery_state == m.DiscoveryState.ignored
        for p in printers
    )


def test_only_selected_devices_move(db):
    _mk_user(db, "admin", m.UserRole.admin)
    printers = _pending(db, 3)
    cli = _login("admin")
    cli.post("/approvals/bulk",
             data={"printer_ids": [printers[0].id], "action": "approve"},
             follow_redirects=False)
    db.expire_all()
    assert db.get(m.Printer, printers[0].id).discovery_state == m.DiscoveryState.approved
    assert db.get(m.Printer, printers[1].id).discovery_state == m.DiscoveryState.pending
    assert db.get(m.Printer, printers[2].id).discovery_state == m.DiscoveryState.pending


def test_already_decided_devices_are_not_reopened(db):
    """A stale form (or a replayed submit) must not flip a device somebody has
    since ignored back to approved."""
    _mk_user(db, "admin", m.UserRole.admin)
    printers = _pending(db, 2)
    printers[0].discovery_state = m.DiscoveryState.ignored
    db.commit()
    cli = _login("admin")
    cli.post("/approvals/bulk",
             data={"printer_ids": [p.id for p in printers], "action": "approve"},
             follow_redirects=False)
    db.expire_all()
    assert db.get(m.Printer, printers[0].id).discovery_state == m.DiscoveryState.ignored
    assert db.get(m.Printer, printers[1].id).discovery_state == m.DiscoveryState.approved


def test_unknown_ids_are_skipped_not_fatal(db):
    _mk_user(db, "admin", m.UserRole.admin)
    printers = _pending(db, 1)
    cli = _login("admin")
    resp = cli.post("/approvals/bulk",
                    data={"printer_ids": [printers[0].id, 99999], "action": "approve"},
                    follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(m.Printer, printers[0].id).discovery_state == m.DiscoveryState.approved


def test_bad_action_changes_nothing(db):
    _mk_user(db, "admin", m.UserRole.admin)
    printers = _pending(db, 2)
    cli = _login("admin")
    cli.post("/approvals/bulk",
             data={"printer_ids": [p.id for p in printers], "action": "delete"},
             follow_redirects=False)
    db.expire_all()
    assert all(
        db.get(m.Printer, p.id).discovery_state == m.DiscoveryState.pending
        for p in printers
    )


def test_bulk_action_is_audited_with_the_devices_it_touched(db):
    _mk_user(db, "admin", m.UserRole.admin)
    printers = _pending(db, 2)
    cli = _login("admin")
    cli.post("/approvals/bulk",
             data={"printer_ids": [p.id for p in printers], "action": "approve"},
             follow_redirects=False)
    rows = list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "printer.bulk_approve")
    ))
    assert len(rows) == 1
    assert "2 printers" in rows[0].target
    for p in printers:
        assert p.ip in rows[0].detail
    assert rows[0].username == "admin"


def test_client_readonly_cannot_bulk_approve(db):
    _mk_user(db, "viewer", m.UserRole.client_readonly)
    printers = _pending(db, 1)
    cli = _login("viewer")
    cli.post("/approvals/bulk",
             data={"printer_ids": [printers[0].id], "action": "approve"},
             follow_redirects=False)
    db.expire_all()
    assert db.get(m.Printer, printers[0].id).discovery_state == m.DiscoveryState.pending


def test_anonymous_cannot_bulk_approve(db):
    printers = _pending(db, 1)
    cli = TestClient(app)
    cli.post("/approvals/bulk",
             data={"printer_ids": [printers[0].id], "action": "approve"},
             follow_redirects=False)
    db.expire_all()
    assert db.get(m.Printer, printers[0].id).discovery_state == m.DiscoveryState.pending


def test_approvals_page_renders_bulk_controls_and_safe_buttons(db):
    _mk_user(db, "admin", m.UserRole.admin)
    printers = _pending(db, 1)
    cli = _login("admin")
    body = cli.get("/approvals").text
    assert 'id="bulk-form"' in body
    assert f'value="{printers[0].id}"' in body
    # Row buttons must be type="button" or clicking one submits the whole batch.
    assert 'type="button" hx-post="/approvals/' in body


def test_device_supplied_hostname_is_escaped_not_executed(db):
    """Hostnames come off the wire from devices on a client LAN."""
    _mk_user(db, "admin", m.UserRole.admin)
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    db.add(m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.5",
        hostname="<script>alert(1)</script>",
        discovery_state=m.DiscoveryState.pending, status=m.PrinterStatus.unknown,
    ))
    db.commit()
    body = _login("admin").get("/approvals").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
