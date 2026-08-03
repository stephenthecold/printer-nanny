"""Emitting an event, and the tenancy invariant that governs who receives it.

``emit`` writes one ``OutboundEvent`` and fans it out to every subscription that
is (a) enabled, (b) in scope for the event's tenant, and (c) interested in the
type. It does **not** commit: like ``central.audit.record`` it appends to the
caller's transaction, so an alert that is rolled back never leaves a phantom
event claiming it happened.

THE TENANCY INVARIANT
---------------------
This is the same rule ``services.assign_printer`` owns for printer assignments,
in the same shape and with the same exception type:

    a client-scoped subscription receives ONLY that client's events

It spans tables (``event_subscriptions.client_id`` against
``outbound_events.client_id``) so no CHECK can state it, which is precisely why
it lives in one function every path goes through rather than being restated per
route. A cross-tenant attempt raises ``services.TenancyError`` -- deliberately
the same class, because "an operator picked the wrong row in a form" and "a
subscriber was handed another customer's fleet" are the same kind of event here
as they are there, and both are worth auditing rather than rendering as routine
validation.

Two rules that are easy to get subtly wrong, so they are stated:

* **A global subscription (client_id NULL) receives everything**, including
  every tenant's events. That is what "global" means and it is the MSP's own
  integration; it is not a leak.
* **A client-scoped subscription receives nothing whose tenancy is NULL.** An
  event with no client (a fleet-wide or system-level fact) is not "yours" just
  because you are the only customer. Absence of tenancy is not a match.

The check runs at fan-out AND again immediately before each send. That is not
belt-and-braces: a subscription's ``client_id`` is editable, so an operator
re-scoping a global subscription to one customer while deliveries for other
customers are still queued would otherwise ship exactly the thing this rule
forbids. See ``events.delivery``.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central.events.catalogue import _text, require
from central.services import TenancyError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """UTC ISO-8601 with a ``Z``, or None.

    SQLite hands back naive datetimes; they are UTC by construction everywhere in
    this codebase, so they are stamped as such rather than silently rendered as
    a local time the consumer would misread by hours.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def new_uid() -> str:
    return "evt_" + secrets.token_hex(16)


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #
def scope_allows(subscription: m.EventSubscription, client_id: Optional[int]) -> bool:
    """May this subscription be told about an event belonging to ``client_id``?"""
    if subscription.client_id is None:
        return True
    return client_id is not None and subscription.client_id == client_id


def assert_scope(subscription: m.EventSubscription, client_id: Optional[int]) -> None:
    """``scope_allows``, as a refusal.

    Used by the paths where a human explicitly asked for a specific delivery (a
    replay, a targeted send), because there the right answer is to say no
    loudly. The sweeper uses the predicate instead and dead-letters the row --
    it is processing a queue, not answering a request.
    """
    if not scope_allows(subscription, client_id):
        raise TenancyError(
            "subscription %s is scoped to client %s; event belongs to client %s"
            % (subscription.id, subscription.client_id, client_id)
        )


def wants_type(subscription: m.EventSubscription, event_type: str) -> bool:
    """Type filter -- a preference, not a security boundary."""
    wanted = subscription.event_types or []
    return not wanted or event_type in wanted


def subscriptions_for(
    db: Session, event_type: str, client_id: Optional[int]
) -> List[m.EventSubscription]:
    """Every destination that should receive this event, in id order."""
    rows = db.scalars(
        select(m.EventSubscription)
        .where(m.EventSubscription.enabled.is_(True))
        .order_by(m.EventSubscription.id)
    )
    return [
        s for s in rows if scope_allows(s, client_id) and wants_type(s, event_type)
    ]


