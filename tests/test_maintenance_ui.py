"""Maintenance schedule UI: CRUD, mark-serviced rolls next_due, worker
alert auto-resolves on the cycle after the roll forward.

Also the case that had none of that: a schedule with no ``interval_days``.
"Mark serviced" rolled ``next_due`` **only** for an interval schedule, so a
page-threshold schedule was left exactly as due as it was -- the alert could
never resolve, the operator was told the service was logged, and the only way
out was editing the date by hand. The tests below hold the three outcomes
apart: re-armed on a date, re-armed on the meter, or plainly told it will not
recur.
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


def _mark_serviced(cli, sched_id) -> str:
    """Click 'Mark serviced' and return what the operator is told afterwards."""
    cli.post(f"/manage/maintenance/schedules/{sched_id}/log",
             data={"performed_by": "tech"}, follow_redirects=False)
    return cli.get("/manage/maintenance").text


def test_page_threshold_schedule_recurs_from_the_current_meter(db):
    """A page-driven schedule must come due AGAIN, one threshold further on.

    The threshold is an odometer target, so servicing has to move it or the
    schedule is due forever. It moves by anchoring to the meter as it reads at
    the service (``last_serviced_page_count``) rather than by rewriting the
    threshold itself -- which would make the next step the sum of the last two
    and double on every service.
    """
    printer = _seed_printer(db)              # page_count=20000
    printer.page_count = 50_400
    past = datetime.now(timezone.utc) - timedelta(days=1)
    sched = m.MaintenanceSchedule(
        name="Fuser kit", printer_id=printer.id,
        page_threshold=50_000, next_due=past,
    )
    db.add(sched)
    db.commit()

    # It is due: the date has passed and the meter is past the target.
    jobs.check_maintenance_due(db)
    alert = db.scalar(select(m.Alert).where(
        m.Alert.type == m.AlertConditionType.maintenance_due))
    assert alert is not None and alert.state == m.AlertState.open

    cli = _admin(db)
    body = _mark_serviced(cli, sched.id)
    db.refresh(sched)
    assert sched.last_serviced_page_count == 50_400
    assert sched.page_target() == 100_400
    # next_due deliberately stays armed in the past: the worker only reaches a
    # page-gated schedule through next_due <= now, so clearing it would disarm
    # the page trigger for good.
    assert sched.next_due is not None
    assert "100,400 pages" in body

    # The alert resolves on the next cycle -- via the page gate, not the date.
    jobs.check_maintenance_due(db)
    db.refresh(alert)
    assert alert.state == m.AlertState.resolved

    # ... and it fires again once the meter crosses the new target.
    printer.page_count = 100_400
    db.commit()
    jobs.check_maintenance_due(db)
    live = [a for a in db.scalars(select(m.Alert)) if a.state == m.AlertState.open]
    assert len(live) == 1

    # A second service steps by the SAME 50,000, not by 100,400.
    _mark_serviced(cli, sched.id)
    db.refresh(sched)
    assert sched.page_target() == 150_400


def test_a_schedule_that_cannot_recur_says_so_and_stops_alerting(db):
    """A one-off reminder genuinely cannot recur. What it must not do is keep
    its alert open forever while reporting a plain success."""
    printer = _seed_printer(db)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    sched = m.MaintenanceSchedule(
        name="Replace by hand", printer_id=printer.id, next_due=past,
    )
    db.add(sched)
    db.commit()
    jobs.check_maintenance_due(db)
    alert = db.scalar(select(m.Alert).where(
        m.Alert.type == m.AlertConditionType.maintenance_due))
    assert alert.state == m.AlertState.open

    body = _mark_serviced(_admin(db), sched.id)
    assert "will NOT become due again" in body
    db.refresh(sched)
    assert sched.next_due is None            # cleared, so the alert can clear

    jobs.check_maintenance_due(db)
    db.refresh(alert)
    assert alert.state == m.AlertState.resolved


def test_marking_a_model_wide_schedule_serviced_explains_what_it_could_not_do(db):
    """maintenance_records.printer_id is NOT NULL and a model-wide schedule has
    no printer to log against -- this used to insert NULL and 500 the request.
    Now it is a stated outcome, and the schedule still stops being due."""
    _seed_printer(db)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    sched = m.MaintenanceSchedule(
        name="Fleet fuser", model="M404", page_threshold=50_000, next_due=past,
    )
    db.add(sched)
    db.commit()
    cli = _admin(db)
    resp = cli.post(f"/manage/maintenance/schedules/{sched.id}/log",
                    data={"performed_by": "tech"}, follow_redirects=False)
    assert resp.status_code == 303
    body = cli.get("/manage/maintenance").text
    assert "No service record was written" in body
    assert "not tied to one printer" in body
    assert db.scalar(select(m.MaintenanceRecord)) is None
    db.refresh(sched)
    assert sched.next_due is None


def test_a_printer_with_no_meter_yet_is_reported_not_silently_disarmed(db):
    """A page schedule on a printer that has never reported a page count cannot
    be anchored. Say which, rather than leaving the operator to discover that
    'serviced' meant nothing."""
    printer = _seed_printer(db)
    printer.page_count = None
    sched = m.MaintenanceSchedule(
        name="Fuser kit", printer_id=printer.id, page_threshold=50_000,
        next_due=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.add(sched)
    db.commit()
    body = _mark_serviced(_admin(db), sched.id)
    assert "never reported a page count" in body
    db.refresh(sched)
    assert sched.last_serviced_page_count is None
    assert sched.next_due is None


def test_interval_and_page_schedule_rolls_both(db):
    printer = _seed_printer(db)
    printer.page_count = 50_400
    sched = m.MaintenanceSchedule(
        name="Kit", printer_id=printer.id, interval_days=90,
        page_threshold=50_000,
        next_due=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.add(sched)
    db.commit()
    body = _mark_serviced(_admin(db), sched.id)
    db.refresh(sched)
    nd = sched.next_due
    if nd.tzinfo is None:
        nd = nd.replace(tzinfo=timezone.utc)
    assert nd > datetime.now(timezone.utc)
    assert sched.page_target() == 100_400
    assert "100,400 pages" in body


def test_schedule_delete_admin_only_for_records(db):
    _seed_printer(db)
    sched = m.MaintenanceSchedule(name="X", interval_days=30)
    db.add(sched)
    db.commit()
    cli = _admin(db)
    cli.post(f"/manage/maintenance/schedules/{sched.id}/delete",
             follow_redirects=False)
    assert db.scalar(select(m.MaintenanceSchedule)) is None
