"""Zero routed channels must not lose the alert permanently.

The loss scenario this covers (P0): every channel is disabled -- which happens
in practice, not just in theory, because ``save_settings(sections=None)`` flips
every channel's ``enabled`` flag off on a branding-logo upload. An alert
condition then fires, ``record_dispatch`` is handed an empty channel list, and:

1. no ``NotificationDelivery`` row is written, so ``retry_due`` has nothing to
   find and the notification is never retried;
2. the alert is nonetheless open, so dedupe suppresses every future attempt for
   the same condition;
3. re-enabling the channel therefore does NOT recover it -- the alert is lost
   permanently and silently, with no record anywhere that a notification was
   owed.

These tests assert the fixed behaviour end-to-end: a channel-less "owed" row is
persisted, the retry sweeper fans it out to whatever channels exist when one
comes back, the alert genuinely arrives, and an owed row that can never be
delivered terminates (alert resolved, or the give-up window elapsed) instead of
sitting pending forever and invisible.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central import queries
from central.channels import routable_channels
from central.channels.base import ChannelResult, Notification, NotificationChannel
from central.channels.delivery import (
    UNROUTED_CHANNEL_KEY,
    UNROUTED_CHANNEL_LABEL,
    UNROUTED_GIVE_UP,
    record_dispatch,
    retry_due,
)
from central.main import app
from central.runtime import load_settings, save_settings
from central.security import hash_password
from central.worker import jobs


class _Recorder(NotificationChannel):
    """A channel that records what it was asked to send. No network."""

    type = "recorder"

    def __init__(self, name="Email"):
        super().__init__(name, config={}, runtime={})
        self.sent: list = []

    def send(self, note: Notification) -> ChannelResult:
        self.sent.append(note)
        return ChannelResult(ok=True, detail="sent")


def _low_toner_fleet(db):
    """One approved printer at 5% black + a supply_below rule that fires on it."""
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.5",
        model="HP M404", display_name="Front desk",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    supply = m.Supply(
        printer_id=printer.id, type=m.SupplyType.toner, color="black", level_pct=5
    )
    db.add(supply)
    db.add(m.AlertRule(
        name="low", condition_type=m.AlertConditionType.supply_below,
        threshold=10, severity=m.EventSeverity.warning,
    ))
    db.commit()
    return printer, supply


# --------------------------------------------------------------------------- #
# The headline scenario: nothing enabled -> alert opens -> channel comes back
# -> the previously-owed alert is actually delivered.
# --------------------------------------------------------------------------- #
def test_alert_owed_with_zero_channels_is_delivered_once_a_channel_returns(db, monkeypatch):
    printer, _supply = _low_toner_fleet(db)

    # Phase 1 -- every channel disabled (the post-logo-upload state). Nothing to
    # route to at all: no NotificationChannel rows, no channel enabled in settings.
    assert routable_channels(db, load_settings(db)) == []

    res = jobs.evaluate_alerts(db)
    assert res["alerts_opened"] == 1
    alert = db.query(m.Alert).one()
    assert alert.state == m.AlertState.open

    # Nothing reached a channel, and the alert row says so honestly rather than
    # looking like a clean send.
    assert alert.notified_channels == [
        {"channel": UNROUTED_CHANNEL_LABEL, "ok": False, "sent": False,
         "detail": alert.notified_channels[0]["detail"]}
    ]
    assert not alert.notified_channels[0]["ok"]
    # sent=False is what stops the alerts page rendering a tick for this.
    assert not alert.notified_channels[0]["sent"]

    # ...and the notification is on the books as OWED, so the retry sweeper has
    # something to find. This is the row the bug never wrote.
    owed = db.query(m.NotificationDelivery).all()
    assert len(owed) == 1
    assert owed[0].channel_key == UNROUTED_CHANNEL_KEY
    assert owed[0].status == m.DeliveryStatus.pending
    assert owed[0].attempts == 0
    assert owed[0].next_attempt_at is None          # due now
    assert owed[0].payload["title"] == alert.title  # frozen for an exact re-send

    # The loss is visible to an operator before any recovery: audit trail...
    audit = db.query(m.AuditLog).filter_by(action="notification.unroutable").all()
    assert len(audit) == 1
    assert audit[0].target == f"alert:{alert.id}"
    # ...and the Alerts-page banner counter.
    assert queries.undelivered_notifications(db) == {"owed": 1, "dead": 0}

    # Dedupe still suppresses a duplicate open, and repeated cycles do not pile
    # up owed rows for the same condition.
    assert jobs.evaluate_alerts(db)["alerts_opened"] == 0
    assert db.query(m.NotificationDelivery).count() == 1

    # Phase 2 -- the operator re-enables a channel. The owed alert must arrive.
    ch = _Recorder("Email")
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])

    res = jobs.retry_deliveries(db)
    assert res["deliveries_delivered"] == 1
    assert [n.title for n in ch.sent] == [alert.title]

    d = db.query(m.NotificationDelivery).one()
    assert d.channel_key == "Email"
    assert d.status == m.DeliveryStatus.delivered
    assert d.next_attempt_at is None

    # The alert no longer claims nobody was told -- the badge follows reality.
    db.refresh(alert)
    assert alert.notified_channels == [
        {"channel": "Email", "ok": True, "sent": True, "detail": "sent"}
    ]

    # Terminal: a further sweep does not re-send it.
    jobs.retry_deliveries(db)
    assert len(ch.sent) == 1
    # Nothing left owed, so the banner clears.
    assert queries.undelivered_notifications(db) == {"owed": 0, "dead": 0}


# --------------------------------------------------------------------------- #
# The same recovery through the real settings path -- an operator turning the
# channel back on in the UI, not a monkeypatched channel list.
# --------------------------------------------------------------------------- #
def test_recovery_when_the_operator_re_enables_the_channel_in_settings(db, monkeypatch):
    """Zero routed channels, then a real Settings save, recovers the alert."""
    sent: list = []
    monkeypatch.setattr(
        "central.channels.webhook.WebhookChannel.send",
        lambda self, note: sent.append((self.name, note.title))
        or ChannelResult(ok=True, detail="posted"),
    )
    _low_toner_fleet(db)
    # Channel configured and working...
    save_settings(db, {"webhook.enabled": "on", "webhook.url": "https://hooks.test/x"})
    # ...then the operator unchecks it in the UI. That is a *scoped* save whose
    # payload omits the box, which is how an absent bool legitimately means
    # "unchecked" -- see save_settings' contract. This test originally reached the
    # zero-channel state by doing an unscoped branding save, relying on the
    # bool-wipe P0 that is now fixed; the state it exercises is still reachable
    # (this path, or a fresh install with nothing configured), so only the setup
    # moved.
    save_settings(db, {"webhook.url": "https://hooks.test/x"},
                  sections={"Webhook (generic)"})
    assert routable_channels(db, load_settings(db)) == []

    jobs.evaluate_alerts(db)
    assert sent == []                                       # nothing delivered
    assert db.query(m.NotificationDelivery).count() == 1     # but it is owed

    # The operator notices the banner and re-enables the webhook.
    save_settings(db, {"webhook.enabled": "on", "webhook.url": "https://hooks.test/x"})
    res = jobs.retry_deliveries(db)

    assert res["deliveries_delivered"] == 1
    assert [name for name, _title in sent] == ["Webhook"]
    d = db.query(m.NotificationDelivery).one()
    assert (d.channel_key, d.status) == ("Webhook", m.DeliveryStatus.delivered)


# --------------------------------------------------------------------------- #
# Fan-out: one owed notification becomes exactly one delivery per channel.
# --------------------------------------------------------------------------- #
def test_owed_row_fans_out_to_every_channel_once(db, monkeypatch):
    _low_toner_fleet(db)
    jobs.evaluate_alerts(db)
    assert db.query(m.NotificationDelivery).count() == 1

    email, slack = _Recorder("Email"), _Recorder("Slack")
    monkeypatch.setattr(
        "central.channels.delivery.active_channels", lambda rt: [email, slack]
    )
    res = jobs.retry_deliveries(db)
    assert res["deliveries_retried"] == 2
    assert res["deliveries_delivered"] == 2

    rows = db.query(m.NotificationDelivery).all()
    assert {r.channel_key for r in rows} == {"Email", "Slack"}
    assert all(r.status == m.DeliveryStatus.delivered for r in rows)
    assert len(email.sent) == 1 and len(slack.sent) == 1

    # No owed row survives the fan-out, and nothing is re-sent.
    assert not [r for r in rows if r.channel_key == UNROUTED_CHANNEL_KEY]
    jobs.retry_deliveries(db)
    assert len(email.sent) == 1 and len(slack.sent) == 1


# --------------------------------------------------------------------------- #
# A failed fan-out stays retryable rather than vanishing again.
# --------------------------------------------------------------------------- #
def test_failed_fan_out_is_retryable_not_lost(db, monkeypatch):
    _low_toner_fleet(db)
    jobs.evaluate_alerts(db)

    calls = {"n": 0}

    class _FlakyOnce(NotificationChannel):
        type = "flaky"

        def send(self, note):
            calls["n"] += 1
            if calls["n"] == 1:
                return ChannelResult(ok=False, detail="smtp down")
            return ChannelResult(ok=True, detail="sent")

    ch = _FlakyOnce("Email", config={}, runtime={})
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])

    now = datetime.now(timezone.utc)
    jobs.retry_deliveries(db, now=now)
    d = db.query(m.NotificationDelivery).one()
    # Fan-out failed: the row is now a normal retryable per-channel delivery.
    assert (d.channel_key, d.status, d.attempts) == ("Email", m.DeliveryStatus.failed, 1)
    assert d.next_attempt_at is not None

    jobs.retry_deliveries(db, now=now + timedelta(hours=2))
    d = db.query(m.NotificationDelivery).one()
    assert (d.status, d.attempts) == (m.DeliveryStatus.delivered, 2)


# --------------------------------------------------------------------------- #
# Bounded growth: an owed row terminates, it does not sit pending forever.
# --------------------------------------------------------------------------- #
def test_owed_row_closes_out_when_its_alert_resolves(db):
    _printer, supply = _low_toner_fleet(db)
    jobs.evaluate_alerts(db)
    assert db.query(m.NotificationDelivery).count() == 1

    # Cartridge replaced before any channel came back -> the alert auto-resolves,
    # so the owed notification is stale news and must close out.
    supply.level_pct = 95
    db.commit()
    assert jobs.evaluate_alerts(db)["alerts_resolved"] == 1

    res = jobs.retry_deliveries(db)
    assert res["deliveries_dead"] == 1
    d = db.query(m.NotificationDelivery).one()
    assert d.status == m.DeliveryStatus.dead
    assert d.last_error == "alert resolved before delivery"
    # A resolved alert's dead letter is history, not an operator to-do.
    assert queries.undelivered_notifications(db) == {"owed": 0, "dead": 0}


def test_repeated_cycles_with_no_channel_do_not_accumulate_or_burn_attempts(db):
    _low_toner_fleet(db)
    for _ in range(6):
        jobs.evaluate_alerts(db)
        res = jobs.retry_deliveries(db)

    # One owed row for the one condition -- dedupe bounds it, and the sweeper
    # neither duplicates it nor spends the retry budget while there is nowhere
    # to send.
    assert db.query(m.NotificationDelivery).count() == 1
    d = db.query(m.NotificationDelivery).one()
    assert (d.status, d.attempts) == (m.DeliveryStatus.pending, 0)
    assert res["deliveries_owed"] == 1
    assert res["deliveries_retried"] == 0
    # And one audit row, not one per cycle.
    assert db.query(m.AuditLog).filter_by(action="notification.unroutable").count() == 1


def test_owed_row_gives_up_after_the_window_and_says_so(db):
    _low_toner_fleet(db)
    jobs.evaluate_alerts(db)
    later = datetime.now(timezone.utc) + UNROUTED_GIVE_UP + timedelta(minutes=1)

    res = jobs.retry_deliveries(db, now=later)
    assert res["deliveries_dead"] == 1
    d = db.query(m.NotificationDelivery).one()
    assert d.status == m.DeliveryStatus.dead
    assert "gave up" in d.last_error
    # Giving up is itself recorded for the operator at /manage/audit -- the
    # durable surface once the row is no longer pending (so no longer bannered).
    gave_up = db.query(m.AuditLog).filter_by(action="notification.gave_up").all()
    assert len(gave_up) == 1
    assert gave_up[0].target == f"alert:{db.query(m.Alert).one().id}"
    assert queries.undelivered_notifications(db) == {"owed": 0, "dead": 0}


def test_banner_counts_a_broken_channels_dead_letters(db):
    """A real channel that exhausted its retries IS an operator to-do."""
    alert = m.Alert(
        type=m.AlertConditionType.supply_below, severity=m.EventSeverity.warning,
        state=m.AlertState.open, title="Low toner", dedupe_key="k:2",
    )
    db.add(alert)
    db.flush()
    ch = _Recorder("Email")
    ch.send = lambda note: ChannelResult(ok=False, detail="auth failed")
    record_dispatch(db, alert.id,
                    Notification(title="Low toner", body="", severity="warning"), [ch],
                    runtime={"notifications.max_attempts": 1})  # dead on first failure
    db.commit()
    assert db.query(m.NotificationDelivery).one().status == m.DeliveryStatus.dead
    assert queries.undelivered_notifications(db) == {"owed": 0, "dead": 1}

    # Once the condition clears it is history, not a to-do.
    alert.state = m.AlertState.resolved
    db.commit()
    assert queries.undelivered_notifications(db) == {"owed": 0, "dead": 0}


def test_fan_out_does_not_re_page_a_channel_an_escalation_already_reached(db, monkeypatch):
    """An escalation that got through first must not be duplicated by the fan-out."""
    _low_toner_fleet(db)
    jobs.evaluate_alerts(db)
    owed = db.query(m.NotificationDelivery).one()
    assert owed.channel_key == UNROUTED_CHANNEL_KEY

    # Simulate the escalation re-notify having already delivered to Email.
    ch = _Recorder("Email")
    record_dispatch(db, owed.alert_id,
                    Notification(title="t", body="b", severity="warning"), [ch],
                    runtime={})
    db.commit()
    assert len(ch.sent) == 1

    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])
    res = jobs.retry_deliveries(db)
    assert res["deliveries_retried"] == 0
    assert len(ch.sent) == 1  # not paged twice
    d = db.get(m.NotificationDelivery, owed.id)
    assert d.status == m.DeliveryStatus.dead
    assert "superseded" in d.last_error


def test_an_owed_escalation_is_not_swallowed_by_the_open_time_delivery(db, monkeypatch):
    """The mirror of the above: a delivery from BEFORE the owed row must not
    suppress it. The alert was notified at open time, went un-resolved, and the
    escalation re-notify then fell into the owed state because the channel had
    been disabled in between -- that nudge is still owed."""
    alert = m.Alert(
        type=m.AlertConditionType.supply_below, severity=m.EventSeverity.warning,
        state=m.AlertState.open, title="Low toner", dedupe_key="k:3",
    )
    db.add(alert)
    db.flush()
    ch = _Recorder("Email")

    # Open time: delivered normally.
    record_dispatch(db, alert.id,
                    Notification(title="Low toner", body="", severity="warning"), [ch],
                    runtime={})
    db.commit()
    assert len(ch.sent) == 1

    # Escalation later, with the channel now disabled -> owed.
    record_dispatch(db, alert.id,
                    Notification(title="Low toner", body="", severity="warning"), [],
                    runtime={})
    db.commit()
    owed = db.query(m.NotificationDelivery).filter_by(
        channel_key=UNROUTED_CHANNEL_KEY).one()

    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])
    res = jobs.retry_deliveries(db)
    assert res["deliveries_delivered"] == 1
    assert len(ch.sent) == 2  # the escalation nudge went out, not swallowed
    assert db.get(m.NotificationDelivery, owed.id).status == m.DeliveryStatus.delivered


# --------------------------------------------------------------------------- #
# Pre-existing behaviour that the reordered resolved-check must not regress:
# a row for a channel the operator disabled still terminates when its alert
# resolves (it used to be skipped forever).
# --------------------------------------------------------------------------- #
def test_delivery_for_a_disabled_channel_closes_out_on_resolve(db):
    alert = m.Alert(
        type=m.AlertConditionType.supply_below, severity=m.EventSeverity.warning,
        state=m.AlertState.open, title="Low toner", dedupe_key="k:1",
    )
    db.add(alert)
    db.flush()
    ch = _Recorder("Slack")
    ch.send = lambda note: ChannelResult(ok=False, detail="slack down")
    record_dispatch(db, alert.id,
                    Notification(title="Low toner", body="", severity="warning"), [ch],
                    runtime={"notifications.max_attempts": 5})
    db.commit()
    d = db.query(m.NotificationDelivery).one()
    assert d.status == m.DeliveryStatus.failed

    # Slack is now disabled (active_channels returns nothing) AND the alert
    # resolved. The row must terminate rather than linger as a due-forever skip.
    alert.state = m.AlertState.resolved
    db.commit()
    res = retry_due(db, {}, now=datetime.now(timezone.utc) + timedelta(hours=2))
    assert res["deliveries_dead"] == 1
    assert res["deliveries_skipped"] == 0
    assert db.query(m.NotificationDelivery).one().status == m.DeliveryStatus.dead


# --------------------------------------------------------------------------- #
# Operator surface: the Alerts page tells them, in words, what happened.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def http(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("admin"),
                  role=m.UserRole.admin))
    db.commit()
    client = TestClient(app)
    assert client.post("/login", data={"username": "admin", "password": "admin"},
                       follow_redirects=False).status_code == 303
    return client


def test_printer_supplied_text_is_escaped_on_the_new_audit_path(http, db):
    """The owed-notification audit row carries the alert title, which contains
    device-supplied SNMP text. It renders in the admin audit viewer, so prove it
    is escaped there (and on the alerts page) rather than executed."""
    hostile = "<script>alert('xss')</script>"
    _printer, _supply = _low_toner_fleet(db)
    printer = db.query(m.Printer).one()
    printer.display_name = hostile
    db.commit()

    jobs.evaluate_alerts(db)
    row = db.query(m.AuditLog).filter_by(action="notification.unroutable").one()
    assert hostile in (row.detail or "")          # stored verbatim...

    for path in ("/manage/audit", "/alerts"):
        page = http.get(path).text
        assert "<script>alert(" not in page       # ...never rendered as markup
        assert "&lt;script&gt;alert(" in page, path


def test_alerts_page_warns_about_undelivered_notifications(http, db):
    _low_toner_fleet(db)
    page = http.get("/alerts").text
    assert "Notifications were not delivered" not in page  # clean tree, no banner

    jobs.evaluate_alerts(db)
    page = http.get("/alerts").text
    assert "Notifications were not delivered" in page
    assert "no channel is enabled" in page
    assert "/settings?group=notifications" in page
    # The alert row itself no longer looks merely un-notified.
    assert UNROUTED_CHANNEL_LABEL in page
