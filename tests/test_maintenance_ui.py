"""Maintenance schedule UI: CRUD, mark-serviced rolls next_due, worker
alert auto-resolves on the cycle after the roll forward.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central.main import app
from central.security import hash_password
from central.worker import jobs


def _admin(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("pw"),
                  role=m.UserRole.admin))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"},
             follow_redirects=False)
    return cli


def _seed_printer(db):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.10",
        brand="HP", model="M404", display_name="Front Desk",
        status=m.PrinterStatus.ok,
        discovery_state=m.DiscoveryState.approved, page_count=20000,
    )
    db.add(printer)
    db.commit()
    return printer


def test_schedule_create_and_list(db):
    printer = _seed_printer(db)
    cli = _admin(db)
    resp = cli.post("/manage/maintenance/schedules", data={
        "name": "Quarterly clean",
        "printer_id": str(printer.id),
        "interval_days": "90",
        "page_threshold": "",
        "next_due": "2026-09-01",
    }, follow_redirects=False)
    assert resp.status_code == 303
    sched = db.scalar(select(m.MaintenanceSchedule))
    assert sched is not None
    assert sched.name == "Quarterly clean"
    assert sched.printer_id == printer.id
    assert sched.interval_days == 90
    body = cli.get("/manage/maintenance").text
    assert "Quarterly clean" in body
    assert "Front Desk" in body


def test_schedule_create_fleet_wide_by_model(db):
    _seed_printer(db)
    cli = _admin(db)
    cli.post("/manage/maintenance/schedules", data={
        "name": "Fuser kit",
        "printer_id": "",
        "model": "M404",
        "interval_days": "",
        "page_threshold": "50000",
        "next_due": "",
    }, follow_redirects=False)
    sched = db.scalar(select(m.MaintenanceSchedule))
    assert sched.model == "M404"
    assert sched.printer_id is None
    assert sched.page_threshold == 50000


def test_mark_serviced_rolls_next_due_and_records_entry(db):
    printer = _seed_printer(db)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    sched = m.MaintenanceSchedule(
        name="Quarterly clean", printer_id=printer.id,
        interval_days=90, next_due=past,
    )
    db.add(sched)
    db.commit()
    cli = _admin(db)
    resp = cli.post(
        f"/manage/maintenance/schedules/{sched.id}/log",
        data={"performed_by": "Stephen", "notes": "all good"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(sched)
    # SQLite hands back naive datetimes; treat as UTC for the comparison.
    nd = sched.next_due
    if nd.tzinfo is None:
        nd = nd.replace(tzinfo=timezone.utc)
    assert nd > datetime.now(timezone.utc)
    rec = db.scalar(select(m.MaintenanceRecord))
    assert rec.performed_by == "Stephen"
    # Notes contain the operator's text plus a "(schedule #N)" suffix the
    # handler appends so audits can trace records back to their schedule.
    assert "all good" in (rec.notes or "")
    assert "(schedule #" in (rec.notes or "")


def test_logging_service_resolves_open_maintenance_alert(db):
    """End-to-end: alert fires (via the worker) -> operator logs service ->
    next worker cycle resolves the alert because next_due rolled forward."""
    printer = _seed_printer(db)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    sched = m.MaintenanceSchedule(
        name="Quarterly clean", printer_id=printer.id,
        interval_days=90, next_due=past,
    )
    db.add(sched)
    db.commit()
    jobs.check_maintenance_due(db)
    alert = db.scalar(select(m.Alert).where(
        m.Alert.type == m.AlertConditionType.maintenance_due,
        m.Alert.state == m.AlertState.open,
    ))
    assert alert is not None
    # Operator marks serviced -> next_due rolls forward.
    cli = _admin(db)
    cli.post(f"/manage/maintenance/schedules/{sched.id}/log",
             data={"performed_by": "tech"}, follow_redirects=False)
    # Next worker cycle reconciles: the schedule is no longer due.
    jobs.check_maintenance_due(db)
    db.refresh(alert)
    assert alert.state == m.AlertState.resolved
    assert alert.resolved_at is not None


def test_schedule_delete_admin_only_for_records(db):
    _seed_printer(db)
    sched = m.MaintenanceSchedule(name="X", interval_days=30)
    db.add(sched)
    db.commit()
    cli = _admin(db)
    cli.post(f"/manage/maintenance/schedules/{sched.id}/delete",
             follow_redirects=False)
    assert db.scalar(select(m.MaintenanceSchedule)) is None
