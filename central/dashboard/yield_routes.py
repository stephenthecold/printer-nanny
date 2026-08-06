"""Cartridge yield: the operator surface for ``central.supply_yield``.

STAFF ONLY, and that is a product decision rather than an oversight -- the same
one ``/security/posture`` makes, for a closely related reason.

A "yield below expected" row is a measurement whose most interesting reading is
*your customer is buying non-OEM cartridges*. Publishing that to the customer
portal would hand a customer an accusation they cannot verify, about a supply
chain we cannot see, derived from a comparison they have no way to audit. Every
useful response to it -- check the purchase records, ask what was fitted, weigh a
page-coverage explanation -- is an MSP conversation, not a self-service one. So
``client_readonly`` users are sent to their portal, exactly as they are from the
reorder page and the posture report. Exposing a customer-facing version later is
a deliberate decision with wording written for that audience, not a flag flip.

SECURITY POSTURE OF THIS FILE
-----------------------------
* Both write routes are ``_manager`` (admin/tech) and audited. An expected yield
  is fleet knowledge rather than a tenant's data, so there is no client scope to
  check on it -- but it *changes what the report accuses a customer of*, so it is
  audited with its numbers.
* The read route is tenant-filtered in SQL through ``supply_yield.assessments``.
  ``?client_id=`` is validated against clients that actually exist rather than
  trusted, and an unknown id widens to the fleet rather than 404ing -- it is a
  filter, and the honest answer to a stale bookmark is everything.
* Nothing is interpolated anywhere: the ORM parameterises and Jinja escapes.
  ``model_tag`` in particular is operator free-text that is later matched against
  device-supplied ``printers.model`` -- as a plain lowercase substring test in
  Python, never as SQL and never as a pattern, so there is no ``LIKE`` wildcard
  and no regular expression to get wrong.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import supply_yield as _yield
from central.audit import record
from central.dashboard.manage import _deny, _flash, _manager, _pop_flash, _redirect, _tpl
from central.db import get_db

router = APIRouter(tags=["dashboard"])

#: Cartridges do not yield a million pages, and a page count that large in the
#: expectation table would make every real device look like a fraud. Bounded on
#: input so a typo is refused with a sentence rather than stored.
MAX_EXPECTED_PAGES = 1_000_000

#: What may appear in the supply-type picker. Sourced from the enum so a type
#: added to ``SupplyType`` shows up here without a second list to update.
SUPPLY_TYPES = tuple(t.value for t in m.SupplyType)


def _selected_client(db: Session, raw: str, clients) -> Optional[int]:
    """Validate ``?client_id=`` against clients that exist. Never trusted."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        candidate = int(raw)
    except ValueError:
        return None
    return candidate if any(c.id == candidate for c in clients) else None


@router.get("/supplies/yield", response_class=HTMLResponse)
def supplies_yield(request: Request, db: Session = Depends(get_db)):
    """Pages per cartridge, measured, against what a cartridge should deliver."""
    user = _manager(request, db)
    if user is None:
        from central.dashboard.routes import _user

        # A signed-in customer goes to their own portal; anybody else logs in.
        # Two outcomes rather than one 403, matching every other staff report.
        return _redirect("/portal" if _user(request, db) is not None else "/login")

    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    selected_id = _selected_client(db, request.query_params.get("client_id", ""), clients)

    thresholds = _yield.YieldThresholds.load(db)
    rows = _yield.assessments(db, client_id=selected_id, thresholds=thresholds)
    expectations = list(
        db.scalars(
            select(m.SupplyYieldExpectation).order_by(
                m.SupplyYieldExpectation.model_tag,
                m.SupplyYieldExpectation.supply_type,
                m.SupplyYieldExpectation.color,
            )
        )
    )
    return _tpl(
        request,
        "supply_yield.html",
        db,
        user=user,
        rows=rows,
        summary=_yield.summarize(rows),
        thresholds=thresholds,
        clients=clients,
        selected_client_id=selected_id,
        expectations=expectations,
        supply_types=SUPPLY_TYPES,
        replacements=_yield.recent_replacements(db, client_id=selected_id, limit=20),
        observed_yield=_yield.observed_yield,
        consumed_pct=_yield.consumed_pct,
        verdict_below=_yield.VERDICT_BELOW,
        verdict_within=_yield.VERDICT_WITHIN,
        verdict_insufficient=_yield.VERDICT_INSUFFICIENT,
        source_operator=_yield.SOURCE_OPERATOR,
        source_fleet=_yield.SOURCE_FLEET,
        flash=_pop_flash(request),
    )


