"""Delivering events -- the envelope on the wire, and the sweeper that retries.

REUSES THE EXISTING RETRY MACHINERY, DELIBERATELY
-------------------------------------------------
There is exactly one backoff policy and one dead-letter rule in this codebase.
``channels.delivery.backoff_delay`` computes the schedule and
``channels.delivery._apply_result`` folds one outcome into a delivery row --
both are imported and used here unchanged, against ``EventDelivery`` rows shaped
to match ``NotificationDelivery`` (same ``DeliveryStatus`` vocabulary, same
``attempts`` / ``last_error`` / ``next_attempt_at`` triple). A second
implementation would be the one that drifts, and the specific thing that would
drift is the ``sent``-vs-``ok`` rule below, which took a real defect to get
right the first time.

``ChannelResult.sent`` is carried through with the same meaning it has for
notifications: ``ok`` answers "did anything go wrong", ``sent`` answers "did a
message actually leave the building". Everything here that returns success
without transmitting -- a disabled subscription, a type the subscriber does not
want, a delivery whose subscription has since been re-scoped to another tenant
-- sets ``sent=False``, so it terminates as ``DeliveryStatus.skipped`` and the
cycle summary counts it apart from a real delivery. A green number that includes
sends that never happened is the failure this separation exists to prevent.

THE TENANCY RE-CHECK
--------------------
Scope is checked at fan-out (``events.emit``) and again here, immediately before
the request. That is not paranoia about the first check: ``client_id`` on a
subscription is editable, so an operator re-scoping a global subscription to one
customer would otherwise ship every other customer's already-queued events to
it. Those rows are dead-lettered with a stated reason and audited as
``event_delivery.refused``, matching how ``printer_assignment.refused`` records
the same class of event.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import httpx
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from central import models as m
from central.audit import record as audit_record
from central.channels.base import ChannelResult
from central.channels.delivery import _apply_result
from central.events import signing
from central.events.destinations import DestinationError, validate_url
from central.events.emit import scope_allows, wants_type
from central.secrets import decrypt_value

log = logging.getLogger("central.events")

#: Headers a consumer routes and de-duplicates on without parsing the body.
HEADER_EVENT_ID = "X-PrinterNanny-Event-Id"
HEADER_EVENT_TYPE = "X-PrinterNanny-Event-Type"
HEADER_IDEMPOTENCY = "X-PrinterNanny-Idempotency-Key"
HEADER_ATTEMPT = "X-PrinterNanny-Delivery-Attempt"

USER_AGENT = "PrinterNanny-Events/1"

#: How much of a subscriber's response body is read at all. A webhook endpoint
#: has nothing useful to say in a reply, and an endpoint that returns megabytes
#: (or streams forever) must not be able to spend this worker's memory. The body
#: is streamed and abandoned at this many bytes.
MAX_RESPONSE_BYTES = 2048

#: How much of it is stored as the error detail. Rendered in the UI, so short.
ERROR_SNIPPET = 300

#: Deliveries attempted per worker cycle. Bounded, unlike the notification
#: sweeper it is modelled on: a backlog against one dead subscriber shares its
#: cycle with alert evaluation, and must not be able to spend the whole of it.
BATCH_LIMIT = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; treat them as UTC (as elsewhere)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# The envelope
# --------------------------------------------------------------------------- #
def build_envelope(event: m.OutboundEvent) -> dict:
    """The JSON document a subscriber receives.

    Flat, versioned and boring on purpose. ``id`` is stable across retries and
    ``idempotency_key`` is stable across the whole occurrence, which is the pair
    a consumer needs: the first answers "have I processed this exact delivery",
    the second answers "have I already acted on this fact".
    """
    occurred = _aware(event.occurred_at) or _now()
    return {
        "id": event.uid,
        "type": event.type,
        "version": event.version,
        "idempotency_key": event.idempotency_key,
        "occurred_at": occurred.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "source": "printer-nanny",
        "data": event.data or {},
    }


def serialize(envelope: dict) -> bytes:
    """The exact bytes that are signed AND sent -- never serialised twice.

    ``sort_keys`` and a compact separator make the body reproducible, which is
    what lets a test assert a signature against a fixed string and what lets an
    operator re-derive one from a captured request. The signature covers these
    bytes, so re-encoding the dict anywhere between signing and sending would
    produce a valid-looking delivery that every subscriber rejects.

    ``ensure_ascii`` is left at its default: a device name in Japanese travels as
    ``\\uXXXX`` escapes, which every JSON parser decodes back to the original
    characters and no transport can mangle.
    """
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_headers(
    secret: str, body: bytes, envelope: dict, attempt: int, timestamp: Optional[int] = None
) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        HEADER_EVENT_ID: str(envelope.get("id") or ""),
        HEADER_EVENT_TYPE: str(envelope.get("type") or ""),
        HEADER_IDEMPOTENCY: str(envelope.get("idempotency_key") or ""),
        # Lets a consumer tell a first delivery from a retry without keeping
        # state; a retry carries the same id, so "attempt 3" is the only in-band
        # signal that we are repeating ourselves.
        HEADER_ATTEMPT: str(attempt),
        signing.SIGNATURE_HEADER: signing.sign(secret, body, timestamp),
    }


# --------------------------------------------------------------------------- #
# One send
# --------------------------------------------------------------------------- #
def _post(
    url: str, body: bytes, headers: Dict[str, str], timeout: float
) -> "tuple":
    """POST and return ``(status_code, first bytes of the response)``.

    Streamed and truncated (see MAX_RESPONSE_BYTES) rather than read whole.
    Redirects are NOT followed: a pre-flight address check is defeated in one
    line by a destination answering ``302 Location: http://169.254.169.254/``,
    and there is no webhook worth a redirect anyway -- a subscriber that has
    moved should be re-pointed in the subscription.
    """
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream("POST", url, content=body, headers=headers) as resp:
            chunks = b""
            for chunk in resp.iter_bytes():
                chunks += chunk
                if len(chunks) >= MAX_RESPONSE_BYTES:
                    break
            return resp.status_code, chunks[:MAX_RESPONSE_BYTES]


def send_one(
    subscription: m.EventSubscription,
    event: m.OutboundEvent,
    *,
    runtime: dict,
    attempt: int = 1,
    timestamp: Optional[int] = None,
) -> "tuple":
    """Sign and POST one event to one destination.

    Returns ``(ChannelResult, response_status_or_None)``. Does no tenancy
    checking of its own -- callers must have cleared that first, and there is
    exactly one place (``scope_allows``) where the answer is decided.
    """
    timeout = float(runtime.get("events.timeout_seconds", 15) or 15)
    allow_private = bool(runtime.get("events.allow_private_destinations", False))

    secret = str(decrypt_value(subscription.secret) or "")
    if not secret:
        # A rotated SECRET_KEY leaves an undecryptable secret behind. Signing
        # with "" would produce a signature every subscriber rejects while this
        # end reported a clean send, so refuse to transmit and say why.
        return (
            ChannelResult(
                ok=False,
                detail="signing secret could not be decrypted (SECRET_KEY rotated?) "
                       "-- rotate this subscription's secret",
                sent=False,
            ),
            None,
        )

    try:
        url = validate_url(
            subscription.url, allow_private=allow_private, require_resolvable=True
        )
    except DestinationError as exc:
        # sent=False stated explicitly, unlike the transport failures below: a
        # refused destination is one we provably never contacted, where a
        # timeout or a 5xx may well have left bytes on the wire. The difference
        # matters to anyone reading the log to answer "could the subscriber have
        # acted on this?".
        return (
            ChannelResult(
                ok=False, detail="destination refused: %s" % exc, sent=False
            ),
            None,
        )

    envelope = build_envelope(event)
    body = serialize(envelope)
    headers = build_headers(secret, body, envelope, attempt, timestamp)

    try:
        status, snippet = _post(url, body, headers, timeout)
    except httpx.HTTPError as exc:
        # Never interpolate the response or the secret; httpx messages quote the
        # URL, which is operator-supplied and safe to show, and nothing else.
        return ChannelResult(ok=False, detail="request error: %s" % exc), None
    except Exception as exc:  # noqa: BLE001
        # httpx does not wrap everything: an IDN host raises UnicodeError, a
        # broken proxy environment raises from the transport, and neither is an
        # HTTPError. One hostile destination must not be able to abort the whole
        # sweep -- the queue behind it belongs to other subscribers. Logged with
        # a traceback, recorded as an ordinary retryable failure.
        log.exception("unhandled error delivering to subscription %s", subscription.id)
        return (
            ChannelResult(ok=False, detail="unhandled error: %s" % type(exc).__name__),
            None,
        )

    if 200 <= status < 300:
        return ChannelResult(ok=True, detail="delivered (%d)" % status), status
    text = snippet.decode("utf-8", "replace")[:ERROR_SNIPPET]
    return ChannelResult(ok=False, detail="HTTP %d: %s" % (status, text)), status


def _stamp(subscription: m.EventSubscription, result: ChannelResult, now: datetime) -> None:
    """Per-subscription bookkeeping for the operator's page."""
    subscription.last_attempt_at = now
    subscription.last_ok = bool(result.ok and result.sent)
    subscription.last_error = None if result.ok else (result.detail or "")[:500]


