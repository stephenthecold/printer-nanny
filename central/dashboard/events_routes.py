"""Dashboard: outbound event subscriptions at ``/manage/events``.

**Admin-only**, not manager-only. A subscription is a credential-bearing
outbound integration: creating one mints a signing secret and points this server
at a network address it will then make requests to. That is the same class of
decision as adding an operator account or restoring a database, both of which
already sit behind ``_admin``.

Three rules this module holds, none of which is decoration:

* **The signing secret is generated here and shown once.** An operator never
  types it and can never read it back -- the column is Fernet-encrypted and the
  form has no field for it. Losing it means rotating it, which is a button. That
  is the same discipline as agent API keys, and for the same reason: a secret
  that can be read back is a secret that ends up in a ticket.
* **The URL is validated before it is stored** (``events.destinations``), so an
  operator learns immediately that a link-local address is refused rather than
  discovering it in a dead-lettered delivery an hour later. It is validated again
  at send time, because DNS moves.
* **Every mutation is audited, and never with the secret in the detail.** The
  audit row names the subscription and its scope; the one thing it must not
  carry is the thing that authenticates the payloads.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from central import models as m
from central.audit import record
from central.dashboard import _keystore
from central.dashboard.manage import (
    _admin,
    _flash,
    _pop_flash,
    _redirect,
    _tpl,
)
from central.db import get_db
from central.events import signing
from central.events.catalogue import CATALOGUE, EVENT_TYPE_NAMES
from central.events.delivery import send_one
from central.events.destinations import DestinationError, validate_url
from central.events.emit import assert_scope, new_uid
from central.runtime import load_settings
from central.secrets import encrypt_value
from central.services import TenancyError

router = APIRouter(prefix="/manage", tags=["manage"])

#: Recent events shown on the page. Bounded because this table is append-only
#: and a busy fleet writes thousands of rows a week.
RECENT_EVENTS = 25


def _scope_label(sub: m.EventSubscription, clients: List[m.Client]) -> str:
    if sub.client_id is None:
        return "all clients"
    for c in clients:
        if c.id == sub.client_id:
            return c.name
    return "client %d" % sub.client_id


class _NoTypes(ValueError):
    """The form selected no valid event type."""


def _selected_types(raw: List[str]) -> Optional[list]:
    """Which catalogue types the form asked for; None means 'everything'.

    Filtered against the catalogue rather than trusted, so a hand-posted type
    cannot park an unknown string in the row and quietly filter out every real
    event.

    Two deliberate readings of the form:

    * **All ticked stores None**, not the expanded list. A subscription that
      plainly meant "everything" must receive a type added in a later release;
      freezing today's list would silently exclude it, and nobody would find out
      until they asked why an event never arrived.
    * **Nothing ticked is refused**, not read as "everything". It is genuinely
      ambiguous -- it looks equally like "I want it all" and like "I forgot" --
      and both possible defaults are wrong in one of those cases. Guessing would
      either create a subscription that receives nothing and does nothing, or
      quietly widen a scope the operator was trying to narrow.
    """
    wanted = [t for t in (raw or []) if t in CATALOGUE]
    if not wanted:
        raise _NoTypes("select at least one event type")
    if len(wanted) == len(EVENT_TYPE_NAMES):
        return None
    return sorted(wanted)


def _client_id(raw: str) -> Optional[int]:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@router.get("/events", response_class=HTMLResponse)
def events_home(request: Request, db: Session = Depends(get_db)):
    user = _admin(request, db)
    if user is None:
        return _redirect("/login")

    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    subs = list(db.scalars(
        select(m.EventSubscription).order_by(m.EventSubscription.name, m.EventSubscription.id)
    ))
    events = list(db.scalars(
        select(m.OutboundEvent)
        .order_by(m.OutboundEvent.id.desc())
        .limit(RECENT_EVENTS)
    ))

    # Per-subscription delivery tallies, one grouped query rather than N.
    counts: dict = {}
    for sub_id, status, n in db.execute(
        select(
            m.EventDelivery.subscription_id, m.EventDelivery.status, func.count()
        ).group_by(m.EventDelivery.subscription_id, m.EventDelivery.status)
    ).all():
        counts.setdefault(sub_id, {})[getattr(status, "value", status)] = n

    event_clients = {c.id: c.name for c in clients}
    delivery_rows: dict = {}
    for row in db.scalars(
        select(m.EventDelivery).where(
            m.EventDelivery.event_id.in_([e.id for e in events] or [-1])
        )
    ):
        delivery_rows.setdefault(row.event_id, []).append(row)

    return _tpl(
        request, "events.html", db,
        user=user,
        clients=clients,
        subscriptions=subs,
        scope_label={s.id: _scope_label(s, clients) for s in subs},
        counts=counts,
        events=events,
        event_clients=event_clients,
        deliveries=delivery_rows,
        catalogue=[CATALOGUE[n] for n in EVENT_TYPE_NAMES],
        settings=load_settings(db),
        new_secret=_keystore.pop(request.session.pop("new_event_secret_token", None)),
        flash=_pop_flash(request),
        error=request.session.pop("events_error", None),
    )


@router.post("/events")
def create_subscription(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    client_id: str = Form(""),
    event_types: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    actor = _admin(request, db)
    if actor is None:
        return _redirect("/login")

    label = (name or "").strip()
    if not label:
        request.session["events_error"] = "A name is required."
        return _redirect("/manage/events")

    runtime = load_settings(db)
    try:
        clean_url = validate_url(
            url, allow_private=bool(runtime.get("events.allow_private_destinations", False))
        )
    except DestinationError as exc:
        request.session["events_error"] = str(exc)
        return _redirect("/manage/events")

    scope = _client_id(client_id)
    if scope is not None and db.get(m.Client, scope) is None:
        request.session["events_error"] = "That client no longer exists."
        return _redirect("/manage/events")

    try:
        types = _selected_types(event_types)
    except _NoTypes as exc:
        request.session["events_error"] = str(exc).capitalize() + "."
        return _redirect("/manage/events")

    # Generated, never typed. Shown once through the server-side one-shot store
    # (NOT the session cookie -- Starlette signs it but does not encrypt it, so
    # the browser could read the signing key straight out of it).
    secret = signing.generate_secret()
    sub = m.EventSubscription(
        name=label[:120],
        client_id=scope,
        url=clean_url[:500],
        secret=encrypt_value(secret),
        event_types=types,
        enabled=True,
    )
    db.add(sub)
    db.flush()
    record(
        db, request, actor, "event_subscription.create",
        target="event_subscription:%d" % sub.id,
        # Name, scope and destination -- deliberately never the secret.
        detail="%s -> %s (scope: %s)" % (
            sub.name, sub.url, "all clients" if scope is None else "client:%d" % scope
        ),
    )
    db.commit()
    request.session["new_event_secret_token"] = _keystore.put(
        {"name": sub.name, "secret": secret, "id": sub.id}
    )
    _flash(request, "Subscription '%s' created." % sub.name)
    return _redirect("/manage/events")


@router.post("/events/{sub_id}/update")
def update_subscription(
    sub_id: int,
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    client_id: str = Form(""),
    event_types: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Edit a subscription's name, destination, scope and type filter.

    Re-scoping is allowed and is exactly why the tenancy rule is re-checked at
    send time: deliveries already queued for the old scope must not go out under
    the new one. Those are dead-lettered by the sweeper with a stated reason.
    """
    actor = _admin(request, db)
    if actor is None:
        return _redirect("/login")
    sub = db.get(m.EventSubscription, sub_id)
    if sub is None:
        return _redirect("/manage/events")

    runtime = load_settings(db)
    try:
        clean_url = validate_url(
            url, allow_private=bool(runtime.get("events.allow_private_destinations", False))
        )
    except DestinationError as exc:
        request.session["events_error"] = str(exc)
        return _redirect("/manage/events")

    scope = _client_id(client_id)
    if scope is not None and db.get(m.Client, scope) is None:
        request.session["events_error"] = "That client no longer exists."
        return _redirect("/manage/events")

    try:
        types = _selected_types(event_types)
    except _NoTypes as exc:
        request.session["events_error"] = str(exc).capitalize() + "."
        return _redirect("/manage/events")

    before = (sub.name, sub.url, sub.client_id)
    sub.name = ((name or "").strip() or sub.name)[:120]
    sub.url = clean_url[:500]
    sub.client_id = scope
    sub.event_types = types
    record(
        db, request, actor, "event_subscription.update",
        target="event_subscription:%d" % sub.id,
        detail="was %s -> %s (scope: %s); now %s -> %s (scope: %s)" % (
            before[0], before[1], before[2], sub.name, sub.url, sub.client_id
        ),
    )
    db.commit()
    _flash(request, "Subscription '%s' updated." % sub.name)
    return _redirect("/manage/events")


