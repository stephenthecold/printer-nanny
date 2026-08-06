"""Dashboard: cost-per-page rate cards and invoice preview.

**Admin-only, deliberately.** Everything else under ``/manage`` is open to
``tech`` as well, but this page holds a customer's commercial terms: what an MSP
charges per page, its volume breaks and its minimum commitment. A technician
approving printers has no reason to see the margin, and the nav entry sits with
the other admin-only destinations so the menu and the route agree — a link that
303s to the login page for half the operators who can see it is a worse
experience than no link.

Tenancy is enforced where it can actually be enforced: ``billing.build_invoice``
scopes the printer query to the client and refuses a rate card belonging to
another one. This module's own job is the narrower one of never *displaying* one
client's cards under another's selection, which is why every query filters on
the resolved client id rather than trusting an id that arrived in a form.

Money arrives here as operator free-text and leaves as ``Decimal``. Parsing goes
through ``central.money``, which refuses floats, junk, ``NaN`` and negatives at
the one point that matters -- an invalid rate is a flash message, never a stored
value that quietly prices a year of invoices at nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import billing
from central import models as m
from central.audit import record
from central.dashboard.manage import (
    _deny,
    _admin,
    _flash,
    _pop_flash,
    _redirect,
    _tpl,
)
from central.db import get_db
from central.money import format_money, format_rate, parse_amount, parse_rate

router = APIRouter(prefix="/manage", tags=["manage"])

#: ISO 4217 is three letters. Validated rather than trusted because the value is
#: rendered on every invoice and exported in every CSV.
_CURRENCY_LEN = 3


def _resolve_client(db: Session, raw: Optional[str]) -> Optional[m.Client]:
    """Resolve the selected client, falling back to the first one.

    A bad or non-numeric id resolves to the fallback rather than 500ing: this
    value arrives from a query string, and a stale bookmark is not an error
    worth a stack trace.
    """
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    if not clients:
        return None
    if raw:
        try:
            wanted = int(raw)
        except (TypeError, ValueError):
            return clients[0]
        for c in clients:
            if c.id == wanted:
                return c
    return clients[0]


def _card_for_client(
    db: Session, card_id: int, client_id: Optional[int]
) -> Optional[m.BillingRateCard]:
    """A card, but only if it belongs to the client currently in view.

    The id comes from a form. A manager may administer every client, so this is
    not a privilege check -- it is what stops a stale form or a hand-edited id
    from editing one customer's rates while the page says another's name.
    """
    card = db.get(m.BillingRateCard, card_id)
    if card is None or (client_id is not None and card.client_id != client_id):
        return None
    return card


def _parse_month(raw: str, now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """``YYYY-MM`` (what ``<input type=month>`` posts) -> a UTC half-open range.

    Anything unparseable falls back to the last complete month, which is also
    the default when nothing is chosen.
    """
    text = (raw or "").strip()
    if len(text) == 7 and text[4] == "-":
        try:
            year, month = int(text[:4]), int(text[5:])
            if 1 <= month <= 12 and 1970 <= year <= 9999:
                return billing.month_bounds(year, month)
        except ValueError:
            pass
    return billing.previous_month(now)


def _currency(raw: str) -> str:
    text = (raw or "").strip().upper()
    if len(text) != _CURRENCY_LEN or not text.isalpha():
        raise ValueError("currency: use a 3-letter ISO code, e.g. USD")
    return text


@router.get("/billing", response_class=HTMLResponse)
def billing_home(
    request: Request,
    client_id: str = "",
    month: str = "",
    db: Session = Depends(get_db),
):
    user = _admin(request, db)
    if user is None:
        return _deny(request, db)
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    client = _resolve_client(db, client_id)

    cards = []
    invoice = None
    invoice_error = None
    period_start, period_end = _parse_month(month)
    if client is not None:
        cards = list(
            db.scalars(
                select(m.BillingRateCard)
                .where(m.BillingRateCard.client_id == client.id)
                .order_by(
                    m.BillingRateCard.active.desc(),
                    m.BillingRateCard.name,
                )
            )
        )
        try:
            invoice = billing.build_invoice(db, client.id, period_start, period_end)
        except billing.BillingError as exc:
            # An expected operator-facing state ("no rate card yet"), not a fault.
            invoice_error = str(exc)

    return _tpl(
        request, "billing.html", db,
        user=user,
        clients=clients,
        client=client,
        cards=cards,
        invoice=invoice,
        invoice_error=invoice_error,
        month_value=period_start.strftime("%Y-%m"),
        period_start=period_start,
        period_end=period_end,
        meter_classes=[c.value for c in m.MeterClass],
        policies=[p.value for p in m.UnsplitPolicy],
        format_money=format_money,
        format_rate=format_rate,
        flash=_pop_flash(request),
    )


@router.post("/billing/rate-cards")
def rate_card_create(
    request: Request,
    client_id: str = Form(...),
    name: str = Form(...),
    currency: str = Form("USD"),
    mono_rate: str = Form(""),
    color_rate: str = Form(""),
    minimum_charge: str = Form(""),
    unsplit_policy: str = Form("exclude"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    actor = _admin(request, db)
    if actor is None:
        return _deny(request, db)
    client = _resolve_client(db, client_id)
    if client is None:
        _flash(request, "Create a client before adding a rate card.", level="error")
        return _redirect("/manage/billing")
    back = f"/manage/billing?client_id={client.id}"

    label = name.strip()
    if not label:
        _flash(request, "Rate card name is required.", level="error")
        return _redirect(back)
    try:
        card = m.BillingRateCard(
            client_id=client.id,
            name=label,
            currency=_currency(currency),
            mono_rate=parse_rate(mono_rate, field="mono rate"),
            color_rate=parse_rate(color_rate, field="colour rate"),
            minimum_charge=(
                parse_amount(minimum_charge, field="minimum commitment")
                if minimum_charge.strip() else None
            ),
            unsplit_policy=m.UnsplitPolicy(unsplit_policy)
            if unsplit_policy in {p.value for p in m.UnsplitPolicy}
            else m.UnsplitPolicy.exclude,
            notes=notes.strip() or None,
            # A new card starts inactive when the client already has an active
            # one: activating is a separate, deliberate click, because it
            # changes what every future invoice charges.
            active=billing.active_rate_card(db, client.id) is None,
        )
    except ValueError as exc:
        _flash(request, f"Rate card not created — {exc}", level="error")
        return _redirect(back)

    if db.scalar(
        select(m.BillingRateCard.id).where(
            m.BillingRateCard.client_id == client.id,
            m.BillingRateCard.name == label,
        )
    ):
        _flash(request, f"{client.name} already has a rate card called '{label}'.", level="error")
        return _redirect(back)

    db.add(card)
    db.flush()
    record(
        db, request, actor, "billing.rate_card.create",
        target=f"client:{client.name}/{label}",
        detail=(
            f"mono:{format_rate(card.mono_rate)} colour:{format_rate(card.color_rate)} "
            f"currency:{card.currency} minimum:{format_money(card.minimum_charge) or '-'} "
            f"unsplit:{card.unsplit_policy.value} active:{card.active}"
        ),
    )
    db.commit()
    _flash(request, f"Rate card '{label}' added.")
    return _redirect(back)


@router.post("/billing/rate-cards/{card_id}")
def rate_card_update(
    card_id: int,
    request: Request,
    client_id: str = Form(...),
    name: str = Form(""),
    currency: str = Form(""),
    mono_rate: str = Form(""),
    color_rate: str = Form(""),
    minimum_charge: str = Form(""),
    unsplit_policy: str = Form(""),
    db: Session = Depends(get_db),
):
    actor = _admin(request, db)
    if actor is None:
        return _deny(request, db)
    client = _resolve_client(db, client_id)
    back = f"/manage/billing?client_id={client.id}" if client else "/manage/billing"
    card = _card_for_client(db, card_id, client.id if client else None)
    if card is None:
        _flash(request, "That rate card does not belong to the selected client.", level="error")
        return _redirect(back)

    before = (
        f"mono:{format_rate(card.mono_rate)} colour:{format_rate(card.color_rate)} "
        f"minimum:{format_money(card.minimum_charge) or '-'} "
        f"unsplit:{card.unsplit_policy.value}"
    )
    try:
        if name.strip() and name.strip() != card.name:
            # Checked here rather than left to the UNIQUE constraint: an
            # IntegrityError at commit surfaces as a 500 on a form typo.
            if db.scalar(
                select(m.BillingRateCard.id).where(
                    m.BillingRateCard.client_id == card.client_id,
                    m.BillingRateCard.name == name.strip(),
                )
            ):
                raise ValueError(f"another rate card is already called '{name.strip()}'")
            card.name = name.strip()
        if currency.strip():
            card.currency = _currency(currency)
        if mono_rate.strip():
            card.mono_rate = parse_rate(mono_rate, field="mono rate")
        if color_rate.strip():
            card.color_rate = parse_rate(color_rate, field="colour rate")
        # Blank clears the minimum. The field is always posted by the form, so
        # blank unambiguously means "no minimum" rather than "not submitted".
        card.minimum_charge = (
            parse_amount(minimum_charge, field="minimum commitment")
            if minimum_charge.strip() else None
        )
        if unsplit_policy in {p.value for p in m.UnsplitPolicy}:
            card.unsplit_policy = m.UnsplitPolicy(unsplit_policy)
    except ValueError as exc:
        db.rollback()
        _flash(request, f"Rate card unchanged — {exc}", level="error")
        return _redirect(back)

    record(
        db, request, actor, "billing.rate_card.update",
        target=f"rate_card:{card.id}",
        detail=(
            f"was {before} -> now mono:{format_rate(card.mono_rate)} "
            f"colour:{format_rate(card.color_rate)} "
            f"minimum:{format_money(card.minimum_charge) or '-'} "
            f"unsplit:{card.unsplit_policy.value}"
        ),
    )
    db.commit()
    _flash(request, f"Rate card '{card.name}' updated.")
    return _redirect(back)


@router.post("/billing/rate-cards/{card_id}/activate")
def rate_card_activate(
    card_id: int,
    request: Request,
    client_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """Make this the card invoices are built from.

    Deactivating the incumbent has to happen and be flushed *before* this one is
    activated: the partial unique index allows exactly one active card per
    client, so the other order raises instead of switching.
    """
    actor = _admin(request, db)
    if actor is None:
        return _deny(request, db)
    client = _resolve_client(db, client_id)
    back = f"/manage/billing?client_id={client.id}" if client else "/manage/billing"
    card = _card_for_client(db, card_id, client.id if client else None)
    if card is None:
        _flash(request, "That rate card does not belong to the selected client.", level="error")
        return _redirect(back)

    current = billing.active_rate_card(db, card.client_id)
    if current is not None and current.id != card.id:
        current.active = False
        db.flush()
    card.active = True
    record(
        db, request, actor, "billing.rate_card.activate",
        target=f"rate_card:{card.id}",
        detail=f"client:{card.client_id} replaced:{current.id if current else '-'}",
    )
    db.commit()
    _flash(request, f"'{card.name}' is now the active rate card.")
    return _redirect(back)


@router.post("/billing/rate-cards/{card_id}/delete")
def rate_card_delete(
    card_id: int,
    request: Request,
    client_id: str = Form(...),
    db: Session = Depends(get_db),
):
    actor = _admin(request, db)
    if actor is None:
        return _deny(request, db)
    client = _resolve_client(db, client_id)
    back = f"/manage/billing?client_id={client.id}" if client else "/manage/billing"
    card = _card_for_client(db, card_id, client.id if client else None)
    if card is None:
        _flash(request, "That rate card does not belong to the selected client.", level="error")
        return _redirect(back)
    name = card.name
    record(
        db, request, actor, "billing.rate_card.delete",
        target=f"rate_card:{card.id}",
        detail=f"client:{card.client_id} name:{name} active:{card.active}",
    )
    db.delete(card)
    db.commit()
    _flash(request, f"Rate card '{name}' deleted.")
    return _redirect(back)


@router.post("/billing/rate-cards/{card_id}/tiers")
def tier_create(
    card_id: int,
    request: Request,
    client_id: str = Form(...),
    kind: str = Form(...),
    up_to: str = Form(""),
    rate: str = Form(""),
    db: Session = Depends(get_db),
):
    actor = _admin(request, db)
    if actor is None:
        return _deny(request, db)
    client = _resolve_client(db, client_id)
    back = f"/manage/billing?client_id={client.id}" if client else "/manage/billing"
    card = _card_for_client(db, card_id, client.id if client else None)
    if card is None:
        _flash(request, "That rate card does not belong to the selected client.", level="error")
        return _redirect(back)
    if kind not in {c.value for c in m.MeterClass}:
        _flash(request, "Volume band needs a meter class (mono or colour).", level="error")
        return _redirect(back)
    try:
        ceiling = int(up_to.strip())
    except (TypeError, ValueError):
        ceiling = 0
    if ceiling <= 0:
        _flash(request, "Volume band ceiling must be a positive number of pages.", level="error")
        return _redirect(back)
    try:
        band_rate = parse_rate(rate, field="band rate")
    except ValueError as exc:
        _flash(request, f"Volume band not added — {exc}", level="error")
        return _redirect(back)
    if any(t.kind.value == kind and t.up_to == ceiling for t in card.tiers):
        _flash(
            request,
            f"That card already has a {kind} band ending at {ceiling:,}.",
            level="error",
        )
        return _redirect(back)

    db.add(
        m.BillingRateTier(
            rate_card_id=card.id, kind=m.MeterClass(kind), up_to=ceiling, rate=band_rate
        )
    )
    record(
        db, request, actor, "billing.rate_tier.create",
        target=f"rate_card:{card.id}",
        detail=f"{kind} up_to:{ceiling} rate:{format_rate(band_rate)}",
    )
    db.commit()
    _flash(request, f"Added {kind} band up to {ceiling:,} pages.")
    return _redirect(back)


@router.post("/billing/tiers/{tier_id}/delete")
def tier_delete(
    tier_id: int,
    request: Request,
    client_id: str = Form(...),
    db: Session = Depends(get_db),
):
    actor = _admin(request, db)
    if actor is None:
        return _deny(request, db)
    client = _resolve_client(db, client_id)
    back = f"/manage/billing?client_id={client.id}" if client else "/manage/billing"
    tier = db.get(m.BillingRateTier, tier_id)
    if tier is None or (client is not None and tier.rate_card.client_id != client.id):
        _flash(request, "That volume band does not belong to the selected client.", level="error")
        return _redirect(back)
    detail = f"{tier.kind.value} up_to:{tier.up_to} rate:{format_rate(tier.rate)}"
    record(
        db, request, actor, "billing.rate_tier.delete",
        target=f"rate_card:{tier.rate_card_id}", detail=detail,
    )
    db.delete(tier)
    db.commit()
    _flash(request, "Volume band removed.")
    return _redirect(back)


@router.get("/billing/invoice.csv")
def invoice_download(
    request: Request,
    client_id: str = "",
    month: str = "",
    db: Session = Depends(get_db),
):
    """The rendered invoice as CSV.

    Audited: an invoice is a statement of what a customer owes, and who pulled
    one and for which period is exactly the sort of thing an MSP is asked to
    evidence later.
    """
    actor = _admin(request, db)
    if actor is None:
        return _deny(request, db)
    client = _resolve_client(db, client_id)
    if client is None:
        return _redirect("/manage/billing")
    period_start, period_end = _parse_month(month)
    try:
        invoice = billing.build_invoice(db, client.id, period_start, period_end)
    except billing.BillingError as exc:
        _flash(request, str(exc), level="error")
        return _redirect(f"/manage/billing?client_id={client.id}")

    body = billing.invoice_csv(invoice)
    record(
        db, request, actor, "billing.invoice.export",
        target=f"client:{client.name}",
        detail=(
            f"period:{invoice.period_label} total:{format_money(invoice.total)} "
            f"{invoice.currency} lines:{len(invoice.lines)} "
            f"unbilled_pages:{invoice.unbilled_pages}"
        ),
    )
    db.commit()
    stamp = period_start.strftime("%Y-%m")
    # The filename carries no customer-controlled text: the client name is
    # operator free-text and a quote or newline in it would break the header.
    filename = f"printer-nanny-invoice-{client.id}-{stamp}.csv"
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
