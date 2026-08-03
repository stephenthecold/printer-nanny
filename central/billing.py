"""Cost-per-page billing: rate cards in, invoice-shaped output out.

The engine is deliberately **pure and on-demand**. It reads meters and a rate
card and returns an ``Invoice`` value; it writes nothing. Two reasons, both
worth stating because "why is there no invoices table" is the first question
anyone asks:

* The meters are append-only and the rate card records the terms, so an invoice
  is *derivable*. A stored copy is a second source of truth that drifts the
  moment somebody corrects a rate, and then two people reading the same period
  get different numbers depending on which one they looked at.
* A persisted invoice is a financial document, and doing that honestly means
  immutability, a numbering series, void/credit notes and tax treatment. That is
  a much larger commitment than pricing pages, and half of it — an invoice you
  can silently edit — is worse than none. If it lands later, this module is what
  it would snapshot.

WHAT IS AND IS NOT BILLED
-------------------------
The governing rule, inherited from the billing CSV: **an unknown meter is never
billed as zero.** ``queries.period_meters`` returns ``None`` for a meter the
device did not report in the window, and every branch below treats that as "we
cannot price this", producing an explicit unbilled disclosure rather than a
quietly cheaper invoice. The four cases:

======================  ==========================================================
Device reported         Billed
======================  ==========================================================
mono and colour         both, at their rates. Any total-page surplus over
                        ``mono + colour`` is disclosed as unbilled — it is real
                        printing of an unknown class.
one of the two          that one only. The remainder (total minus it) is
                        disclosed as unbilled; it is by definition *not* the
                        class we do know, so no policy may claim it.
neither                 nothing, unless the card's ``unsplit_policy`` is
                        ``bill_as_mono`` — the operator's explicit statement that
                        this fleet's unsplit devices are mono devices.
nothing at all          nothing, and the printer is listed as having no readings.
======================  ==========================================================

TENANCY
-------
``build_invoice`` is the boundary. It scopes the printer query to the client and
refuses a rate card belonging to a different one with ``TenancyError`` — the
same type, for the same reason, as the printer-assignment path: a form typo and
a reach into another customer's fleet are different events and must not share an
error. Nothing above it may pass a card and a client that were not checked
together.

ROUNDING
--------
Half-up, at the invoice line, once — see ``central.money``. A line is one
printer's pages of one meter class; band subtotals inside it stay at full
precision, and the invoice total is the exact sum of already-rounded lines, so
the printed lines always add up to the printed total.
"""

from __future__ import annotations

import calendar
import io
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, NamedTuple, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import queries
from central.csv_safe import safe_writer
from central.money import format_money, format_rate, round_money
from central.services import TenancyError

__all__ = [
    "BillingError",
    "Invoice",
    "InvoiceLine",
    "UnbilledPrinter",
    "active_rate_card",
    "build_invoice",
    "invoice_csv",
    "month_bounds",
    "previous_month",
    "tiered_cost",
]

ZERO = Decimal("0")


class BillingError(ValueError):
    """Something the operator has to fix before an invoice can exist."""


