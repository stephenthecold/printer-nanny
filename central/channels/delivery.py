"""Durable, retryable notification delivery on top of the channel registry.

The alert lifecycle dedupes re-notification while an alert stays open, so a
channel send that fails on the first try used to be lost for good -- a brief
SMTP/Slack/webhook outage silently dropped the alert. This module makes every
(alert, channel) send durable: each attempt is persisted as a
``NotificationDelivery`` row, failures are scheduled for an exponential-backoff
retry, and a permanently-failing delivery is dead-lettered after a configurable
max-attempts cap instead of being retried forever.

Two entry points:

``record_dispatch`` -- called from the alert open paths in place of a bare
``dispatch``. It sends to each active channel, writes a delivery row per
channel, and returns the same ``(name, ChannelResult)`` list the callers
already use to populate ``Alert.notified_channels``.

``retry_due`` -- the worker job body (see ``jobs.retry_deliveries``). It re-sends
deliveries whose ``next_attempt_at`` is due, marking them ``delivered`` on
success and ``dead`` once the cap is reached. Idempotent and safe every cycle:
terminal rows (delivered / dead) are never touched again.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from central import models as m
from central.channels.base import ChannelResult, Notification, NotificationChannel
from central.channels.registry import active_channels, dispatch

# Hard ceiling on a single backoff step so attempts on a long outage don't drift
# out to days; the operator-tunable base only controls how fast we get here.
_BACKOFF_CAP_SECONDS = 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; treat them as UTC for comparisons."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def notification_payload(note: Notification) -> dict:
    """Freeze the Notification fields a retry needs into a JSON-safe dict.

    Attachments are deliberately omitted -- they're only used by scheduled
    reports (which don't go through this retry path) and aren't JSON-safe.
    """
    return {
        "title": note.title,
        "body": note.body,
        "severity": note.severity,
        "client_name": note.client_name,
        "site_name": note.site_name,
        "printer_label": note.printer_label,
        "alert_id": note.alert_id,
    }


def notification_from_payload(payload: dict) -> Notification:
    """Rebuild a Notification from a stored ``NotificationDelivery.payload``."""
    data = payload or {}
    return Notification(
        title=data.get("title", ""),
        body=data.get("body", ""),
        severity=data.get("severity", "warning"),
        client_name=data.get("client_name"),
        site_name=data.get("site_name"),
        printer_label=data.get("printer_label"),
        alert_id=data.get("alert_id"),
    )


def backoff_delay(attempts: int, base_seconds: int) -> timedelta:
    """Exponential backoff for the next attempt: ``base * 2**(attempts-1)``, capped.

    ``attempts`` is the number of attempts already made (>= 1 when scheduling a
    retry), so the first retry waits ``base`` seconds, the second ``2*base``,
    and so on, never exceeding ``_BACKOFF_CAP_SECONDS``.
    """
    base = max(1, int(base_seconds or 1))
    exp = max(0, int(attempts) - 1)
    # Cap the exponent too, so 2**exp can't overflow into an enormous int.
    if exp > 20:
        exp = 20
    delay = min(base * (2 ** exp), _BACKOFF_CAP_SECONDS)
    return timedelta(seconds=delay)


def _apply_result(
    delivery: m.NotificationDelivery,
    result: ChannelResult,
    now: datetime,
    *,
    max_attempts: int,
    base_seconds: int,
) -> None:
    """Fold one send outcome into a delivery row (status, attempts, backoff)."""
    delivery.attempts += 1
    if result.ok:
        delivery.status = m.DeliveryStatus.delivered
        delivery.last_error = None
        delivery.next_attempt_at = None
        return
    delivery.last_error = (result.detail or "")[:2000]
    if delivery.attempts >= max(1, max_attempts):
        # Exhausted the cap -- dead-letter it instead of retrying forever.
        delivery.status = m.DeliveryStatus.dead
        delivery.next_attempt_at = None
    else:
        delivery.status = m.DeliveryStatus.failed
        delivery.next_attempt_at = now + backoff_delay(delivery.attempts, base_seconds)


def record_dispatch(
    db: Session,
    alert_id: Optional[int],
    note: Notification,
    channels: Iterable[NotificationChannel],
    *,
    runtime: dict,
    now: Optional[datetime] = None,
) -> List[Tuple[str, ChannelResult]]:
    """Send ``note`` to each channel, persisting a retryable delivery per channel.

    Returns ``[(channel_name, ChannelResult), ...]`` so callers can keep
    populating ``Alert.notified_channels`` exactly as before. A failed send is
    not lost: its delivery row is left ``failed`` with a backoff
    ``next_attempt_at`` for ``retry_due`` to pick up (or ``dead`` immediately if
    the cap is 1).
    """
    now = now or _now()
    max_attempts = int(runtime.get("notifications.max_attempts", 5) or 5)
    base_seconds = int(runtime.get("notifications.retry_base_seconds", 60) or 60)

    payload = notification_payload(note)
    results = dispatch(note, channels)
    for name, result in results:
        delivery = m.NotificationDelivery(
            alert_id=alert_id,
            channel_key=name,
            attempts=0,
            payload=payload,
        )
        _apply_result(
            delivery, result, now, max_attempts=max_attempts, base_seconds=base_seconds
        )
        db.add(delivery)
    return results


def _due_deliveries(db: Session, now: datetime) -> List[m.NotificationDelivery]:
    """Non-terminal deliveries that are due for a (re)send this cycle.

    A row is due when its status is pending/failed and ``next_attempt_at`` is
    NULL (never scheduled) or in the past. delivered/dead rows are terminal and
    excluded, so the job never double-sends a success or revives a dead-letter.
    """
    stmt = select(m.NotificationDelivery).where(
        m.NotificationDelivery.status.in_(
            (m.DeliveryStatus.pending, m.DeliveryStatus.failed)
        ),
        or_(
            m.NotificationDelivery.next_attempt_at.is_(None),
            m.NotificationDelivery.next_attempt_at <= now,
        ),
    )
    return [
        d
        for d in db.scalars(stmt)
        if d.next_attempt_at is None or _aware(d.next_attempt_at) <= now
    ]


def retry_due(db: Session, runtime: dict, now: Optional[datetime] = None) -> dict:
    """Re-send every due pending/failed delivery; mark delivered/dead as warranted.

    Channels are rebuilt once from ``active_channels`` and matched to each
    delivery by ``channel_key``. A delivery whose channel is no longer active
    (disabled in settings) is left untouched -- nothing to send through, and it
    becomes due again once re-enabled. Returns a small summary the worker loop
    and tests can assert on.
    """
    now = now or _now()
    max_attempts = int(runtime.get("notifications.max_attempts", 5) or 5)
    base_seconds = int(runtime.get("notifications.retry_base_seconds", 60) or 60)

    channels = {ch.name: ch for ch in active_channels(runtime)}
    retried = delivered = dead = skipped = 0
    for delivery in _due_deliveries(db, now):
        channel = channels.get(delivery.channel_key)
        if channel is None:
            skipped += 1
            continue
        note = notification_from_payload(delivery.payload)
        # Resolved alerts no longer need notifying; close the loop quietly so a
        # transient outage that has since cleared doesn't page on stale news.
        if delivery.alert_id is not None:
            alert = db.get(m.Alert, delivery.alert_id)
            if alert is not None and alert.state == m.AlertState.resolved:
                delivery.status = m.DeliveryStatus.dead
                delivery.last_error = "alert resolved before delivery"
                delivery.next_attempt_at = None
                dead += 1
                continue
        retried += 1
        result = dispatch(note, [channel])[0][1]
        _apply_result(
            delivery, result, now, max_attempts=max_attempts, base_seconds=base_seconds
        )
        if delivery.status == m.DeliveryStatus.delivered:
            delivered += 1
        elif delivery.status == m.DeliveryStatus.dead:
            dead += 1
    db.commit()
    return {
        "deliveries_retried": retried,
        "deliveries_delivered": delivered,
        "deliveries_dead": dead,
        "deliveries_skipped": skipped,
    }
