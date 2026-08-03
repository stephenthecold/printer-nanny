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

Zero routed channels are handled explicitly. Dispatching to an empty channel
list used to write no row at all, so ``retry_due`` had nothing to find -- while
the alert itself was open, which makes dedupe suppress every future attempt for
that condition. Re-enabling a channel afterwards did NOT recover it: the alert
was lost permanently, with no record anywhere that a notification was owed. That
state is reachable in practice (a settings save with ``sections=None`` clears
every channel's ``enabled`` flag), so a single channel-less **owed** row is now
persisted instead, and ``retry_due`` fans it out to whatever channels exist the
moment one comes back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Set, Tuple, Union

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from central import models as m
from central.audit import record as audit_record
from central.channels.base import ChannelResult, Notification, NotificationChannel
from central.channels.registry import active_channels, dispatch

# Hard ceiling on a single backoff step so attempts on a long outage don't drift
# out to days; the operator-tunable base only controls how fast we get here.
_BACKOFF_CAP_SECONDS = 3600

# Reserved ``NotificationDelivery.channel_key`` marking a notification that was
# owed but had nowhere to go -- deliberately not a legal channel name (the
# Settings-page channels are Email / FreeScout / Teams / Webhook / Slack).
UNROUTED_CHANNEL_KEY = "*unrouted*"

# What an owed notification is called on the alert row / in the dashboard.
UNROUTED_CHANNEL_LABEL = "no channel routed"

UNROUTED_DETAIL = (
    "no notification channel was enabled or routed -- queued for delivery "
    "once one is configured"
)

# Reserved ``channel_key`` for a notification a suppression window held (deferred)
# or dropped (suppressed). Like UNROUTED_CHANNEL_KEY it names no real channel,
# because at hold time we deliberately haven't chosen one yet -- the digest fans
# out to whatever is enabled when the window closes, which is what makes a
# channel enabled *during* quiet hours still receive the morning batch.
WITHHELD_CHANNEL_KEY = "*withheld*"

# How long an owed notification waits for a destination before it is
# dead-lettered. Long enough that an operator back from a week off still
# recovers the alert; short enough that a permanently-unconfigured system
# terminates its owed rows instead of holding them forever. Owed rows normally
# terminate long before this, the moment their alert resolves.
UNROUTED_GIVE_UP = timedelta(days=7)


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
    delivery: "Union[m.NotificationDelivery, m.EventDelivery]",
    result: ChannelResult,
    now: datetime,
    *,
    max_attempts: int,
    base_seconds: int,
) -> None:
    """Fold one send outcome into a delivery row (status, attempts, backoff).

    ``ok`` without ``sent`` is recorded as ``skipped``, never ``delivered``: a
    severity gate or an unconfigured dry-run transmits nothing, and marking it
    delivered made the durable log (and the alerts page) claim a send that never
    happened. ``skipped`` is terminal -- ``_due_deliveries`` only looks at
    pending/failed -- so it is never retried and never double-counted.

    Deliberately structural rather than typed to one model: the outbound event
    bus (``central.events.delivery``) folds ITS outcomes through this same
    function, against ``EventDelivery`` rows shaped to match
    (``status``/``attempts``/``last_error``/``next_attempt_at``, same
    ``DeliveryStatus`` vocabulary). There is one backoff policy and one
    dead-letter rule in this codebase; a second copy is the one that would drift,
    and the rule most likely to drift is the ``sent``-vs-``ok`` distinction
    above, which took a shipped defect to arrive at.
    """
    delivery.attempts += 1
    if result.ok:
        delivery.status = (
            m.DeliveryStatus.delivered if result.sent else m.DeliveryStatus.skipped
        )
        delivery.last_error = None if result.sent else (result.detail or "")[:2000]
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


