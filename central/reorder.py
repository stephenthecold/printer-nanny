"""Supply reorder recommendations feeding a separate operator order ledger.

WHAT THIS IS
------------
"Order these N cartridges." A read-time judgement over measurements the system
already collects, surfaced in the dashboard, the customer portal and the weekly
report, and published as a ``supply.reorder_recommended`` event so an MSP's own
ERP can act on it.

WHAT STAYS SEPARATE
-------------------
This module never places an order and the recommendation is still computed on
read. ``SupplyOrder`` records the human action, destination and delivery state;
it does not mutate the measurement or make a recommendation disappear.

PERSIST vs COMPUTE
------------------
The split is: **persist the measurements, compute the judgement.**

``Supply.level_pct`` (from polling), ``Supply.days_to_empty`` and
``Supply.pages_to_empty`` (from the worker's forecast pass) are persisted,
because re-fitting a regression over 30 days of readings on every page render is
what the existing forecast job was built to avoid. The recommendation itself is
computed on read from those three numbers plus the operator's thresholds, and is
stored nowhere:

  * a threshold change takes effect on the next render, rather than at the next
    worker cycle -- an operator who lowers the reorder level and sees no change
    concludes the setting does nothing;
  * there is no recommendation row to go stale when a cartridge is swapped, and
    therefore no reconciliation pass to get wrong;
  * there is no recommendation row to reconcile with the separate order ledger.

TRIGGERS
--------
Level OR days-remaining OR pages-remaining -- any one fires. Level alone is not
enough: 20% of a cartridge is a fortnight on one device and an afternoon on
another. Calibration for the shipped defaults comes from the incumbents: Xerox
ASR orders at 2-3 weeks of usage remaining, Konica dispatches at 20% remaining,
Xerox XDA's alert window is configurable 0-20 days. All four thresholds are
operator-settable (``reorder.*`` in central.runtime).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session, contains_eager

from central import models as m

log = logging.getLogger("central.reorder")

# The event this module publishes. Named for the fact, not for an instruction:
# a consumer decides what to do about a recommendation, and calling it
# "supply.order_placed" would be a claim we are in no position to make.
EVENT_TYPE = "supply.reorder_recommended"

# Urgency bands. Two, because they map to two different decisions, and a third
# ("keep an eye on it") is not a decision anybody acts on.
URGENCY_NOW = "order_now"    # will run out at or before a replacement can arrive
URGENCY_SOON = "order_soon"  # inside the reorder window, with slack left
URGENCY_ORDER = (URGENCY_NOW, URGENCY_SOON)
_URGENCY_RANK = {URGENCY_NOW: 0, URGENCY_SOON: 1}


def _one_line(value: Optional[str], limit: int = 120) -> str:
    """Collapse a device- or operator-supplied string to a single clean line.

    Printer model, hostname and supply description are SNMP strings from a device
    on a customer LAN; display_name and location are operator free-text. Both
    reach the weekly report, which is plain text -- where an embedded newline
    forges a report line, and a carriage return can hide everything before it in
    a terminal. Jinja escapes these for HTML and JSON encodes them for the event
    payload, so this is the plain-text wire boundary's own escaping, applied here
    rather than trusted to the caller.
    """
    if not value:
        return ""
    cleaned = " ".join(str(value).split())
    return cleaned[:limit]


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReorderThresholds:
    """The operator's reorder policy, resolved from runtime settings.

    Every value is read defensively: a hand-edited or hostile ``app_settings``
    row must degrade to the shipped default rather than raise inside a page
    render or a worker cycle.
    """

    level_pct: float = 20.0        # Konica dispatches at 20% remaining
    days_remaining: int = 21       # Xerox ASR orders at 2-3 weeks of usage left
    pages_remaining: int = 500     # 0 disables the pages trigger entirely
    urgent_level_pct: float = 5.0
    # Receptacles (waste toner, chip boxes) run the OTHER way: their level is
    # how full they are, so they are ordered when they are nearly full, not
    # nearly empty. Two separate numbers rather than reusing the pair above,
    # because "20% remaining" and "80% full" are not the same policy and an
    # operator tuning one must not silently move the other.
    receptacle_full_pct: float = 80.0
    urgent_receptacle_full_pct: float = 95.0
    lead_days: int = 14            # alerts.reorder_lead_days -- the operator has
                                   # already told us how long a cartridge takes
                                   # to arrive; a supply that will not outlast it
                                   # is not "soon", it is late.

    @classmethod
    def from_runtime(cls, runtime: Optional[Dict[str, Any]]) -> "ReorderThresholds":
        rt = runtime or {}
        d = cls()

        def _num(key: str, fallback, cast):
            try:
                return cast(rt[key])
            except (KeyError, TypeError, ValueError):
                return fallback

        return cls(
            level_pct=_num("reorder.level_pct", d.level_pct, float),
            days_remaining=_num("reorder.days_remaining", d.days_remaining, int),
            pages_remaining=_num("reorder.pages_remaining", d.pages_remaining, int),
            urgent_level_pct=_num("reorder.urgent_level_pct", d.urgent_level_pct, float),
            receptacle_full_pct=_num(
                "reorder.receptacle_full_pct", d.receptacle_full_pct, float
            ),
            urgent_receptacle_full_pct=_num(
                "reorder.urgent_receptacle_full_pct", d.urgent_receptacle_full_pct, float
            ),
            lead_days=_num("alerts.reorder_lead_days", d.lead_days, int),
        )

    @classmethod
    def load(cls, db: Session) -> "ReorderThresholds":
        from central.runtime import load_settings  # lazy: avoid an import cycle

        return cls.from_runtime(load_settings(db))


# --------------------------------------------------------------------------- #
# The recommendation
# --------------------------------------------------------------------------- #
@dataclass
class SupplyRecommendation:
    """One supply on one printer that an operator should order.

    Carries its reasoning, not just its verdict: an operator who cannot see WHY
    a cartridge is on the list has to go and re-derive it from the printer page,
    and a recommendation nobody can check is one nobody trusts.
    """

    printer_id: int
    printer_label: str   # "Front Desk @ 10.0.0.7" -- operator-facing
    printer_name: str    # "Front Desk" -- customer-facing; the portal shows
                         # friendly names and no addressing, which is both its
                         # existing convention and one less internal detail on a
                         # page a customer's whole office can open.
    client_id: int
    client_name: str
    site_id: int
    site_name: str
    model: str
    supply_id: int
    supply_label: str
    supply_type: str
    color: Optional[str]
    level_pct: Optional[float]
    days_remaining: Optional[float]
    pages_remaining: Optional[float]
    urgency: str
    # Whether ``level_pct`` is fullness rather than remaining. Carried on the
    # recommendation, not looked up again by each renderer: the reorder page,
    # the portal, the weekly report and the outbound event all print this
    # number, and "5%" means opposite things in the two cases. A consumer that
    # cannot tell them apart cannot act on either.
    is_receptacle: bool = False
    triggers: List[str] = field(default_factory=list)   # machine-readable
    reasons: List[str] = field(default_factory=list)    # human-readable, with numbers

    @property
    def is_urgent(self) -> bool:
        return self.urgency == URGENCY_NOW

    @property
    def dedupe_key(self) -> str:
        """Stable identity for this recommendation.

        Per (printer, supply) -- NOT per printer. A colour device with three
        depleting toners is three separate things to order, and collapsing them
        into one row is how the third one gets forgotten.

        The urgency is deliberately NOT part of the key: a supply that escalates
        from ``order_soon`` to ``order_now`` is the same cartridge, and a
        consumer keying off this must see an update rather than a second item.
        """
        return f"{EVENT_TYPE}:printer:{self.printer_id}:supply:{self.supply_id}"

    @property
    def cartridge_key(self) -> Tuple[str, str, str]:
        """What counts as "the same cartridge" when aggregating an order.

        (model, type, colour) -- not (type, colour). Without a SKU catalogue this
        is the closest honest statement of sameness available: black toner for a
        Brother MFC is not black toner for an HP, and telling an MSP to "order 4
        black toners" across four models is advice that cannot be acted on.
        """
        return (self.model or "unknown model", self.supply_type, self.color or "")

    def as_event_payload(self) -> Dict[str, Any]:
        """The ``supply.reorder_recommended`` body.

        Carries no secrets (no SNMP community, no credentials, no API keys) and
        no raw device strings -- every free-text field goes through ``_one_line``
        on the way in, so a consumer rendering this into a ticket, a log line or
        a terminal is not handed embedded newlines or control characters.
        """
        return {
            "type": EVENT_TYPE,
            "dedupe_key": self.dedupe_key,
            "urgency": self.urgency,
            "triggers": list(self.triggers),
            "reasons": list(self.reasons),
            "client": {"id": self.client_id, "name": self.client_name},
            "site": {"name": self.site_name},
            "printer": {
                "id": self.printer_id,
                "label": self.printer_label,
                "model": self.model,
            },
            "supply": {
                "id": self.supply_id,
                "label": self.supply_label,
                "type": self.supply_type,
                "color": self.color,
                "level_pct": self.level_pct,
                # What ``level_pct`` means. Without it a consumer reading 92
                # cannot tell a nearly-full cartridge from a waste box that is
                # about to halt the printer -- and those call for opposite
                # actions.
                "is_receptacle": self.is_receptacle,
                "days_remaining": self.days_remaining,
                "pages_remaining": self.pages_remaining,
            },
        }


def _fmt_days(days: float) -> str:
    return f"{days:.0f}" if days >= 1 else f"{days:.1f}"


def _evaluate_receptacle(
    supply: m.Supply,
    thresholds: ReorderThresholds,
) -> Optional[Tuple[List[str], List[str], str]]:
    """Reorder verdict for a container whose level means "how full".

    One trigger, not three. Days- and pages-remaining are estimates of when a
    level reaches ZERO, which for a filling container is the state it starts in;
    the worker's fit refuses a rising series outright, so both are ``None`` here
    by construction rather than by omission.

    A threshold at or below 0 turns the trigger off, matching the consumed
    branch -- and 100 is left usable, because "flag it when it is completely
    full" is a defensible if late policy and clamping it would be us overruling
    an operator without saying so.
    """
    level = supply.level_pct
    if thresholds.receptacle_full_pct <= 0 or level is None:
        return None
    if level < thresholds.receptacle_full_pct:
        return None

    triggers = ["receptacle_full"]
    reasons = [
        f"container is {level:.0f}% full, at or above the "
        f"{thresholds.receptacle_full_pct:.0f}% replacement level"
    ]
    urgency = URGENCY_SOON
    if level >= thresholds.urgent_receptacle_full_pct:
        urgency = URGENCY_NOW
        reasons.append(
            f"{level:.0f}% full is at or above the "
            f"{thresholds.urgent_receptacle_full_pct:.0f}% order-now level"
        )
    return triggers, reasons, urgency


def evaluate_supply(
    supply: m.Supply,
    thresholds: ReorderThresholds,
) -> Optional[Tuple[List[str], List[str], str]]:
    """(triggers, reasons, urgency) if this supply should be ordered, else None.

    Split out from the query so the policy is unit-testable against a bare
    Supply row, with no database, no printer and no settings table.

    A RECEPTACLE IS A DIFFERENT QUESTION AND TAKES A DIFFERENT BRANCH. Its level
    is how full it is, so the three triggers below are all inverted for it:
    ``level <= 20`` means an almost-empty waste box, which was being recommended
    for reorder as "level 5% is at or below the 5% order-now level" about a part
    somebody had just fitted. The forecasts do not apply either -- they fit a
    DECLINING series, so a filling container yields ``None`` from both and, had
    it not been given its own branch, would simply never have been flagged when
    it mattered. See ``central.supplies``.
    """
    from central.supplies import is_receptacle

    if is_receptacle(supply):
        return _evaluate_receptacle(supply, thresholds)

    level = supply.level_pct
    days = supply.days_to_empty
    pages = supply.pages_to_empty

    triggers: List[str] = []
    reasons: List[str] = []

    # A threshold at or below 0 turns its trigger OFF rather than firing on
    # everything at exactly zero. That is the documented meaning of each setting,
    # and it is the only reading that lets an operator who trusts only the
    # forecasts switch the level trigger off without also switching off the
    # alert that fires when a cartridge actually reaches empty.
    if thresholds.level_pct > 0 and level is not None and level <= thresholds.level_pct:
        triggers.append("level")
        reasons.append(
            f"level {level:.0f}% is at or below the {thresholds.level_pct:.0f}% reorder level"
        )
    if thresholds.days_remaining > 0 and days is not None and days <= thresholds.days_remaining:
        triggers.append("days")
        reasons.append(
            f"~{_fmt_days(days)} day(s) left, at or below the "
            f"{thresholds.days_remaining}-day reorder window"
        )
    if thresholds.pages_remaining > 0 and pages is not None and pages <= thresholds.pages_remaining:
        triggers.append("pages")
        reasons.append(
            f"~{pages:,.0f} page(s) left, at or below the "
            f"{thresholds.pages_remaining:,}-page reorder window"
        )

    if not triggers:
        return None

    urgency = URGENCY_SOON
    if days is not None and days <= thresholds.lead_days:
        urgency = URGENCY_NOW
        reasons.append(
            f"will run out inside the {thresholds.lead_days}-day replacement lead time"
        )
    elif level is not None and level <= thresholds.urgent_level_pct:
        urgency = URGENCY_NOW
        reasons.append(
            f"level {level:.0f}% is at or below the {thresholds.urgent_level_pct:.0f}% "
            "order-now level"
        )
    return triggers, reasons, urgency


def _supply_label(supply: m.Supply) -> str:
    return _one_line(
        supply.description or supply.color or supply.type.value, limit=80
    ) or supply.type.value


def _printer_name(printer: m.Printer) -> str:
    return _one_line(
        printer.display_name or printer.model or printer.hostname, limit=80
    ) or "printer"


def _printer_label(printer: m.Printer) -> str:
    return f"{_printer_name(printer)} @ {_one_line(printer.ip, limit=64)}"


def recommendations(
    db: Session,
    *,
    client_id: Optional[int] = None,
    thresholds: Optional[ReorderThresholds] = None,
) -> List[SupplyRecommendation]:
    """Every supply an operator should order, most urgent first.

    ``client_id`` scopes the result to one tenant IN SQL. That is not an
    optimisation: a caller that fetches the fleet and narrows it in Python has
    already loaded every other customer's rows to render one customer's page,
    and the moment a cap is involved it shows a tenant nothing at all because
    the cap was spent elsewhere (see ``queries.open_alerts`` for the version of
    that bug which actually shipped).

    Only approved printers are considered -- a device sitting in the discovery
    queue is not yet part of anybody's fleet, and recommending consumables for
    it would be recommending them for a device nobody has agreed to manage.
    """
    thresholds = thresholds or ReorderThresholds.load(db)
    stmt = (
        select(m.Supply)
        .join(m.Supply.printer)
        .join(m.Printer.client)
        .join(m.Printer.site)
        .options(
            contains_eager(m.Supply.printer)
            .contains_eager(m.Printer.client),
            contains_eager(m.Supply.printer)
            .contains_eager(m.Printer.site),
        )
        .where(m.Printer.discovery_state == m.DiscoveryState.approved)
    )
    if client_id is not None:
        stmt = stmt.where(m.Printer.client_id == client_id)

    out: List[SupplyRecommendation] = []
    for supply in db.scalars(stmt):
        verdict = evaluate_supply(supply, thresholds)
        if verdict is None:
            continue
        triggers, reasons, urgency = verdict
        printer = supply.printer
        out.append(
            SupplyRecommendation(
                printer_id=printer.id,
                printer_label=_printer_label(printer),
                printer_name=_printer_name(printer),
                client_id=printer.client_id,
                client_name=_one_line(printer.client.name if printer.client else "", 80),
                site_id=printer.site_id,
                site_name=_one_line(printer.site.name if printer.site else "", 80),
                model=_one_line(printer.model, 80),
                supply_id=supply.id,
                supply_label=_supply_label(supply),
                supply_type=supply.type.value,
                color=_one_line(supply.color, 40) or None,
                level_pct=supply.level_pct,
                is_receptacle=supply.is_receptacle,
                days_remaining=supply.days_to_empty,
                pages_remaining=supply.pages_to_empty,
                urgency=urgency,
                triggers=triggers,
                reasons=reasons,
            )
        )
    return sort_recommendations(out)


def sort_recommendations(
    recs: Sequence[SupplyRecommendation],
) -> List[SupplyRecommendation]:
    """Most urgent first, then soonest to run out.

    ``None`` days sort last within a band rather than first: a supply with no
    forecast is on the list because its level or page count says so, and it must
    not displace one we can actually predict will fail on Thursday. The final
    tie-break is the supply id, so the order is total and a page does not
    reshuffle between two renders of identical data.
    """
    return sorted(
        recs,
        key=lambda r: (
            _URGENCY_RANK.get(r.urgency, 9),
            r.days_remaining if r.days_remaining is not None else float("inf"),
            r.level_pct if r.level_pct is not None else float("inf"),
            r.supply_id,
        ),
    )


# --------------------------------------------------------------------------- #
# Aggregation for the report: an MSP orders once, not once per device
# --------------------------------------------------------------------------- #
@dataclass
class CartridgeLine:
    """One order line: N of the same cartridge, and which printers need them."""

    model: str
    supply_type: str
    color: Optional[str]
    label: str
    quantity: int
    urgency: str
    printers: List[str] = field(default_factory=list)
    soonest_days: Optional[float] = None

    @property
    def is_urgent(self) -> bool:
        return self.urgency == URGENCY_NOW


@dataclass
class ClientReorderGroup:
    client_id: int
    client_name: str
    lines: List[CartridgeLine] = field(default_factory=list)
    recommendations: List[SupplyRecommendation] = field(default_factory=list)

    @property
    def total_units(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def urgent_units(self) -> int:
        return sum(line.quantity for line in self.lines if line.is_urgent)


def group_by_client(
    recs: Sequence[SupplyRecommendation],
) -> List[ClientReorderGroup]:
    """Roll recommendations up per client, then per cartridge within a client.

    Two levels, because they answer two different questions. Per client is who
    to raise the order with; per cartridge is what to put on it. A group's
    urgency is the MOST urgent member's -- ordering four cartridges together
    where one is needed this week means the whole line is needed this week.
    """
    groups: Dict[int, ClientReorderGroup] = {}
    lines: Dict[Tuple[int, Tuple[str, str, str]], CartridgeLine] = {}

    for rec in sort_recommendations(recs):
        group = groups.get(rec.client_id)
        if group is None:
            group = ClientReorderGroup(client_id=rec.client_id, client_name=rec.client_name)
            groups[rec.client_id] = group
        group.recommendations.append(rec)

        key = (rec.client_id, rec.cartridge_key)
        line = lines.get(key)
        if line is None:
            line = CartridgeLine(
                model=rec.model or "unknown model",
                supply_type=rec.supply_type,
                color=rec.color,
                label=rec.supply_label,
                quantity=0,
                urgency=rec.urgency,
            )
            lines[key] = line
            group.lines.append(line)
        line.quantity += 1
        if rec.printer_label not in line.printers:
            line.printers.append(rec.printer_label)
        if rec.is_urgent:
            line.urgency = URGENCY_NOW
        if rec.days_remaining is not None and (
            line.soonest_days is None or rec.days_remaining < line.soonest_days
        ):
            line.soonest_days = rec.days_remaining

    ordered = sorted(
        groups.values(), key=lambda g: (-g.urgent_units, -g.total_units, g.client_name)
    )
    for group in ordered:
        group.lines.sort(
            key=lambda line: (
                _URGENCY_RANK.get(line.urgency, 9),
                line.soonest_days if line.soonest_days is not None else float("inf"),
                line.model,
                line.label,
            )
        )
    return ordered


def summarize(recs: Sequence[SupplyRecommendation]) -> Dict[str, int]:
    """Counts for a nav badge / summary strip. Never raises on an empty list."""
    return {
        "total": len(recs),
        "order_now": sum(1 for r in recs if r.urgency == URGENCY_NOW),
        "order_soon": sum(1 for r in recs if r.urgency == URGENCY_SOON),
        "clients": len({r.client_id for r in recs}),
        "printers": len({r.printer_id for r in recs}),
    }


# --------------------------------------------------------------------------- #
# Event-bus seam (F2)
# --------------------------------------------------------------------------- #
# The typed outbound event bus is built separately. This module must not hard-
# depend on it: with the bus absent the recommendation surface still has to
# work, and with the bus present nothing here should need editing.
#
# So the publisher is resolved at CALL time, by name, through one function --
# which is also the injection point tests use. The contract asked of the bus is
# one call:  publish(db, event_type: str, payload: dict) -> Any
#
# INTEGRATION NOTE (F2): if the bus exposes a different name or signature,
# ``_resolve_publisher`` is the single line to change. Nothing else in this
# module, the dashboard, the reports or the worker touches it.
PUBLISHER_MODULE = "central.events"
PUBLISHER_ATTR = "publish"

Publisher = Callable[[Session, str, Dict[str, Any]], Any]


def _resolve_publisher() -> Optional[Publisher]:
    """The event bus's publish callable, or None if the bus is not installed."""
    try:
        module = __import__(PUBLISHER_MODULE, fromlist=[PUBLISHER_ATTR])
    except ImportError:
        return None
    publisher = getattr(module, PUBLISHER_ATTR, None)
    return publisher if callable(publisher) else None


