"""Printer-level offline detection.

An unreachable printer produces no reading at all -- the agent drops it from
the batch -- so ``Printer.status`` used to sit on its last-good value forever
and nothing ever alerted. These tests pin the two halves of the fix: the
worker marks the status, and a ``printer_offline`` rule notifies on it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from central import models as m
from central.worker import jobs


def _fleet(db, *, agent_online=True, last_seen_minutes_ago=120):
    """One approved printer whose site is served by one agent."""
    now = datetime.now(timezone.utc)
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    agent = m.Agent(
        site_id=site.id,
        name="hq-agent",
        api_key_hash="x",
        status=m.AgentStatus.online if agent_online else m.AgentStatus.offline,
        last_heartbeat=now,
    )
    db.add(agent)
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip="10.0.0.5",
        model="HP M404",
        discovery_state=m.DiscoveryState.approved,
        status=m.PrinterStatus.ok,
        last_seen=now - timedelta(minutes=last_seen_minutes_ago),
    )
    db.add(printer)
    db.commit()
    return printer, agent


def _offline_rule(db, threshold=30):
    db.add(
        m.AlertRule(
            name="printer offline",
            condition_type=m.AlertConditionType.printer_offline,
            threshold=threshold,
            severity=m.EventSeverity.critical,
        )
    )
    db.commit()


def test_stale_printer_is_marked_offline(db):
    printer, _ = _fleet(db, last_seen_minutes_ago=120)

    res = jobs.mark_offline_printers(db)

    assert res["printers_marked_offline"] == 1
    db.refresh(printer)
    assert printer.status == m.PrinterStatus.offline


def test_recently_seen_printer_is_left_alone(db):
    printer, _ = _fleet(db, last_seen_minutes_ago=1)

    res = jobs.mark_offline_printers(db)

    assert res["printers_marked_offline"] == 0
    db.refresh(printer)
    assert printer.status == m.PrinterStatus.ok


def test_never_polled_printer_is_not_marked_offline(db):
    """Approved but never polled has no staleness to measure -- don't guess."""
    printer, _ = _fleet(db)
    printer.last_seen = None
    db.commit()

    assert jobs.mark_offline_printers(db)["printers_marked_offline"] == 0
    db.refresh(printer)
    assert printer.status == m.PrinterStatus.ok


def test_printers_behind_a_dead_agent_are_skipped(db):
    """A site outage stays one agent alert, not one alert per printer."""
    printer, _ = _fleet(db, agent_online=False, last_seen_minutes_ago=120)

    assert jobs.mark_offline_printers(db)["printers_marked_offline"] == 0
    db.refresh(printer)
    assert printer.status == m.PrinterStatus.ok


def test_marking_is_idempotent(db):
    _fleet(db, last_seen_minutes_ago=120)

    assert jobs.mark_offline_printers(db)["printers_marked_offline"] == 1
    # Already offline -> counted as changed only the first time.
    assert jobs.mark_offline_printers(db)["printers_marked_offline"] == 0


def test_offline_rule_opens_and_auto_resolves(db):
    printer, _ = _fleet(db, last_seen_minutes_ago=120)
    _offline_rule(db)

    res = jobs.evaluate_alerts(db)
    assert res["alerts_opened"] == 1
    alert = db.query(m.Alert).filter_by(state=m.AlertState.open).one()
    assert "offline" in alert.title.lower()

    # Dedupe: still down, no second alert.
    assert jobs.evaluate_alerts(db)["alerts_opened"] == 0

    # The printer answers again -> the alert resolves itself.
    printer.last_seen = datetime.now(timezone.utc)
    db.commit()
    assert jobs.evaluate_alerts(db)["alerts_resolved"] == 1


def test_offline_rule_suppressed_while_the_agent_is_down(db):
    """No printer-offline alerts for a site whose collector is dark."""
    _fleet(db, agent_online=False, last_seen_minutes_ago=120)
    _offline_rule(db)

    assert jobs.evaluate_alerts(db)["alerts_opened"] == 0
    assert db.query(m.Alert).count() == 0