def _record_owed(
    db: Session, alert_id: Optional[int], payload: dict
) -> Tuple[str, ChannelResult]:
    """Persist the channel-less ``pending`` row for a notification owed to nobody.

    ``next_attempt_at`` is left NULL (== due now) so the very next ``retry_due``
    sweep reconsiders it: the instant a channel is enabled again the owed
    notification is fanned out and delivered. An audit row makes the loss visible
    to an operator at ``/manage/audit`` even before that -- one row per owed
    notification, which alert dedupe already bounds to one per condition.
    """
    db.add(m.NotificationDelivery(
        alert_id=alert_id,
        channel_key=UNROUTED_CHANNEL_KEY,
        status=m.DeliveryStatus.pending,
        attempts=0,
        last_error=UNROUTED_DETAIL,
        next_attempt_at=None,
        payload=payload,
    ))
    audit_record(
        db, None, None, "notification.unroutable",
        target=("alert:%d" % alert_id) if alert_id is not None else "",
        detail="%s: %s" % (payload.get("title") or "notification", UNROUTED_DETAIL),
    )
    return (
        UNROUTED_CHANNEL_LABEL,
        ChannelResult(ok=False, detail=UNROUTED_DETAIL, sent=False),
    )


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

    ``channels`` being empty is a real state, not a no-op: every channel may be
    disabled, or routing/severity may have excluded all of them. One channel-less
    ``pending`` row is persisted for that case (see ``UNROUTED_CHANNEL_KEY``) so
    the notification stays on the books, and the returned result says plainly
    that nothing was delivered rather than leaving the alert looking un-notified.
    """
    now = now or _now()
    max_attempts = int(runtime.get("notifications.max_attempts", 5) or 5)
    base_seconds = int(runtime.get("notifications.retry_base_seconds", 60) or 60)

    payload = notification_payload(note)
    results = dispatch(note, channels)
    if not results:
        return [_record_owed(db, alert_id, payload)]
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
    NULL (never scheduled) or in the past. delivered/dead/skipped rows are
    terminal and excluded, so the job never double-sends a success, revives a
    dead-letter, or retries a deterministic severity skip.
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


def _close(delivery: m.NotificationDelivery, reason: str) -> None:
    """Dead-letter a delivery without counting an attempt against it."""
    delivery.status = m.DeliveryStatus.dead
    delivery.last_error = reason[:2000]
    delivery.next_attempt_at = None


def _alert_resolved(db: Session, delivery: m.NotificationDelivery) -> bool:
    """Has the alert this delivery is for already been resolved?"""
    if delivery.alert_id is None:
        return False
    alert = db.get(m.Alert, delivery.alert_id)
    return alert is not None and alert.state == m.AlertState.resolved


def _delivered_after(db: Session, alert_id: Optional[int], after_id: int) -> Set[str]:
    """Channels this alert reached in a dispatch LATER than row ``after_id``.

    Stops the fan-out re-paging a channel that an escalation re-notify already
    reached while the notification sat owed. Bounded on purpose: a delivery
    written *before* the owed row belongs to an earlier notification and must not
    suppress this one -- otherwise an escalation that fell into the owed state
    would be silently swallowed by the very open-time send it was following up.

    Ordered by ``id`` rather than timestamp: it is monotonic by insertion, so it
    can't be defeated by two dispatches landing in the same clock tick.
    """
    if alert_id is None:
        return set()
    return set(db.scalars(
        select(m.NotificationDelivery.channel_key).where(
            m.NotificationDelivery.alert_id == alert_id,
            m.NotificationDelivery.status == m.DeliveryStatus.delivered,
            m.NotificationDelivery.id > after_id,
        )
    ))


def channel_badges(results: List[Tuple[str, ChannelResult]]) -> List[dict]:
    """Render dispatch results into ``Alert.notified_channels`` badge dicts.

    Shared by the initial dispatch (worker.jobs._notify_alert) and the owed
    fan-out so the two can't drift. ``sent`` is carried alongside ``ok`` because
    the template needs three states, not two: delivered, not-sent (a severity
    gate or unconfigured channel -- ok, but nobody was told), and failed. Older
    rows predate the key and read as ``sent`` missing, which the template treats
    as sent so historical alerts keep rendering unchanged.
    """
    return [
        {"channel": name, "ok": res.ok, "sent": res.sent, "detail": res.detail}
        for name, res in results
    ]


def _restamp_alert(
    db: Session,
    alert_id: Optional[int],
    results: List[Tuple[str, ChannelResult]],
    now: datetime,
) -> None:
    """Replace an alert's "nobody was told" badge with what the fan-out achieved.

    Only the owed path does this. Without it the Alerts inbox would keep showing
    a red ``no channel routed`` badge on an alert that has since been delivered
    -- the same class of dishonest UI the owed row exists to remove.
    ``last_notified_at`` is stamped because the alert genuinely was notified now,
    which also stops an escalation firing a duplicate seconds later.
    """
    if alert_id is None or not results:
        return
    alert = db.get(m.Alert, alert_id)
    if alert is None:
        return
    alert.notified_channels = channel_badges(results)
    alert.last_notified_at = now


def _fan_out_owed(
    db: Session,
    delivery: m.NotificationDelivery,
    now: datetime,
    *,
    channels: "Dict[str, NotificationChannel]",
    max_attempts: int,
    base_seconds: int,
) -> dict:
    """Turn a channel-less owed row into real per-channel deliveries.

    The owed row is REUSED as the delivery for the first channel (so one owed
    notification never becomes two rows for one destination) and a sibling row is
    added for each remaining channel -- exactly the set of rows ``record_dispatch``
    would have written had a channel been enabled at open time.

    With still no channel the row is left owed and **no attempt is burned**: an
    unconfigured system must not eat the retry budget before the operator has a
    chance to fix it. It terminates when its alert resolves (handled by the
    caller) or when ``UNROUTED_GIVE_UP`` has elapsed, so nothing sits pending
    forever.
    """
    out = {"retried": 0, "delivered": 0, "dead": 0, "owed": 0, "not_sent": 0}
    if not channels:
        age = now - (_aware(delivery.created_at) or now)
        if age < UNROUTED_GIVE_UP:
            out["owed"] = 1
            return out
        reason = (
            "no notification channel was enabled within %dd -- gave up"
            % UNROUTED_GIVE_UP.days
        )
        _close(delivery, reason)
        audit_record(
            db, None, None, "notification.gave_up",
            target=("alert:%d" % delivery.alert_id) if delivery.alert_id else "",
            detail="%s: %s" % ((delivery.payload or {}).get("title") or "notification", reason),
        )
        out["dead"] = 1
        return out

    already = _delivered_after(db, delivery.alert_id, delivery.id)
    targets = [ch for name, ch in channels.items() if name not in already]
    if not targets:
        _close(delivery, "superseded by a later delivery to %s" % ", ".join(sorted(already)))
        out["dead"] = 1
        return out

    note = notification_from_payload(delivery.payload)
    # Reuse this row for the first destination; add siblings for the rest.
    delivery.channel_key = targets[0].name
    rows = [delivery]
    for channel in targets[1:]:
        sibling = m.NotificationDelivery(
            alert_id=delivery.alert_id,
            channel_key=channel.name,
            attempts=0,
            payload=delivery.payload,
        )
        db.add(sibling)
        rows.append(sibling)
    results: List[Tuple[str, ChannelResult]] = []
    for row, channel in zip(rows, targets):
        out["retried"] += 1
        result = dispatch(note, [channel])[0][1]
        results.append((channel.name, result))
        _apply_result(
            row, result, now, max_attempts=max_attempts, base_seconds=base_seconds
        )
        if row.status == m.DeliveryStatus.delivered:
            out["delivered"] += 1
        elif row.status == m.DeliveryStatus.dead:
            out["dead"] += 1
        elif row.status == m.DeliveryStatus.skipped:
            out["not_sent"] += 1
    # An enabled-but-unconfigured channel is not a destination: if EVERY target
    # merely skipped, this notification still has nowhere to go, so the owed row
    # goes back to owed rather than terminating. Terminating here would undo the
    # recovery property owed rows exist for -- pasting a webhook URL later must
    # still deliver the backlog, exactly as enabling a channel does. No attempt
    # is burned, and the row still terminates on alert-resolve or UNROUTED_GIVE_UP.
    #
    # Guarded on *every* result being a skip, not on "nothing delivered": a real
    # failure (ok=False) owns a retryable failed row with backoff, and reverting
    # that to owed would throw away its attempt count and retry schedule.
    if results and all(r.ok and not r.sent for _, r in results):
        delivery.channel_key = UNROUTED_CHANNEL_KEY
        delivery.status = m.DeliveryStatus.pending
        delivery.attempts = 0
        delivery.last_error = UNROUTED_DETAIL
        delivery.next_attempt_at = None
        # Siblings created above would duplicate the owed row; drop them.
        for sibling in rows[1:]:
            db.delete(sibling)
        out["retried"] = 0
        out["not_sent"] = 0
        out["owed"] = 1
        return out
    _restamp_alert(db, delivery.alert_id, results, now)
    return out


def retry_due(db: Session, runtime: dict, now: Optional[datetime] = None) -> dict:
    """Re-send every due pending/failed delivery; mark delivered/dead as warranted.

    Channels are rebuilt once from ``active_channels`` and matched to each
    delivery by ``channel_key``. A delivery whose channel is no longer active
    (disabled in settings) is left untouched -- nothing to send through, and it
    becomes due again once re-enabled. A channel-less **owed** row (nothing was
    routable when the alert opened) is fanned out to whatever channels exist now,
    which is what makes re-enabling a channel actually recover the alert.

    The resolved-alert check runs BEFORE the channel lookup: a row whose channel
    is gone must still close out when its alert resolves, otherwise rows for
    disabled channels -- owed rows included -- accumulate forever on a system
    that never gets a channel back. Returns a small summary the worker loop and
    tests can assert on.
    """
    now = now or _now()
    max_attempts = int(runtime.get("notifications.max_attempts", 5) or 5)
    base_seconds = int(runtime.get("notifications.retry_base_seconds", 60) or 60)

    channels = {ch.name: ch for ch in active_channels(runtime)}
    retried = delivered = dead = skipped = owed = not_sent = 0
    for delivery in _due_deliveries(db, now):
        # Resolved alerts no longer need notifying; close the loop quietly so a
        # transient outage that has since cleared doesn't page on stale news.
        if _alert_resolved(db, delivery):
            _close(delivery, "alert resolved before delivery")
            dead += 1
            continue
        if delivery.channel_key == UNROUTED_CHANNEL_KEY:
            out = _fan_out_owed(
                db, delivery, now, channels=channels,
                max_attempts=max_attempts, base_seconds=base_seconds,
            )
            retried += out["retried"]
            delivered += out["delivered"]
            dead += out["dead"]
            owed += out["owed"]
            not_sent += out["not_sent"]
            continue
        channel = channels.get(delivery.channel_key)
        if channel is None:
            skipped += 1
            continue
        note = notification_from_payload(delivery.payload)
        retried += 1
        result = dispatch(note, [channel])[0][1]
        _apply_result(
            delivery, result, now, max_attempts=max_attempts, base_seconds=base_seconds
        )
        if delivery.status == m.DeliveryStatus.delivered:
            delivered += 1
        elif delivery.status == m.DeliveryStatus.dead:
            dead += 1
        elif delivery.status == m.DeliveryStatus.skipped:
            not_sent += 1
    db.commit()
    return {
        "deliveries_retried": retried,
        "deliveries_delivered": delivered,
        "deliveries_dead": dead,
        # Channel is no longer active, so the row was left alone for a later
        # cycle. Distinct from deliveries_not_sent, which DID run and
        # terminated without transmitting.
        "deliveries_skipped": skipped,
        # Terminated as DeliveryStatus.skipped: the channel reported success but
        # transmitted nothing (severity gate / unconfigured dry-run). Counted
        # apart from delivered so a green cycle can't hide a silent one.
        "deliveries_not_sent": not_sent,
        # Notifications still owed with nowhere to send them -- non-zero means an
        # operator needs to enable a channel (surfaced on the Alerts page).
        "deliveries_owed": owed,
    }


# --------------------------------------------------------------------------- #
# Deferred notifications -- the quiet-hours digest.
#
# A held notification is a `deferred` row whose next_attempt_at is the instant its
# window closes, so nothing polls and no second scheduler exists: the row simply
# becomes due. `_due_deliveries` deliberately does NOT return deferred rows --
# retry_due would re-send them individually, which is the noise the window was
# meant to prevent. flush_deferred owns them and batches instead.
# --------------------------------------------------------------------------- #
def _due_deferred(db: Session, now: datetime) -> List[m.NotificationDelivery]:
    """Held rows whose window has now closed."""
    rows = db.scalars(
        select(m.NotificationDelivery).where(
            m.NotificationDelivery.status == m.DeliveryStatus.deferred
        )
    )
    return [
        r for r in rows
        if r.next_attempt_at is None or _aware(r.next_attempt_at) <= now
    ]


def _digest_note(entries: List[dict], resolved_count: int, client_name: Optional[str]) -> Notification:
    """Render N held notifications into one message.

    Severity is the highest among the entries, so a digest containing a critical
    is not gated out by a channel whose minimum is critical. ``resolved_count`` is
    stated rather than dropped: conditions that cleared while held are not sent
    individually (nobody wants to be paged about a fixed problem at 07:00), but
    silently discarding them would leave an operator unable to answer "what
    happened overnight?".
    """
    rank = {"info": 0, "warning": 1, "critical": 2}
    severity = "info"
    for e in entries:
        if rank.get(e.get("severity", "info"), 0) > rank.get(severity, 0):
            severity = e.get("severity", "info")

    lines = []
    for e in entries:
        label = e.get("printer_label") or ""
        title = e.get("title") or "alert"
        lines.append("- [%s] %s%s" % (
            e.get("severity", "info"), title, (" (%s)" % label) if label else ""
        ))
    if resolved_count:
        lines.append(
            "- (%d further condition(s) opened and cleared while held; not listed)"
            % resolved_count
        )
    scope = (" for %s" % client_name) if client_name else ""
    title = "%d alert%s held during quiet hours%s" % (
        len(entries), "" if len(entries) == 1 else "s", scope,
    )
    return Notification(
        title=title,
        body="\n".join(lines),
        severity=severity,
        client_name=client_name,
    )


def _client_of(db: Session, delivery: m.NotificationDelivery):
    """(client_id, client_name) for a held row, via its alert's printer."""
    if delivery.alert_id is None:
        return (None, None)
    alert = db.get(m.Alert, delivery.alert_id)
    if alert is None or alert.printer_id is None:
        return (None, None)
    printer = db.get(m.Printer, alert.printer_id)
    if printer is None or printer.client_id is None:
        return (None, None)
    client = db.get(m.Client, printer.client_id)
    return (printer.client_id, client.name if client else None)