@dataclass
class PublishResult:
    """What a publish attempt actually did.

    ``published`` and ``ok`` are separate for the same reason
    ``ChannelResult.sent`` is separate from ``ChannelResult.ok``: "did anything
    leave the building" is a different question from "did anything go wrong". A
    run with the bus absent, or with publishing switched off, is a legitimate
    outcome that transmitted NOTHING -- and it must never be reported as a
    successful send. Anything added here that returns early without transmitting
    must leave ``published`` at 0 and say why in ``reason``.
    """

    published: int = 0
    failed: int = 0
    skipped: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "published": self.published,
            "failed": self.failed,
            "skipped": self.skipped,
        }
        if self.reason:
            out["reason"] = self.reason
        return out


def publish_recommendations(
    db: Session,
    recs: Sequence[SupplyRecommendation],
    *,
    publisher: Optional[Publisher] = None,
) -> PublishResult:
    """Publish one ``supply.reorder_recommended`` event per recommendation.

    One event per (printer, supply), each carrying a stable ``dedupe_key``, so a
    consumer that sees the same recommendation on two runs can recognise it as
    the same cartridge rather than a second one to order. Delivery semantics --
    retry, ordering, at-least-once dedupe -- belong to the bus, exactly as
    channel delivery semantics belong to ``channels.delivery`` rather than to
    each caller.

    A publisher that raises for one recommendation is counted and the loop
    continues: one malformed row must not cost the rest of the fleet its events.
    """
    publisher = publisher or _resolve_publisher()
    if publisher is None:
        return PublishResult(
            skipped=len(recs),
            reason=f"event bus not installed ({PUBLISHER_MODULE}.{PUBLISHER_ATTR})",
        )
    result = PublishResult()
    for rec in recs:
        try:
            publisher(db, EVENT_TYPE, rec.as_event_payload())
        except Exception:  # noqa: BLE001 -- one bad event must not stop the rest
            result.failed += 1
            # No payload in the log line: it names printers and sites, and this
            # log is read by whoever can read the container's stdout.
            log.exception("publishing %s for %s failed", EVENT_TYPE, rec.dedupe_key)
        else:
            result.published += 1
    return result