# --------------------------------------------------------------------------- #
# The sweeper
# --------------------------------------------------------------------------- #
def _due(db: Session, now: datetime) -> List[m.EventDelivery]:
    """Non-terminal deliveries due this cycle, oldest first (see BATCH_LIMIT)."""
    stmt = (
        select(m.EventDelivery)
        .where(
            m.EventDelivery.status.in_(
                (m.DeliveryStatus.pending, m.DeliveryStatus.failed)
            ),
            or_(
                m.EventDelivery.next_attempt_at.is_(None),
                m.EventDelivery.next_attempt_at <= now,
            ),
        )
        .order_by(m.EventDelivery.id)
        .limit(BATCH_LIMIT)
    )
    return [
        d
        for d in db.scalars(stmt)
        if d.next_attempt_at is None or _aware(d.next_attempt_at) <= now
    ]


def _terminate(row: m.EventDelivery, status: "m.DeliveryStatus", reason: str) -> None:
    row.status = status
    row.last_error = reason[:2000]
    row.next_attempt_at = None


def deliver_due(
    db: Session, runtime: dict, now: Optional[datetime] = None
) -> dict:
    """Send every due event delivery; retry, dead-letter or skip as warranted.

    Idempotent and safe every cycle. Terminal rows (delivered / dead / skipped)
    are never revisited, so a success is never double-sent and a dead letter is
    never revived -- the same contract ``retry_due`` holds for notifications.
    """
    now = now or _now()
    if not runtime.get("events.enabled", True):
        # A hard off switch, distinct from every subscription being disabled:
        # rows stay queued and go out when it is switched back on.
        return {"events_delivered": 0, "events_paused": True}

    max_attempts = int(runtime.get("events.max_attempts", 8) or 8)
    base_seconds = int(runtime.get("events.retry_base_seconds", 60) or 60)

    sent = failed = dead = skipped = not_sent = 0
    for row in _due(db, now):
        subscription = db.get(m.EventSubscription, row.subscription_id)
        event = db.get(m.OutboundEvent, row.event_id)
        if subscription is None or event is None:
            # Reachable: the FK cascade that would prevent it is not enforced on
            # SQLite (no PRAGMA foreign_keys), so an orphan is a real state on
            # the dev/test backend. Dead-lettered rather than left as a row that
            # can never be sent and would be retried forever.
            _terminate(row, m.DeliveryStatus.dead, "subscription or event no longer exists")
            dead += 1
            continue

        if not subscription.enabled:
            # Left untouched, exactly as retry_due leaves a delivery whose
            # channel is currently disabled: re-enabling must deliver the
            # backlog, not find it terminated.
            skipped += 1
            continue

        if not scope_allows(subscription, event.client_id):
            # The subscription was re-scoped after this row was queued. This is
            # the tenancy invariant, and the answer is refusal, not delivery.
            reason = (
                "refused: subscription is scoped to client %s, event belongs to "
                "client %s" % (subscription.client_id, event.client_id)
            )
            _terminate(row, m.DeliveryStatus.dead, reason)
            audit_record(
                db, None, None, "event_delivery.refused",
                target="event_subscription:%d" % subscription.id,
                detail="%s (%s)" % (reason, event.type),
            )
            dead += 1
            continue

        if not wants_type(subscription, event.type):
            # Type filter narrowed after queueing. Not a failure and not a
            # delivery -- skipped is exactly the state for that.
            _terminate(
                row, m.DeliveryStatus.skipped,
                "subscription no longer subscribes to %s" % event.type,
            )
            not_sent += 1
            continue

        attempt = (row.attempts or 0) + 1
        result, status = send_one(
            subscription, event, runtime=runtime, attempt=attempt
        )
        row.response_status = status
        _apply_result(
            row, result, now, max_attempts=max_attempts, base_seconds=base_seconds
        )
        _stamp(subscription, result, now)
        if row.status == m.DeliveryStatus.delivered:
            sent += 1
        elif row.status == m.DeliveryStatus.dead:
            dead += 1
        elif row.status == m.DeliveryStatus.skipped:
            not_sent += 1
        else:
            failed += 1

    pruned = prune(db, runtime, now)
    db.commit()
    return {
        "events_delivered": sent,
        "events_failed": failed,
        "events_dead": dead,
        # Subscription disabled -- row untouched, will be reconsidered later.
        "events_skipped": skipped,
        # Ran and terminated without transmitting. Counted apart from delivered
        # so a green cycle cannot hide a silent one.
        "events_not_sent": not_sent,
        "events_pruned": pruned,
    }


