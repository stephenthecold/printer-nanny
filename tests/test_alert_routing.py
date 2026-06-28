"""Per-tenant alert routing, escalation re-notify, and the ack-resolve fix.

These exercise the new routing layer end-to-end through ``evaluate_alerts`` and
``check_maintenance_due``. Channels are NotificationChannel rows of type
``webhook`` with no URL configured, so every ``send`` is a no-network dry-run
that still records into ``Alert.notified_channels`` -- which is exactly the set
of channels the router decided to deliver to. Asserting on that set proves the
routing decision without touching the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from central import models as m
from central.channels import RoutableChannel, route_channels, routable_channels
from central.runtime import load_settings, save_settings
from central.worker import jobs


def _routed_names(alert: m.Alert) -> set:
    """Channel names the alert was actually dispatched to."""
    return {entry["channel"] for entry in (alert.notified_channels or [])}


def _two_clients_with_low_supply(db):
    """Two clients, each with one approved printer whose black toner is low."""
    out = {}
    for cname, ip in (("Acme", "10.0.0.5"), ("Globex", "10.0.1.5")):
        client = m.Client(name=cname)
        db.add(client)
        db.flush()
        site = m.Site(client_id=client.id, name=f"{cname} HQ")
        db.add(site)
        db.flush()
        printer = m.Printer(
            client_id=client.id, site_id=site.id, ip=ip, model="HP M404",
            discovery_state=m.DiscoveryState.approved,
        )
        db.add(printer)
        db.flush()
        db.add(m.Supply(printer_id=printer.id, type=m.SupplyType.toner,
                        color="black", level_pct=5))
        out[cname] = {"client": client, "site": site, "printer": printer}
    db.commit()
    return out


def _webhook_channel(db, name, *, scope=m.AlertScope.global_, scope_id=None,
                     config=None, enabled=True):
    row = m.NotificationChannel(
        name=name, type=m.ChannelType.webhook, config=config or {},
        scope=scope, scope_id=scope_id, enabled=enabled,
    )
    db.add(row)
    db.flush()
    return row


def _supply_rule(db, *, channel_ids=None):
    rule = m.AlertRule(
        name="low", condition_type=m.AlertConditionType.supply_below,
        threshold=10, severity=m.EventSeverity.warning, channel_ids=channel_ids,
    )
    db.add(rule)
    db.flush()
    return rule


# --------------------------------------------------------------------------- #
# Routing by NotificationChannel scope
# --------------------------------------------------------------------------- #
def test_routing_by_scope(db):
    data = _two_clients_with_low_supply(db)
    acme_id = data["Acme"]["client"].id
    globex_id = data["Globex"]["client"].id
    _webhook_channel(db, "acme-hook", scope=m.AlertScope.client, scope_id=acme_id)
    _webhook_channel(db, "globex-hook", scope=m.AlertScope.client, scope_id=globex_id)
    _supply_rule(db)  # no channel_ids → all in-scope channels eligible
    db.commit()

    res = jobs.evaluate_alerts(db)
    assert res["alerts_opened"] == 2

    by_client = {}
    for alert in db.query(m.Alert).all():
        printer = db.get(m.Printer, alert.printer_id)
        by_client[printer.client_id] = _routed_names(alert)

    # Acme's alert reaches only the Acme-scoped channel; Globex's only Globex's.
    assert by_client[acme_id] == {"acme-hook"}
    assert by_client[globex_id] == {"globex-hook"}


def test_global_channel_reaches_every_tenant(db):
    _two_clients_with_low_supply(db)
    _webhook_channel(db, "all-hook", scope=m.AlertScope.global_)
    _supply_rule(db)
    db.commit()

    jobs.evaluate_alerts(db)
    for alert in db.query(m.Alert).all():
        assert "all-hook" in _routed_names(alert)


# --------------------------------------------------------------------------- #
# Routing by AlertRule.channel_ids
# --------------------------------------------------------------------------- #
def test_routing_by_rule_channel_ids(db):
    _two_clients_with_low_supply(db)
    chosen = _webhook_channel(db, "chosen-hook", scope=m.AlertScope.global_)
    _webhook_channel(db, "other-hook", scope=m.AlertScope.global_)
    _supply_rule(db, channel_ids=[chosen.id])  # restrict to the chosen channel
    db.commit()

    jobs.evaluate_alerts(db)
    for alert in db.query(m.Alert).all():
        names = _routed_names(alert)
        assert names == {"chosen-hook"}
        assert "other-hook" not in names


def test_rule_channel_ids_excludes_global_runtime_channel(db):
    """A rule naming a channel must NOT also fan out to the Settings-page channels."""
    _two_clients_with_low_supply(db)
    # Enable a global runtime webhook via settings (would otherwise route too).
    save_settings(db, {"webhook.enabled": "on", "webhook.url": ""})
    chosen = _webhook_channel(db, "chosen-hook", scope=m.AlertScope.global_)
    _supply_rule(db, channel_ids=[chosen.id])
    db.commit()

    jobs.evaluate_alerts(db)
    for alert in db.query(m.Alert).all():
        # Only the named row, not the global "Webhook" runtime channel.
        assert _routed_names(alert) == {"chosen-hook"}


# --------------------------------------------------------------------------- #
# Per-channel severity filter (now generalized to all channels)
# --------------------------------------------------------------------------- #
def test_severity_filter_drops_below_minimum(db):
    _two_clients_with_low_supply(db)
    # A critical-only channel must not receive a warning-severity supply alert.
    _webhook_channel(db, "crit-only", config={"min_severity": "critical"})
    _webhook_channel(db, "everything", config={"min_severity": "info"})
    _supply_rule(db)  # severity = warning
    db.commit()

    jobs.evaluate_alerts(db)
    for alert in db.query(m.Alert).all():
        names = _routed_names(alert)
        assert "everything" in names
        assert "crit-only" not in names


def test_route_channels_severity_unit():
    """route_channels filters candidates by per-channel min_severity directly."""
    from central.channels.webhook import WebhookChannel

    crit = WebhookChannel("crit", {"min_severity": "critical"})
    info = WebhookChannel("info", {"min_severity": "info"})
    cands = [
        RoutableChannel(crit, None, m.AlertScope.global_, None),
        RoutableChannel(info, None, m.AlertScope.global_, None),
    ]
    picked = route_channels(cands, severity="warning")
    assert [c.name for c in picked] == ["info"]


# --------------------------------------------------------------------------- #
# Ack-resolve fix
# --------------------------------------------------------------------------- #
def test_acknowledged_alert_resolves_when_condition_clears(db):
    _two_clients_with_low_supply(db)
    _webhook_channel(db, "all-hook")
    _supply_rule(db)
    db.commit()

    jobs.evaluate_alerts(db)
    alerts = db.query(m.Alert).filter_by(state=m.AlertState.open).all()
    assert len(alerts) == 2

    # Operator acknowledges both (seen, not yet fixed).
    for alert in alerts:
        alert.state = m.AlertState.acknowledged
    db.commit()

    # Re-running with the condition still present does NOT re-open duplicates
    # (dedupe now treats acknowledged as live).
    res = jobs.evaluate_alerts(db)
    assert res["alerts_opened"] == 0
    assert db.query(m.Alert).count() == 2

    # Supplies refilled on every printer → the condition clears.
    for supply in db.query(m.Supply).all():
        supply.level_pct = 95
    db.commit()

    res2 = jobs.evaluate_alerts(db)
    # Both acknowledged alerts auto-resolve (the bug: they used to be stuck).
    assert res2["alerts_resolved"] == 2
    assert db.query(m.Alert).filter_by(state=m.AlertState.acknowledged).count() == 0
    assert db.query(m.Alert).filter_by(state=m.AlertState.resolved).count() == 2


def test_acknowledged_maintenance_alert_resolves(db):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(client_id=client.id, site_id=site.id, ip="10.0.0.9",
                        model="HP M404", discovery_state=m.DiscoveryState.approved)
    db.add(printer)
    db.flush()
    sched = m.MaintenanceSchedule(
        printer_id=printer.id, name="Quarterly PM",
        interval_days=90, next_due=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.add(sched)
    db.commit()

    jobs.check_maintenance_due(db)
    alert = db.query(m.Alert).filter_by(
        type=m.AlertConditionType.maintenance_due).one()
    alert.state = m.AlertState.acknowledged
    db.commit()

    # Service logged → schedule rolled forward → acknowledged alert resolves.
    sched.next_due = datetime.now(timezone.utc) + timedelta(days=90)
    db.commit()
    res = jobs.check_maintenance_due(db)
    assert res["maintenance_resolved"] == 1
    assert db.query(m.Alert).filter_by(state=m.AlertState.resolved).count() == 1


# --------------------------------------------------------------------------- #
# Escalation re-notify
# --------------------------------------------------------------------------- #
def test_escalation_off_by_default(db):
    _two_clients_with_low_supply(db)
    _webhook_channel(db, "all-hook")
    _supply_rule(db)
    db.commit()

    res = jobs.evaluate_alerts(db)
    assert res["alerts_escalated"] == 0
    for alert in db.query(m.Alert).all():
        assert alert.escalation_level == 0
        assert alert.last_notified_at is not None  # stamped on open


def test_escalation_renotifies_after_window(db):
    _two_clients_with_low_supply(db)
    _webhook_channel(db, "all-hook")
    _supply_rule(db)
    save_settings(db, {"alerts.escalate_after_minutes": "30"})
    db.commit()

    t0 = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    jobs.evaluate_alerts(db, now=t0)
    for alert in db.query(m.Alert).all():
        assert alert.escalation_level == 0
        assert alert.last_notified_at is not None

    # 10 minutes later: still inside the 30-minute window → no escalation.
    jobs.evaluate_alerts(db, now=t0 + timedelta(minutes=10))
    assert all(a.escalation_level == 0 for a in db.query(m.Alert).all())

    # 40 minutes after the last notify: window elapsed → re-notify, bump level.
    res = jobs.evaluate_alerts(db, now=t0 + timedelta(minutes=50))
    assert res["alerts_escalated"] == 2
    for alert in db.query(m.Alert).all():
        assert alert.escalation_level == 1
        assert jobs._aware(alert.last_notified_at) == t0 + timedelta(minutes=50)
        # The escalation re-dispatched through the same routing.
        assert "all-hook" in _routed_names(alert)


def test_escalation_continues_climbing(db):
    _two_clients_with_low_supply(db)
    _webhook_channel(db, "all-hook")
    _supply_rule(db)
    save_settings(db, {"alerts.escalate_after_minutes": "30"})
    db.commit()

    t0 = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    jobs.evaluate_alerts(db, now=t0)
    jobs.evaluate_alerts(db, now=t0 + timedelta(minutes=40))   # level → 1
    jobs.evaluate_alerts(db, now=t0 + timedelta(minutes=80))   # level → 2
    for alert in db.query(m.Alert).all():
        assert alert.escalation_level == 2


def test_escalation_skips_resolved(db):
    _two_clients_with_low_supply(db)
    _webhook_channel(db, "all-hook")
    _supply_rule(db)
    save_settings(db, {"alerts.escalate_after_minutes": "30"})
    db.commit()

    t0 = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    jobs.evaluate_alerts(db, now=t0)
    # Clear the condition so the alerts resolve on the next pass.
    for supply in db.query(m.Supply).all():
        supply.level_pct = 95
    db.commit()
    res = jobs.evaluate_alerts(db, now=t0 + timedelta(minutes=90))
    assert res["alerts_resolved"] == 2
    assert res["alerts_escalated"] == 0  # resolved alerts are not escalated


def test_routable_channels_lists_rows_and_globals(db):
    _two_clients_with_low_supply(db)
    save_settings(db, {"webhook.enabled": "on", "webhook.url": ""})
    _webhook_channel(db, "row-hook", scope=m.AlertScope.global_)
    db.commit()
    runtime = load_settings(db)
    cands = routable_channels(db, runtime)
    names = {rc.channel.name for rc in cands}
    assert "row-hook" in names      # DB row
    assert "Webhook" in names       # global runtime channel
    # The row carries its id; the global one does not.
    row_ids = {rc.channel.name: rc.row_id for rc in cands}
    assert row_ids["row-hook"] is not None
    assert row_ids["Webhook"] is None