# --------------------------------------------------------------------------- #
# Emit
# --------------------------------------------------------------------------- #
def emit(
    db: Session,
    event_type: str,
    *,
    data: Dict[str, Any],
    client_id: Optional[int] = None,
    idempotency_key: str,
    occurred_at: Optional[datetime] = None,
) -> Optional[m.OutboundEvent]:
    """Record one event and queue it for every subscriber in scope.

    Returns the new event, or ``None`` when ``idempotency_key`` has already been
    emitted -- which is the normal case on a worker cycle that re-evaluates a
    condition that has not changed. Dedupe is on the key rather than on "is there
    an open alert", because the key is what the consumer sees and what they will
    use to reject the duplicate; keeping our definition of "same occurrence"
    identical to theirs is the point.

    Does not commit. The caller's ``db.commit()`` persists the event with the
    thing it describes, so a failed transaction emits nothing.

    An event is recorded **even with no subscribers**, deliberately. It costs one
    row at the alert rate (not the poll rate), it is bounded by
    ``events.retention_days``, and it is what makes the "recent events" panel
    show an operator what they would actually receive *before* they commit to a
    subscription -- an empty panel until you have already wired something up is
    the wrong way round.
    """
    spec = require(event_type)
    # Truncated BEFORE the lookup, not just before the insert. The column is 200
    # chars, so a longer key is stored short -- and looking up the full key while
    # storing the truncated one means a re-emit finds nothing and then trips the
    # unique constraint, taking the caller's whole transaction with it.
    key = (idempotency_key or "")[:200]
    existing = db.scalar(
        select(m.OutboundEvent).where(m.OutboundEvent.idempotency_key == key)
    )
    if existing is not None:
        return None

    event = m.OutboundEvent(
        uid=new_uid(),
        type=spec.name,
        version=spec.version,
        idempotency_key=key,
        client_id=client_id,
        data=data,
        occurred_at=occurred_at or _now(),
    )
    db.add(event)
    db.flush()  # assign event.id for the delivery rows below

    for subscription in subscriptions_for(db, spec.name, client_id):
        db.add(m.EventDelivery(
            event_id=event.id,
            subscription_id=subscription.id,
            status=m.DeliveryStatus.pending,
            attempts=0,
            next_attempt_at=None,  # NULL == due on the very next sweep
        ))
    return event


# --------------------------------------------------------------------------- #
# Payload builders -- the one place ORM rows become event `data`.
#
# Kept together so "what does a consumer actually see" is answerable by reading
# one screen, and so every device-controlled string goes through _text exactly
# once. A builder never reaches for a value it has not been handed a row for: a
# missing site is reported as null, not omitted, because a consumer diffing our
# payloads should not have to distinguish "absent" from "unknown".
# --------------------------------------------------------------------------- #
def _printer_block(printer: Optional[m.Printer]) -> Optional[dict]:
    if printer is None:
        return None
    return {
        "id": printer.id,
        "name": _text(printer.display_name or printer.model or printer.hostname),
        "ip": _text(printer.ip, 64),
        "hostname": _text(printer.hostname),
        "model": _text(printer.model),
        "brand": _text(printer.brand),
        "serial": _text(printer.serial, 120),
        "asset_tag": _text(printer.asset_tag, 120),
        "location": _text(printer.location),
        "status": printer.status.value if printer.status else None,
        "last_seen": _iso(printer.last_seen),
    }


def _site_block(site: Optional[m.Site]) -> Optional[dict]:
    if site is None:
        return None
    return {"id": site.id, "name": _text(site.name)}


def _client_block(client: Optional[m.Client]) -> Optional[dict]:
    if client is None:
        return None
    return {"id": client.id, "name": _text(client.name)}


def _context(db: Session, printer: Optional[m.Printer]) -> dict:
    """The client/site/printer triple every event carries, resolved once."""
    client = site = None
    if printer is not None:
        client = db.get(m.Client, printer.client_id) if printer.client_id else None
        site = db.get(m.Site, printer.site_id) if printer.site_id else None
    return {
        "client": _client_block(client),
        "site": _site_block(site),
        "printer": _printer_block(printer),
    }


def _alert_block(alert: m.Alert) -> dict:
    return {
        "id": alert.id,
        "type": alert.type.value if alert.type else None,
        "severity": alert.severity.value if alert.severity else None,
        "state": alert.state.value if alert.state else None,
        # Title and detail are built from device strings (model, front-panel
        # message) upstream, so they are bounded here like any other.
        "title": _text(alert.title),
        "detail": _text(alert.detail),
        "dedupe_key": _text(alert.dedupe_key, 200),
        "flap_count": alert.flap_count or 0,
        "opened_at": _iso(alert.created_at),
        "resolved_at": _iso(alert.resolved_at),
    }


def alert_key(kind: str, alert: m.Alert) -> str:
    """Idempotency key for an alert transition, scoped by flap generation.

    The flap count is in the key, and that is not decoration. Flap damping
    re-opens the **same alert row** rather than raising a new one, so a
    condition that clears and re-fires inside the cooldown produces:

        opened(alert 5) -> resolved(alert 5) -> re-opened(alert 5)

    Keyed on the alert id alone, that third transition would collide with the
    first and emit nothing -- leaving every subscriber holding "resolved" for a
    fault that is live, and unable to hear the *next* resolve either, because
    that key was spent too. A consumer asserting a state that is not true is the
    failure this codebase keeps paying for; here it would be caused by our own
    de-duplication.

    Note the deliberate divergence from notification damping: a flap re-open is
    **not** notified (the operator was told minutes ago and per-oscillation
    paging is the bug damping exists to fix) but it **is** emitted. The two
    audiences want different things -- a person wants to be told once, a system
    wants its state to match ours. Volume here equals the number of real state
    transitions, which is the honest number.
    """
    return "%s:alert:%s:flap:%s" % (kind, alert.id, alert.flap_count or 0)