def prune(db: Session, runtime: dict, now: Optional[datetime] = None) -> int:
    """Drop events whose deliveries have all reached a terminal state and aged out.

    ``outbound_events`` is append-only and one alert-heavy fleet writes thousands
    of rows a week, so leaving it unbounded would be the same defect as the
    readings table has (see the retention work), just smaller and slower to
    notice.

    Only events with **no** non-terminal delivery are eligible: a dead subscriber
    whose backlog is still queued keeps its events, which is the whole point of
    the backlog. Returns how many events were removed.

    The child rows are deleted **explicitly**, before their parents, rather than
    left to ``ON DELETE CASCADE``. SQLite does not enforce foreign keys unless
    ``PRAGMA foreign_keys=ON`` is set per connection -- which this application
    does not do -- so a cascade that works on Postgres silently orphans every
    delivery row on the dev and test backend. Caught by a test asserting the
    child rows were gone, which is the only way that difference shows up.
    """
    now = now or _now()
    try:
        days = int(runtime.get("events.retention_days", 30) or 30)
    except (TypeError, ValueError):
        days = 30
    if days <= 0:
        return 0
    cutoff = now - timedelta(days=days)

    live = select(m.EventDelivery.event_id).where(
        m.EventDelivery.status.in_(
            (m.DeliveryStatus.pending, m.DeliveryStatus.failed, m.DeliveryStatus.deferred)
        )
    )
    stale = [
        e
        for e in db.scalars(
            select(m.OutboundEvent).where(m.OutboundEvent.id.not_in(live))
        )
        if (_aware(e.created_at) or now) < cutoff
    ]
    if not stale:
        return 0
    ids = [e.id for e in stale]
    db.execute(delete(m.EventDelivery).where(m.EventDelivery.event_id.in_(ids)))
    db.execute(delete(m.OutboundEvent).where(m.OutboundEvent.id.in_(ids)))
    return len(stale)
