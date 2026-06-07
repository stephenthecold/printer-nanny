"""Alert-rule evaluation and the supply-depletion forecast."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from central import models as m
from central.worker import jobs


def _approved_printer(db, ip="10.0.0.5"):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip=ip,
        model="HP M404",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    return printer


def test_supply_below_opens_and_resolves(db):
    printer = _approved_printer(db)
    supply = m.Supply(printer_id=printer.id, type=m.SupplyType.toner, color="black", level_pct=5)
    db.add(supply)
    db.add(
        m.AlertRule(
            name="low",
            condition_type=m.AlertConditionType.supply_below,
            threshold=10,
            severity=m.EventSeverity.warning,
        )
    )
    db.commit()

    res = jobs.evaluate_alerts(db)
    assert res["alerts_opened"] == 1
    assert db.query(m.Alert).filter_by(state=m.AlertState.open).count() == 1

    # Re-running doesn't duplicate the open alert (dedupe).
    res2 = jobs.evaluate_alerts(db)
    assert res2["alerts_opened"] == 0

    # Refill → the open alert auto-resolves.
    supply.level_pct = 90
    db.commit()
    res3 = jobs.evaluate_alerts(db)
    assert res3["alerts_resolved"] == 1
    assert db.query(m.Alert).filter_by(state=m.AlertState.open).count() == 0


def test_offline_agent_alert(db):
    printer = _approved_printer(db)
    agent = m.Agent(
        site_id=printer.site_id,
        name="remote",
        api_key_hash="x",
        last_heartbeat=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db.add(agent)
    db.add(
        m.AlertRule(
            name="offline",
            condition_type=m.AlertConditionType.offline_minutes,
            threshold=30,
            severity=m.EventSeverity.warning,
        )
    )
    db.commit()

    res = jobs.evaluate_alerts(db)
    assert res["alerts_opened"] == 1
    alert = db.query(m.Alert).filter_by(state=m.AlertState.open).one()
    assert alert.agent_id == agent.id


def test_error_severity_alert(db):
    printer = _approved_printer(db)
    db.add(
        m.PrinterEvent(
            printer_id=printer.id,
            severity=m.EventSeverity.critical,
            source=m.EventSource.snmp_alert,
            message="Paper jam",
            code="jam",
        )
    )
    db.add(
        m.AlertRule(
            name="errors",
            condition_type=m.AlertConditionType.error_severity,
            severity=m.EventSeverity.critical,
        )
    )
    db.commit()
    res = jobs.evaluate_alerts(db)
    assert res["alerts_opened"] == 1


def test_mark_offline_agents(db):
    printer = _approved_printer(db)
    db.add(
        m.Agent(
            site_id=printer.site_id,
            name="stale",
            api_key_hash="x",
            status=m.AgentStatus.online,
            last_heartbeat=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    db.commit()
    res = jobs.mark_offline_agents(db)
    assert res["agents_updated"] == 1
    assert db.query(m.Agent).filter_by(status=m.AgentStatus.offline).count() == 1


def test_forecast_days_to_empty():
    now = datetime.now(timezone.utc)
    # 50% → 30% over 10 days = 2%/day → 15 days left from 30%.
    points = [(now - timedelta(days=10), 50.0), (now, 30.0)]
    assert jobs.forecast_days_to_empty(points) == 15.0
    # Rising level → no estimate.
    assert jobs.forecast_days_to_empty([(now - timedelta(days=5), 10.0), (now, 40.0)]) is None
    # Too few points.
    assert jobs.forecast_days_to_empty([(now, 50.0)]) is None


def test_forecast_ignores_pre_refill_history():
    now = datetime.now(timezone.utc)
    # Steep drop, then a refill (30→95), then a gentler decline. Only the
    # post-refill segment (95→55 over 10 days = 4%/day) should be fit → 13.8.
    points = [
        (now - timedelta(days=20), 80.0),
        (now - timedelta(days=12), 30.0),
        (now - timedelta(days=10), 95.0),  # refill
        (now, 55.0),
    ]
    assert jobs.forecast_days_to_empty(points) == 13.8


def test_error_alert_resolves_after_event_clears(db):
    from central import schemas as sch
    from central import services

    printer = _approved_printer(db)
    db.add(
        m.AlertRule(
            name="errors",
            condition_type=m.AlertConditionType.error_severity,
            severity=m.EventSeverity.critical,
        )
    )
    db.commit()

    # Poll reports a jam → one open event → alert opens.
    services.apply_reading(db, printer.site_id, sch.ReadingIn(
        ip=printer.ip, events=[sch.EventIn(code="jammed", severity=m.EventSeverity.critical,
                                           source=m.EventSource.snmp_alert, message="Jammed")]))
    db.commit()
    jobs.evaluate_alerts(db)
    assert db.query(m.Alert).filter_by(state=m.AlertState.open).count() == 1
    assert db.query(m.PrinterEvent).filter_by(resolved_at=None).count() == 1

    # Next poll: jam cleared (no events) → event resolves, no duplicate rows...
    services.apply_reading(db, printer.site_id, sch.ReadingIn(ip=printer.ip, events=[]))
    db.commit()
    assert db.query(m.PrinterEvent).filter_by(resolved_at=None).count() == 0
    # ...and the error alert auto-resolves.
    res = jobs.evaluate_alerts(db)
    assert res["alerts_resolved"] == 1
    assert db.query(m.Alert).filter_by(state=m.AlertState.open).count() == 0


def test_event_dedup_does_not_grow_rows(db):
    from central import schemas as sch
    from central import services

    printer = _approved_printer(db)
    ev = lambda: sch.ReadingIn(ip=printer.ip, events=[sch.EventIn(  # noqa: E731
        code="low-toner", severity=m.EventSeverity.warning,
        source=m.EventSource.snmp_alert, message="Low toner")])
    for _ in range(3):
        services.apply_reading(db, printer.site_id, ev())
        db.commit()
    # Same standing condition re-reported 3x → still a single event row.
    assert db.query(m.PrinterEvent).count() == 1


def test_maintenance_due_opens_and_resolves(db):
    printer = _approved_printer(db)
    sched = m.MaintenanceSchedule(
        printer_id=printer.id, name="Quarterly PM",
        next_due=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.add(sched)
    db.commit()

    res = jobs.check_maintenance_due(db)
    assert res["maintenance_opened"] == 1
    assert db.query(m.Alert).filter_by(
        type=m.AlertConditionType.maintenance_due, state=m.AlertState.open).count() == 1

    # Service logged → schedule rolled forward → alert resolves.
    sched.next_due = datetime.now(timezone.utc) + timedelta(days=90)
    db.commit()
    res2 = jobs.check_maintenance_due(db)
    assert res2["maintenance_resolved"] == 1
    assert db.query(m.Alert).filter_by(
        type=m.AlertConditionType.maintenance_due, state=m.AlertState.open).count() == 0