@router.post("/events/{sub_id}/toggle")
def toggle_subscription(sub_id: int, request: Request, db: Session = Depends(get_db)):
    actor = _admin(request, db)
    if actor is None:
        return _redirect("/login")
    sub = db.get(m.EventSubscription, sub_id)
    if sub is not None:
        sub.enabled = not sub.enabled
        record(
            db, request, actor,
            "event_subscription.%s" % ("enable" if sub.enabled else "disable"),
            target="event_subscription:%d" % sub.id, detail=sub.name,
        )
        db.commit()
        _flash(request, "Subscription '%s' %s." % (
            sub.name, "enabled" if sub.enabled else "disabled"
        ))
    return _redirect("/manage/events")


@router.post("/events/{sub_id}/rotate")
def rotate_secret(sub_id: int, request: Request, db: Session = Depends(get_db)):
    """Mint a new signing secret, invalidating the old one immediately.

    There is no overlap window: the previous secret stops working the moment
    this returns, so a subscriber must be updated at the same time. An overlap
    would mean storing two live secrets, which doubles the thing we are trying
    to protect for the benefit of a coordination problem an operator can solve
    by disabling the subscription first.
    """
    actor = _admin(request, db)
    if actor is None:
        return _redirect("/login")
    sub = db.get(m.EventSubscription, sub_id)
    if sub is None:
        return _redirect("/manage/events")
    secret = signing.generate_secret()
    sub.secret = encrypt_value(secret)
    record(
        db, request, actor, "event_subscription.rotate_secret",
        target="event_subscription:%d" % sub.id, detail=sub.name,
    )
    db.commit()
    request.session["new_event_secret_token"] = _keystore.put(
        {"name": sub.name, "secret": secret, "id": sub.id}
    )
    _flash(
        request,
        "New signing secret for '%s'. The previous one stopped working now." % sub.name,
    )
    return _redirect("/manage/events")