def flush_deferred(
    db: Session, runtime: dict, now: Optional[datetime] = None
) -> dict:
    """Deliver every held notification whose window has closed, as a digest.

    Grouped per client so an MSP tech gets one message per customer rather than
    one per alert or one mixing every customer together. Rows whose alert has
    since resolved are closed without a send and only counted in the digest --
    consistent with ``retry_due``, which already refuses to page on stale news.

    Honest terminal states throughout: a digest that reaches nobody (no channel
    enabled) leaves its rows ``pending`` as owed rather than claiming delivery, so
    the existing owed-recovery path picks them up.
    """
    now = now or _now()
    channels = list(active_channels(runtime))
    due = _due_deferred(db, now)
    if not due:
        return {"digests_sent": 0, "deferred_delivered": 0, "deferred_resolved": 0}

    groups: Dict[Optional[int], List[m.NotificationDelivery]] = {}
    resolved: Dict[Optional[int], int] = {}
    names: Dict[Optional[int], Optional[str]] = {}
    for row in due:
        client_id, client_name = _client_of(db, row)
        names[client_id] = client_name
        if _alert_resolved(db, row):
            _close(row, "alert resolved while held by a suppression window")
            resolved[client_id] = resolved.get(client_id, 0) + 1
            continue
        groups.setdefault(client_id, []).append(row)

    digests = delivered = 0
    for client_id, rows in groups.items():
        entries = [r.payload or {} for r in rows]
        note = _digest_note(entries, resolved.get(client_id, 0), names.get(client_id))
        if not channels:
            # Nowhere to send it. Hand the rows to the owed path rather than
            # marking them anything that implies delivery -- re-enabling a
            # channel must still produce the digest.
            for row in rows:
                row.channel_key = UNROUTED_CHANNEL_KEY
                row.status = m.DeliveryStatus.pending
                row.next_attempt_at = None
                row.last_error = UNROUTED_DETAIL
            continue
        results = dispatch(note, channels)
        digests += 1
        sent_any = any(r.ok and r.sent for _, r in results)
        detail = "; ".join("%s: %s" % (n, r.detail) for n, r in results)[:2000]
        for row in rows:
            row.attempts += 1
            row.channel_key = results[0][0] if results else WITHHELD_CHANNEL_KEY
            if sent_any:
                row.status = m.DeliveryStatus.delivered
                row.last_error = None
                delivered += 1
            else:
                # Every channel skipped (severity gate / dry-run). Not a delivery,
                # and not a failure worth retrying -- same call as _apply_result.
                row.status = m.DeliveryStatus.skipped
                row.last_error = detail
            row.next_attempt_at = None
        _restamp_digest(db, rows, results, now)

    db.commit()
    return {
        "digests_sent": digests,
        "deferred_delivered": delivered,
        "deferred_resolved": sum(resolved.values()),
    }


def _restamp_digest(
    db: Session,
    rows: List[m.NotificationDelivery],
    results: List[Tuple[str, ChannelResult]],
    now: datetime,
) -> None:
    """Replace each alert's "held" badge with what the digest actually achieved.

    Without this the Alerts page would keep showing "held by window ... until
    07:00" on an alert that was delivered at 07:00 -- the same stale-badge
    dishonesty ``_restamp_alert`` exists to remove on the owed path.
    """
    badges = channel_badges(results)
    for row in rows:
        if row.alert_id is None:
            continue
        alert = db.get(m.Alert, row.alert_id)
        if alert is None:
            continue
        alert.notified_channels = badges
        if any(r.ok and r.sent for _, r in results):
            alert.last_notified_at = now
