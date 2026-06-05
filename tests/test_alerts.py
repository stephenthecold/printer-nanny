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