# --------------------------------------------------------------------------- #
# Periods
# --------------------------------------------------------------------------- #
def month_bounds(year: int, month: int) -> Tuple[datetime, datetime]:
    """``[first instant of that month, first instant of the next)``, UTC.

    Half-open on purpose: adjacent periods then tile the timeline exactly once,
    so a reading at midnight on the 1st is billed in one month rather than in
    both or neither.
    """
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def previous_month(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """The last **complete** calendar month before ``now``.

    The default billing period. A partial month is never the default: an MSP
    bills work that has finished, and a report generated on the 1st covering the
    1st contains nothing at all.
    """
    now = now or datetime.now(timezone.utc)
    year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return month_bounds(year, month)


def clamp_day_of_month(day: int, year: int, month: int) -> int:
    """A day-of-month setting, clamped into the month that actually exists.

    ``31`` means "the last day" in five months of the year and ``29`` means it in
    February three years out of four. Testing the raw value against
    ``now.day`` loses those months entirely — five of twelve billing reports a
    year for a setting an operator chose deliberately.
    """
    last = calendar.monthrange(year, month)[1]
    return max(1, min(day, last))


# --------------------------------------------------------------------------- #
# Rate cards
# --------------------------------------------------------------------------- #
def active_rate_card(db: Session, client_id: int) -> Optional[m.BillingRateCard]:
    """The one active card for a client, or ``None``.

    At most one can exist (partial unique index), so this is a lookup rather
    than a choice.
    """
    return db.scalar(
        select(m.BillingRateCard).where(
            m.BillingRateCard.client_id == client_id,
            m.BillingRateCard.active.is_(True),
        )
    )


def tiered_cost(
    pages: int, tiers: Sequence[m.BillingRateTier], base_rate: Decimal
) -> Tuple[Decimal, str]:
    """Cost of ``pages`` under graduated volume bands, plus a human explanation.

    Bands are marginal: each covers the pages between the previous ceiling and
    its own. Anything above the highest band is priced at ``base_rate``, which is
    why no unbounded tier row exists to be forgotten.

    Returns the cost at **full precision** — the caller rounds, once, at the
    line. Rounding each band would make the same total depend on how the
    operator chose to slice the bands.
    """
    if pages <= 0:
        return ZERO, format_rate(base_rate) + "/page"
    cost = ZERO
    remaining = pages
    consumed = 0
    parts: List[str] = []
    for tier in sorted(tiers, key=lambda t: t.up_to):
        band = min(remaining, max(0, tier.up_to - consumed))
        consumed = max(consumed, tier.up_to)
        if band <= 0:
            continue
        cost += Decimal(band) * tier.rate
        parts.append(f"{band:,} @ {format_rate(tier.rate)}")
        remaining -= band
        if remaining == 0:
            break
    if remaining > 0:
        cost += Decimal(remaining) * base_rate
        parts.append(f"{remaining:,} @ {format_rate(base_rate)}")
    return cost, " + ".join(parts)


# --------------------------------------------------------------------------- #
# Invoice value objects
# --------------------------------------------------------------------------- #
class InvoiceLine(NamedTuple):
    """One printer's pages of one meter class, priced.

    The unit of rounding. ``amount`` is already quantized half-up to 2dp.
    """

    printer_id: int
    printer_label: str
    ip: str
    serial: str
    kind: str          # "mono" | "color"
    pages: int
    rate_detail: str   # "4,120 @ 0.008500", or the band breakdown
    amount: Decimal


class UnbilledPrinter(NamedTuple):
    """Pages we know were printed and deliberately did not price.

    ``pages`` may be 0 — a printer with no readings at all is disclosed too,
    because "this device produced no invoice line" has two very different causes
    and an operator needs to tell them apart.
    """

    printer_id: int
    printer_label: str
    ip: str
    pages: int
    reason: str


class Invoice(NamedTuple):
    client_id: int
    client_name: str
    currency: str
    period_start: datetime
    period_end: datetime
    rate_card_id: int
    rate_card_name: str
    lines: List[InvoiceLine]
    unbilled: List[UnbilledPrinter]
    metered_subtotal: Decimal
    minimum_charge: Optional[Decimal]
    minimum_adjustment: Decimal
    total: Decimal
    printer_count: int
    generated_at: datetime

    @property
    def billed_printer_count(self) -> int:
        return len({line.printer_id for line in self.lines})

    @property
    def unbilled_pages(self) -> int:
        return sum(u.pages for u in self.unbilled)

    @property
    def period_label(self) -> str:
        """``2026-07`` for a whole calendar month, else an explicit range."""
        start, end = self.period_start, self.period_end
        if (start.day, start.hour, start.minute, start.second) == (1, 0, 0, 0):
            if end == month_bounds(start.year, start.month)[1]:
                return start.strftime("%Y-%m")
        return f"{start.date().isoformat()} → {end.date().isoformat()}"


def _printer_label(printer: m.Printer) -> str:
    """Friendly name first, per the project-wide rule for naming a printer."""
    return (
        printer.display_name
        or printer.model
        or printer.hostname
        or printer.ip
        or f"printer:{printer.id}"
    )


def _classify(
    meters: queries.PeriodMeters, policy: m.UnsplitPolicy
) -> Tuple[Optional[int], Optional[int], int, Optional[str]]:
    """(billable mono, billable colour, unbilled pages, reason).

    ``None`` for a class means "not priced" — either the device never reported
    it, or it did and policy says we may not claim it. Never 0-as-a-guess.
    """
    total = meters.pages
    mono, color = meters.mono, meters.color

    if mono is not None and color is not None:
        surplus = max(0, (total or 0) - mono - color)
        reason = (
            "device total exceeds its mono + colour meters; the difference is of "
            "unknown class"
        ) if surplus else None
        return mono, color, surplus, reason

    if mono is not None:
        surplus = max(0, (total or 0) - mono)
        return mono, None, surplus, (
            "no colour meter reported; pages beyond the mono meter are of unknown class"
        ) if surplus else None

    if color is not None:
        surplus = max(0, (total or 0) - color)
        return None, color, surplus, (
            "no mono meter reported; pages beyond the colour meter are of unknown class"
        ) if surplus else None

    # Neither meter. Only an explicit policy may price these.
    if total is None:
        return None, None, 0, "no meter readings in this period"
    if total == 0:
        return None, None, 0, None
    if policy == m.UnsplitPolicy.bill_as_mono:
        return total, None, 0, None
    return None, None, total, (
        "device reports no mono/colour split; rate card is set to exclude unsplit pages"
    )


def build_invoice(
    db: Session,
    client_id: int,
    period_start: datetime,
    period_end: datetime,
    rate_card: Optional[m.BillingRateCard] = None,
) -> Invoice:
    """Price one client's metered work for ``[period_start, period_end)``.

    ``rate_card`` defaults to the client's active card. Passing one that belongs
    to a different client raises ``TenancyError`` — the caller has crossed a
    tenant boundary and that is not a validation message.
    """
    if period_end <= period_start:
        raise BillingError("billing period ends before it starts")

    client = db.get(m.Client, client_id)
    if client is None:
        raise BillingError(f"no such client: {client_id}")

    if rate_card is None:
        rate_card = active_rate_card(db, client_id)
        if rate_card is None:
            raise BillingError(
                f"{client.name} has no active rate card; create one before invoicing"
            )
    elif rate_card.client_id != client_id:
        raise TenancyError(
            f"rate card {rate_card.id} belongs to client {rate_card.client_id}, "
            f"not client {client_id}"
        )

    mono_tiers = [t for t in rate_card.tiers if t.kind == m.MeterClass.mono]
    color_tiers = [t for t in rate_card.tiers if t.kind == m.MeterClass.color]

    printers = list(
        db.scalars(
            select(m.Printer)
            .where(
                m.Printer.client_id == client_id,
                m.Printer.discovery_state == m.DiscoveryState.approved,
            )
            .order_by(m.Printer.site_id, m.Printer.ip)
        )
    )

    lines: List[InvoiceLine] = []
    unbilled: List[UnbilledPrinter] = []

    for printer in printers:
        meters = queries.period_meters(db, printer.id, period_start, period_end)
        mono, color, surplus, reason = _classify(meters, rate_card.unsplit_policy)
        label = _printer_label(printer)

        for kind, pages, tiers, base in (
            ("mono", mono, mono_tiers, rate_card.mono_rate),
            ("color", color, color_tiers, rate_card.color_rate),
        ):
            if not pages:  # None (unknown) and 0 (nothing printed) are both no line
                continue
            cost, detail = tiered_cost(pages, tiers, base)
            lines.append(
                InvoiceLine(
                    printer_id=printer.id,
                    printer_label=label,
                    ip=printer.ip or "",
                    serial=printer.serial or "",
                    kind=kind,
                    pages=pages,
                    rate_detail=detail,
                    # The single rounding point. See central.money.
                    amount=round_money(cost),
                )
            )

        if reason is not None:
            unbilled.append(
                UnbilledPrinter(
                    printer_id=printer.id,
                    printer_label=label,
                    ip=printer.ip or "",
                    pages=surplus,
                    reason=reason,
                )
            )

    metered_subtotal = sum((line.amount for line in lines), ZERO)
    minimum = rate_card.minimum_charge
    adjustment = ZERO
    if minimum is not None and metered_subtotal < minimum:
        adjustment = minimum - metered_subtotal

    return Invoice(
        client_id=client.id,
        client_name=client.name,
        currency=rate_card.currency or "USD",
        period_start=period_start,
        period_end=period_end,
        rate_card_id=rate_card.id,
        rate_card_name=rate_card.name,
        lines=lines,
        unbilled=unbilled,
        metered_subtotal=metered_subtotal,
        minimum_charge=minimum,
        minimum_adjustment=adjustment,
        total=metered_subtotal + adjustment,
        printer_count=len(printers),
        generated_at=datetime.now(timezone.utc),
    )


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
#: Every row declares its own ``kind``, so one file carries priced lines, the
#: unbilled disclosure, the minimum-commitment adjustment and the total without
#: a consumer having to guess from column emptiness which shape it is reading.
INVOICE_KINDS = ("mono", "color", "unbilled", "minimum_adjustment", "total")


def invoice_csv(invoice: Invoice) -> bytes:
    """The invoice as CSV, through ``safe_writer``.

    Printer labels, models and serials are SNMP strings off devices on a client
    LAN, and this file is opened by hand in Excel — so it goes through the
    formula-neutralising writer, header included, exactly like the billing CSV.
    Amounts and page counts are numeric and pass through untouched (a rate of
    ``-``-prefixed text cannot occur; the money type refuses negatives).
    """
    buf = io.StringIO()
    writer = safe_writer(buf)
    writer.writerow([
        "client", "period_start", "period_end", "currency", "kind",
        "printer", "ip", "serial", "pages", "rate_detail", "amount", "note",
    ])
    start = invoice.period_start.date().isoformat()
    end = invoice.period_end.date().isoformat()

    def row(kind, printer="", ip="", serial="", pages="", detail="", amount="", note=""):
        writer.writerow([
            invoice.client_name, start, end, invoice.currency, kind,
            printer, ip, serial, pages, detail, amount, note,
        ])

    for line in invoice.lines:
        row(line.kind, line.printer_label, line.ip, line.serial, line.pages,
            line.rate_detail, format_money(line.amount))
    for item in invoice.unbilled:
        # Amount is deliberately BLANK, not 0.00: we are declining to price
        # these, which is a different statement from pricing them at nothing.
        row("unbilled", item.printer_label, item.ip, "", item.pages, "", "", item.reason)
    if invoice.minimum_adjustment:
        row("minimum_adjustment", amount=format_money(invoice.minimum_adjustment),
            note=f"minimum commitment {format_money(invoice.minimum_charge)}")
    row("total", amount=format_money(invoice.total),
        note=f"rate card: {invoice.rate_card_name}")
    return buf.getvalue().encode("utf-8")