@router.post("/events/{sub_id}/delete")
def delete_subscription(sub_id: int, request: Request, db: Session = Depends(get_db)):
    actor = _admin(request, db)
    if actor is None:
        return _redirect("/login")
    sub = db.get(m.EventSubscription, sub_id)
    if sub is not None:
        name = sub.name
        record(
            db, request, actor, "event_subscription.delete",
            target="event_subscription:%d" % sub.id, detail="%s -> %s" % (name, sub.url),
        )
        # Deleted explicitly, not left to ON DELETE CASCADE. SQLite does not
        # enforce foreign keys unless PRAGMA foreign_keys is set per connection,
        # which this application does not do -- so the cascade that works on
        # Postgres would orphan every queued delivery on the dev/test backend,
        # and the sweeper would then dead-letter rows for a destination nobody
        # can see any more.
        db.execute(
            delete(m.EventDelivery).where(m.EventDelivery.subscription_id == sub.id)
        )
        db.delete(sub)
        db.commit()
        _flash(request, "Subscription '%s' deleted." % name)
    return _redirect("/manage/events")


@router.post("/events/{sub_id}/test")
def send_test(sub_id: int, request: Request, db: Session = Depends(get_db)):
    """Sign and POST one synthetic event, synchronously, and report the outcome.

    Synthetic on purpose: it is built in memory and never written to
    ``outbound_events``, so a test does not enter the retry machinery, does not
    consume an idempotency key and cannot be confused with a real fact by a
    subscriber -- its type is ``alert.opened`` with a ``data.test`` marker and an
    id that is plainly a test. What it proves is the part an operator actually
    needs proved: that the URL is reachable from this host, that the destination
    accepts our headers, and that it answers 2xx.
    """
    actor = _admin(request, db)
    if actor is None:
        return _redirect("/login")
    sub = db.get(m.EventSubscription, sub_id)
    if sub is None:
        return _redirect("/manage/events")

    probe = m.OutboundEvent(
        uid=new_uid(),
        type="alert.opened",
        version=CATALOGUE["alert.opened"].version,
        idempotency_key="test:%s" % new_uid(),
        client_id=sub.client_id,
        data={
            "test": True,
            "message": "Test delivery from Printer Nanny. No fleet condition "
                       "corresponds to this event; do not act on it.",
        },
    )
    # A test must obey the same scope rule as a real delivery. It cannot fail
    # here (the event is minted with the subscription's own client_id), which is
    # the point: the assertion is what keeps that true if this is ever changed to
    # replay a stored event.
    try:
        assert_scope(sub, probe.client_id)
    except TenancyError as exc:
        record(
            db, request, actor, "event_delivery.refused",
            target="event_subscription:%d" % sub.id, detail=str(exc),
        )
        db.commit()
        request.session["events_error"] = "Refused: %s" % exc
        return _redirect("/manage/events")

    result, status = send_one(sub, probe, runtime=load_settings(db))
    sub.last_attempt_at = m.utcnow()
    sub.last_ok = bool(result.ok and result.sent)
    sub.last_error = None if result.ok else (result.detail or "")[:500]
    record(
        db, request, actor, "event_subscription.test",
        target="event_subscription:%d" % sub.id,
        detail="%s: %s" % ("ok" if result.ok else "failed", result.detail),
    )
    db.commit()
    if result.ok:
        _flash(request, "Test delivered to '%s' (%s)." % (sub.name, result.detail))
    else:
        request.session["events_error"] = "Test to '%s' failed: %s" % (
            sub.name, result.detail
        )
    return _redirect("/manage/events")


__all__ = ["router"]