def emit_alert_opened(
    db: Session, alert: m.Alert, printer: Optional[m.Printer] = None
) -> Optional[m.OutboundEvent]:
    """``alert.opened`` for a freshly-raised or re-opened condition.

    Emitted at open, deliberately independent of suppression windows. Quiet hours
    exist so a *person* is not woken; an integration is not a person, and a
    consumer that wants to hold overnight alerts can do so with information we
    would otherwise have withheld from it. A deferred notification and a
    delivered event are not in conflict -- they are two audiences.
    """
    if printer is None and alert.printer_id is not None:
        printer = db.get(m.Printer, alert.printer_id)
    ctx = _context(db, printer)
    data = dict(ctx)
    data["alert"] = _alert_block(alert)
    return emit(
        db,
        "alert.opened",
        data=data,
        client_id=printer.client_id if printer is not None else None,
        idempotency_key=alert_key("alert.opened", alert),
        occurred_at=alert.created_at,
    )


def emit_alert_resolved(
    db: Session,
    alert: m.Alert,
    printer: Optional[m.Printer] = None,
    *,
    resolved_at: Optional[datetime] = None,
) -> Optional[m.OutboundEvent]:
    """``alert.resolved``: the condition cleared, was serviced, or was closed.

    Keyed by ``alert_key`` so it pairs with the ``alert.opened`` of the same flap
    generation, and so a cycle that re-reads a row it already closed emits
    nothing further.
    """
    if printer is None and alert.printer_id is not None:
        printer = db.get(m.Printer, alert.printer_id)
    ctx = _context(db, printer)
    data = dict(ctx)
    block = _alert_block(alert)
    if block.get("resolved_at") is None:
        block["resolved_at"] = _iso(resolved_at or _now())
    data["alert"] = block
    return emit(
        db,
        "alert.resolved",
        data=data,
        client_id=printer.client_id if printer is not None else None,
        idempotency_key=alert_key("alert.resolved", alert),
        occurred_at=alert.resolved_at or resolved_at or _now(),
    )


def emit_printer_offline(
    db: Session, printer: m.Printer, *, at: Optional[datetime] = None
) -> Optional[m.OutboundEvent]:
    """``printer.offline``: readings dried up past the grace period.

    Keyed on the transition, not on the printer: ``printer.offline:printer:7:<the
    last_seen it went stale from>``. A device that goes offline, comes back and
    goes offline again is two occurrences and must produce two events, while the
    same transition re-observed on a later cycle must not.
    """
    ctx = _context(db, printer)
    data = dict(ctx)
    stamp = _iso(printer.last_seen) or "never"
    return emit(
        db,
        "printer.offline",
        data=data,
        client_id=printer.client_id,
        idempotency_key="printer.offline:printer:%s:%s" % (printer.id, stamp),
        occurred_at=at or _now(),
    )


def emit_supply_reorder_recommended(
    db: Session,
    printer: m.Printer,
    supply: m.Supply,
    *,
    days_to_empty: Optional[float],
    order_by: Optional[datetime] = None,
    reason: Optional[str] = None,
    period: Optional[str] = None,
) -> Optional[m.OutboundEvent]:
    """``supply.reorder_recommended`` -- a recommendation, never an order.

    Printer Nanny holds no SKU catalog, no stock and no order state by explicit
    product decision; this event is how that decision stays workable, by handing
    the recommendation to whatever system the MSP already buys through.

    ``period`` scopes the idempotency key to whatever window the caller considers
    one recommendation (a date, a forecast run id). Without it a daily job would
    re-recommend the same cartridge every cycle until somebody fitted it, and a
    consumer would have to invent the de-duplication we already own. It defaults
    to the UTC date, which is the behaviour a caller almost always wants.
    """
    ctx = _context(db, printer)
    data = dict(ctx)
    data["supply"] = {
        "id": supply.id,
        "type": supply.type.value if supply.type else None,
        "color": _text(supply.color, 40),
        "description": _text(supply.description),
        "level_pct": supply.level_pct,
        "days_to_empty": days_to_empty,
        "order_by": _iso(order_by),
        "reason": _text(reason),
    }
    window = period or _now().date().isoformat()
    return emit(
        db,
        "supply.reorder_recommended",
        data=data,
        client_id=printer.client_id,
        idempotency_key="supply.reorder_recommended:supply:%s:%s" % (supply.id, window),
    )