@router.post("/supplies/yield/expectations")
def save_expectation(
    request: Request,
    model_tag: str = Form(""),
    supply_type: str = Form(""),
    color: str = Form(""),
    expected_pages: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    """Record the datasheet yield for one model + supply slot.

    Upserts on (model_tag, supply_type, color) rather than creating a duplicate:
    that triple is a unique constraint, and the operator's intent when they type
    a tag they have used before is plainly "correct the number", not "add a
    second, ambiguous one" -- which the matcher would then refuse to choose
    between, silently costing them the expectation they already had.
    """
    user = _manager(request, db)
    if user is None:
        return _deny(request, db)

    tag = (model_tag or "").strip()[:200]
    if len(tag) < _yield.MIN_MODEL_TAG:
        _flash(
            request,
            "A model tag needs at least %d characters -- a shorter one would "
            "match half the fleet." % _yield.MIN_MODEL_TAG,
            level="error",
        )
        return _redirect("/supplies/yield")
    if supply_type not in SUPPLY_TYPES:
        _flash(request, "Unknown supply type.", level="error")
        return _redirect("/supplies/yield")
    try:
        pages = int((expected_pages or "").strip())
    except (TypeError, ValueError):
        pages = 0
    if pages <= 0 or pages > MAX_EXPECTED_PAGES:
        _flash(
            request,
            "Expected pages must be between 1 and %s." % f"{MAX_EXPECTED_PAGES:,}",
            level="error",
        )
        return _redirect("/supplies/yield")

    slot_color = _yield.slot_color(color)
    row = db.scalar(
        select(m.SupplyYieldExpectation).where(
            m.SupplyYieldExpectation.model_tag == tag,
            m.SupplyYieldExpectation.supply_type == supply_type,
            m.SupplyYieldExpectation.color == slot_color,
        )
    )
    is_new = row is None
    if row is None:
        row = m.SupplyYieldExpectation(
            model_tag=tag, supply_type=supply_type, color=slot_color
        )
        db.add(row)
    row.expected_pages = pages
    row.note = (note or "").strip()[:300] or None
    row.updated_at = m.utcnow()
    db.flush()

    record(
        db, request, user,
        "supply_yield.expectation_create" if is_new else "supply_yield.expectation_update",
        "supply_yield_expectation:%s" % row.id,
        # The numbers are in the audit line because they are what decides
        # whether a customer gets accused of buying non-OEM cartridges.
        "model_tag=%r type=%s color=%r expected_pages=%d"
        % (tag, supply_type, slot_color, pages),
    )
    db.commit()
    _flash(
        request,
        "Expected yield %s for %r %s saved."
        % (f"{pages:,}", tag, _yield.slot_label(supply_type, slot_color)),
    )
    return _redirect("/supplies/yield")


@router.post("/supplies/yield/expectations/{expectation_id}/delete")
def delete_expectation(
    request: Request, expectation_id: int, db: Session = Depends(get_db)
):
    """Remove an expected yield.

    Deleting it does not delete any measurement: the cartridge cycles behind the
    report are untouched, and the affected slots fall back to a fleet baseline if
    one exists or to "no expectation" if not. That is the whole reason a verdict
    is computed on read rather than stored.
    """
    user = _manager(request, db)
    if user is None:
        return _deny(request, db)

    row = db.get(m.SupplyYieldExpectation, expectation_id)
    if row is None:
        _flash(request, "That expected yield no longer exists.", level="error")
        return _redirect("/supplies/yield")

    record(
        db, request, user,
        "supply_yield.expectation_delete",
        "supply_yield_expectation:%s" % row.id,
        "model_tag=%r type=%s color=%r expected_pages=%s"
        % (row.model_tag, row.supply_type, row.color, row.expected_pages),
    )
    db.delete(row)
    db.commit()
    _flash(request, "Expected yield removed.")
    return _redirect("/supplies/yield")
