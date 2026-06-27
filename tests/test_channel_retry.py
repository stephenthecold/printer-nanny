"""Durable notification delivery: retry, backoff, and dead-letter.

Covers the regression where a failed channel send was recorded but never
retried -- alert dedupe then suppressed re-notification while the alert stayed
open, so a transient outage silently dropped the alert. Each (alert, channel)
send is now a NotificationDelivery row that the worker retries with backoff and
dead-letters after a cap.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from central import models as m
from central.channels import Notification
from central.channels.base import ChannelResult, NotificationChannel
from central.channels.delivery import (
    backoff_delay,
    record_dispatch,
    retry_due,
)


class _FlakyChannel(NotificationChannel):
    """Fails the first ``fail_times`` sends, then succeeds. Counts calls."""

    type = "flaky"

    def __init__(self, name="Flaky", fail_times=1):
        super().__init__(name, config={}, runtime={})
        self.fail_times = fail_times
        self.calls = 0

    def send(self, note: Notification) -> ChannelResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            return ChannelResult(ok=False, detail=f"boom {self.calls}")
        return ChannelResult(ok=True, detail="ok")


class _AlwaysFailChannel(NotificationChannel):
    type = "deadbeat"

    def send(self, note: Notification) -> ChannelResult:
        return ChannelResult(ok=False, detail="permanent failure")


def _open_alert(db) -> m.Alert:
    alert = m.Alert(
        type=m.AlertConditionType.supply_below,
        severity=m.EventSeverity.warning,
        state=m.AlertState.open,
        title="Low toner",
        detail="Black at 5%",
        dedupe_key="test:1",
    )
    db.add(alert)
    db.flush()
    return alert


def _note(alert) -> Notification:
    return Notification(title=alert.title, body=alert.detail, severity="warning", alert_id=alert.id)


# --------------------------------------------------------------------------- #
# Backoff schedule
# --------------------------------------------------------------------------- #
def test_backoff_is_exponential_and_capped():
    # base=60: 60, 120, 240, ... doubling each retry.
    assert backoff_delay(1, 60) == timedelta(seconds=60)
    assert backoff_delay(2, 60) == timedelta(seconds=120)
    assert backoff_delay(3, 60) == timedelta(seconds=240)
    # Capped at one hour no matter how many attempts have piled up.
    assert backoff_delay(50, 60) == timedelta(seconds=3600)


# --------------------------------------------------------------------------- #
# Failure persists a retryable delivery with a scheduled next_attempt_at
# --------------------------------------------------------------------------- #
def test_failure_persists_retryable_delivery_with_backoff(db):
    alert = _open_alert(db)
    now = datetime.now(timezone.utc)
    runtime = {"notifications.max_attempts": 5, "notifications.retry_base_seconds": 60}

    ch = _FlakyChannel(name="Flaky", fail_times=1)
    record_dispatch(db, alert.id, _note(alert), [ch], runtime=runtime, now=now)
    db.commit()

    d = db.query(m.NotificationDelivery).one()
    assert d.status == m.DeliveryStatus.failed   # retryable, not dropped
    assert d.attempts == 1
    assert d.channel_key == "Flaky"
    assert d.last_error == "boom 1"
    # next_attempt_at scheduled ~60s out (base backoff after the first attempt).
    assert d.next_attempt_at is not None
    delta = d.next_attempt_at.replace(tzinfo=timezone.utc) - now
    assert timedelta(seconds=55) <= delta <= timedelta(seconds=65)


# --------------------------------------------------------------------------- #
# Retry: fail once -> retried when due -> delivered, and stops (no double-send)
# --------------------------------------------------------------------------- #
def test_retry_marks_delivered_and_stops(db, monkeypatch):
    alert = _open_alert(db)
    now = datetime.now(timezone.utc)
    runtime = {"notifications.max_attempts": 5, "notifications.retry_base_seconds": 60}

    ch = _FlakyChannel(name="Flaky", fail_times=1)
    # Both the first send and the retry resolve "Flaky" through active_channels.
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])

    record_dispatch(db, alert.id, _note(alert), [ch], runtime=runtime, now=now)
    db.commit()
    assert db.query(m.NotificationDelivery).one().status == m.DeliveryStatus.failed

    # Not yet due -> retry is a no-op (backoff still pending).
    res_early = retry_due(db, runtime, now=now)
    assert res_early["deliveries_retried"] == 0

    # Past the backoff window -> retried and succeeds.
    later = now + timedelta(seconds=120)
    res = retry_due(db, runtime, now=later)
    assert res["deliveries_retried"] == 1
    assert res["deliveries_delivered"] == 1
    d = db.query(m.NotificationDelivery).one()
    assert d.status == m.DeliveryStatus.delivered
    assert d.attempts == 2
    assert d.next_attempt_at is None
    assert ch.calls == 2

    # A further retry pass does NOT re-send the now-delivered row.
    res_again = retry_due(db, runtime, now=later + timedelta(hours=1))
    assert res_again["deliveries_retried"] == 0
    assert ch.calls == 2


# --------------------------------------------------------------------------- #
# Max attempts -> dead-letter
# --------------------------------------------------------------------------- #
def test_max_attempts_dead_letters(db, monkeypatch):
    alert = _open_alert(db)
    now = datetime.now(timezone.utc)
    runtime = {"notifications.max_attempts": 3, "notifications.retry_base_seconds": 1}

    ch = _AlwaysFailChannel("Deadbeat", config={}, runtime={})
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])

    record_dispatch(db, alert.id, _note(alert), [ch], runtime=runtime, now=now)
    db.commit()
    d = db.query(m.NotificationDelivery).one()
    assert d.status == m.DeliveryStatus.failed and d.attempts == 1

    # Drive retries well past every backoff window; cap=3 -> dead on attempt 3.
    t = now
    for _ in range(5):
        t += timedelta(hours=2)
        retry_due(db, runtime, now=t)

    d = db.query(m.NotificationDelivery).one()
    assert d.status == m.DeliveryStatus.dead
    assert d.attempts == 3                 # stopped exactly at the cap
    assert d.next_attempt_at is None
    assert d.last_error == "permanent failure"

    # Dead-lettered rows are terminal: a later pass never revives them.
    before = d.attempts
    res = retry_due(db, runtime, now=t + timedelta(days=1))
    assert res["deliveries_retried"] == 0
    assert db.query(m.NotificationDelivery).one().attempts == before


# --------------------------------------------------------------------------- #
# Cap of 1 dead-letters immediately on the first failure
# --------------------------------------------------------------------------- #
def test_cap_of_one_dead_letters_on_first_failure(db):
    alert = _open_alert(db)
    runtime = {"notifications.max_attempts": 1, "notifications.retry_base_seconds": 60}
    ch = _AlwaysFailChannel("Deadbeat", config={}, runtime={})
    record_dispatch(db, alert.id, _note(alert), [ch], runtime=runtime)
    db.commit()
    d = db.query(m.NotificationDelivery).one()
    assert d.status == m.DeliveryStatus.dead
    assert d.next_attempt_at is None


# --------------------------------------------------------------------------- #
# Success on first send -> delivered, never re-tried
# --------------------------------------------------------------------------- #
def test_success_does_not_create_pending_retry(db, monkeypatch):
    alert = _open_alert(db)
    runtime = {"notifications.max_attempts": 5, "notifications.retry_base_seconds": 60}
    ch = _FlakyChannel(name="Flaky", fail_times=0)  # succeeds immediately
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])

    record_dispatch(db, alert.id, _note(alert), [ch], runtime=runtime)
    db.commit()
    d = db.query(m.NotificationDelivery).one()
    assert d.status == m.DeliveryStatus.delivered

    res = retry_due(db, runtime)
    assert res["deliveries_retried"] == 0
    assert ch.calls == 1


# --------------------------------------------------------------------------- #
# A retry whose alert resolved in the meantime is closed out, not paged
# --------------------------------------------------------------------------- #
def test_retry_skips_resolved_alert(db, monkeypatch):
    alert = _open_alert(db)
    now = datetime.now(timezone.utc)
    runtime = {"notifications.max_attempts": 5, "notifications.retry_base_seconds": 60}
    ch = _FlakyChannel(name="Flaky", fail_times=1)
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])

    record_dispatch(db, alert.id, _note(alert), [ch], runtime=runtime, now=now)
    db.commit()

    # Condition cleared before the retry fired.
    alert.state = m.AlertState.resolved
    db.commit()

    res = retry_due(db, runtime, now=now + timedelta(seconds=120))
    assert res["deliveries_retried"] == 0
    d = db.query(m.NotificationDelivery).one()
    assert d.status == m.DeliveryStatus.dead
    assert ch.calls == 1  # never sent again
