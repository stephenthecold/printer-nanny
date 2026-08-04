"""Yield-gap detection: pages actually delivered per cartridge, against expected.

WHAT THIS IS
------------
A cartridge is sold by a page yield. This measures the pages a fleet's cartridges
actually deliver, per printer per supply slot, and reports the ones that fall
persistently short. A persistent shortfall is the non-OEM / refill signal --
remanufactured and refilled cartridges routinely deliver a fraction of the rated
pages, and nothing else in a monitoring system notices, because every individual
reading looks entirely normal.

WHAT IT DELIBERATELY IS NOT
---------------------------
**It is not a verdict of "this customer is buying counterfeit toner."** The
finding this module produces is *"yield below expected"*, which is a
measurement. Non-OEM is one interpretation of it; so are heavy page coverage, a
cartridge fitted half-used, a starter cartridge shipped with the device, and a
model whose datasheet yield assumes 5% coverage nobody prints at. The UI says
"below expected" and offers the non-OEM reading as an interpretation, never as
the headline, and every threshold that leads to a finding is operator-settable
with a conservative default. A false positive here is an MSP telling a customer
they are buying grey-market cartridges. That is not a bug report; it is a
relationship.

HOW A CARTRIDGE CHANGE IS DETECTED
----------------------------------
By the level RISING -- LibreNMS's cheap trick, and the only replacement signal a
printer gives. That test lives in ``supplies.refill_boundaries`` and is shared
verbatim with the depletion forecast, which needs the last boundary where this
needs every one of them. They must not be able to disagree: a forecast that has
reset its baseline while this is still accumulating pages against the previous
cartridge would produce a yield that is wrong with nothing to report it.

WHERE THE HISTORY COMES FROM
----------------------------
Both sources, partitioned by date and never overlapping: ``reading_rollups`` for
days older than ``retention.raw_days``, raw ``readings`` from there on. A drum
lasts a year and raw readings are kept 90 days, so a yield measurement that read
only ``readings`` could not measure the consumables that matter most. This is
exactly the use ``ReadingRollup.supply_snapshot`` was carried for -- see its
docstring, which says so before this module existed.

PERSIST vs COMPUTE
------------------
Same split as ``central.reorder``: **persist the measurement, compute the
judgement.** ``supply_cycles`` rows are measurements and cannot be recomputed
once the raw readings age out. The verdict -- enough evidence? below expected? --
is computed on read from those rows, the expectations and the operator's
thresholds, and is stored nowhere. So a threshold change takes effect on the next
render rather than the next worker cycle, and there is no stale verdict row to
reconcile when a cartridge is swapped.

WHERE EXPECTED YIELD COMES FROM, AND WHY BOTH
---------------------------------------------
There is no cartridge yield database in this repo and there is not going to be
one: it would need a SKU catalogue keyed to every model an MSP touches, and
keeping it current is a full-time data job. So two sources, in this precedence:

1. **Operator-entered** (``supply_yield_expectations``) -- the datasheet number,
   typed once per model. Authoritative when present, because it is a fact about
   the hardware rather than an inference from it.
2. **Fleet-derived baseline** -- the MEDIAN pages-per-cartridge for the same
   model and supply slot across the REST of the fleet. Self-calibrating, needs no
   data entry, and it is what makes this feature work on day one for an MSP with
   four hundred printers and no datasheets.

Both, rather than either, because each covers the other's failure. The baseline
needs samples an operator does not have to produce; the operator's number covers
the model this MSP owns exactly one of. And the baseline has a weakness that has
to be said out loud rather than buried: **if a customer's whole fleet of a model
is running non-OEM, the baseline calibrates to non-OEM and finds nothing.** It is
a comparison against peers, not against a datasheet. The UI states which source
produced each expectation for exactly that reason.

Median rather than mean, because one refilled cartridge in the sample would drag
a mean down and turn every peer into a finding.

CONFIDENCE, AND WHY ABSENCE IS NOT A FINDING
--------------------------------------------
A single short cartridge is not evidence. Three gates, all settable:

* ``yield.min_cycles`` (3) completed cartridges for that printer's slot before
  any verdict is offered. Below it the report shows the measurement and says
  INSUFFICIENT DATA -- it does not show a number dressed as a conclusion.
* ``yield.min_consumed_pct`` (60) of level consumed before a cycle counts at all.
  A cartridge pulled at 70% remaining says nothing about its yield.
* ``yield.baseline_min_cycles`` (5) from ``yield.baseline_min_printers`` (3)
  distinct OTHER printers before a fleet baseline exists at all.

Missing an expectation is reported as *no expectation*, not as *within
expected*; too few cycles is reported as *insufficient data*, not as *fine*.
This codebase has already had to correct a security report for grading absence
as safety, and the same rule applies to a commercial finding: we report what we
measured, and we say plainly when we measured nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session, contains_eager

from central import models as m
from central.supplies import REFILL_TOLERANCE_PCT, refill_boundaries

log = logging.getLogger("central.supply_yield")

#: How far back a first-ever scan reaches. A cartridge life is the unit here and
#: a drum can run a year, so this is generous -- but it is bounded, because the
#: alternative is a full-table scan of every rollup a deployment has ever
#: written, on the first pass after an upgrade, under the worker's leader lock.
#: Not a setting: it only affects how much history a brand-new install
#: back-fills once, and a knob whose only effect is invisible after the first
#: pass is a knob nobody can tune.
HISTORY_DAYS = 400

#: Verdicts. Four, and three of them are ways of having nothing to say -- which
#: is the honest shape of this measurement on most of a real fleet.
VERDICT_BELOW = "below_expected"
VERDICT_WITHIN = "within_expected"
VERDICT_INSUFFICIENT = "insufficient_data"
VERDICT_NO_EXPECTATION = "no_expectation"

#: Where an expected yield came from. Carried on every assessment because the
#: two are not equally strong evidence and an operator acting on a finding has
#: to be able to tell them apart.
SOURCE_OPERATOR = "operator"
SOURCE_FLEET = "fleet"

#: How far back the REPORT looks for cartridges to judge on. Two purposes, and
#: the second is the important one: it bounds a page render (a five-year-old
#: fleet is otherwise 100k+ cycle rows loaded to draw one table), and it keeps
#: the finding meaningful -- what a cartridge fitted in 2021 delivered is not
#: evidence about what the customer is buying now. Not a setting: an operator
#: tuning it would be tuning how far back an accusation reaches, which is not a
#: dial worth handing over for the one install that wants three years.
ASSESS_LOOKBACK_DAYS = 730

#: An ambiguous operator expectation is refused rather than guessed -- see
#: ``expectation_for``. Reported as its own state so the operator can fix the
#: overlapping rows instead of wondering why a model has no expectation.
SOURCE_AMBIGUOUS = "ambiguous"

#: Shortest usable ``model_tag``. Two characters match half the fleet; the same
#: floor the driver-package matcher uses, for the same reason.
MIN_MODEL_TAG = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """UTC-normalise a datetime that SQLite may have handed back naive."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def slot_color(color: Optional[str]) -> str:
    """The stored spelling of a supply colour: never NULL, always trimmed.

    ``""`` means the device reported no colour. It is a string rather than NULL
    because every lookup here is an equality on it, and SQL treats NULLs as
    distinct -- a NULL colour would make "the open cycle for this slot"
    unfindable and open a second cycle on every pass, forever.
    """
    return (color or "").strip()[:40]


def _median(values: Sequence[float]) -> float:
    """Median of a non-empty sequence. Callers guarantee non-empty."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (float(ordered[mid - 1]) + float(ordered[mid])) / 2.0


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class YieldThresholds:
    """The operator's yield policy, resolved from runtime settings.

    Read defensively throughout: a hand-edited or hostile ``app_settings`` row
    must degrade to the shipped default rather than raise inside a page render
    or a worker cycle.
    """

    enabled: bool = True
    scan_interval_min: int = 360
    min_cycles: int = 3
    min_consumed_pct: float = 60.0
    shortfall_pct: float = 30.0
    baseline_min_cycles: int = 5
    baseline_min_printers: int = 3
    emit_events: bool = False

    @classmethod
    def from_runtime(cls, runtime: Optional[Dict[str, Any]]) -> "YieldThresholds":
        rt = runtime or {}
        d = cls()

        def _num(key: str, fallback, cast):
            try:
                return cast(rt[key])
            except (KeyError, TypeError, ValueError):
                return fallback

        return cls(
            enabled=bool(rt.get("yield.enabled", d.enabled)),
            scan_interval_min=max(1, _num("yield.scan_interval_min", d.scan_interval_min, int)),
            # Floored at 2. One completed cartridge reported as a finding is the
            # single-sample conclusion this whole module exists to refuse, so an
            # operator cannot set it to 1 by typing one.
            min_cycles=max(2, _num("yield.min_cycles", d.min_cycles, int)),
            # Floored well above zero for the same reason: normalising a yield
            # by a 2% consumption multiplies both the measurement and its noise
            # by fifty.
            min_consumed_pct=min(100.0, max(20.0, _num(
                "yield.min_consumed_pct", d.min_consumed_pct, float))),
            # 0 would flag every cartridge that came in a page under expected;
            # 100 could never fire. Both ends clamped so a typo cannot turn the
            # report into noise or into silence.
            shortfall_pct=min(90.0, max(5.0, _num(
                "yield.shortfall_pct", d.shortfall_pct, float))),
            baseline_min_cycles=max(2, _num(
                "yield.baseline_min_cycles", d.baseline_min_cycles, int)),
            baseline_min_printers=max(2, _num(
                "yield.baseline_min_printers", d.baseline_min_printers, int)),
            emit_events=bool(rt.get("yield.emit_events", d.emit_events)),
        )

    @classmethod
    def load(cls, db: Session) -> "YieldThresholds":
        from central.runtime import load_settings  # lazy: avoid an import cycle

        return cls.from_runtime(load_settings(db))

    @property
    def shortfall_ratio(self) -> float:
        """Observed/expected at or below which a slot is flagged."""
        return max(0.0, 1.0 - (self.shortfall_pct / 100.0))


# --------------------------------------------------------------------------- #
# Scanning: readings -> cartridge cycles
# --------------------------------------------------------------------------- #
@dataclass
class _Point:
    """One observation of one supply slot: when, the meter, the level."""

    ts: datetime
    page_count: Optional[int]
    level_pct: float


def raw_cutoff(now: datetime, runtime: Optional[Dict[str, Any]] = None) -> datetime:
    """Midnight UTC at which raw readings take over from the daily rollup.

    Rollups summarise whole UTC days that closed before ``now - raw_days``, so
    reading rollups strictly BELOW this instant and raw readings from it onward
    partitions the history with no overlap and no gap. Overlapping the two would
    double-count a day's readings into a cycle's page total; leaving a gap would
    silently drop pages out of a cartridge's life.

    The boundary comes from ``retention.effective_raw_days`` rather than from a
    literal 90, so an operator who widens or narrows retention moves both halves
    of the partition at once.
    """
    from central.retention import effective_raw_days  # lazy: avoids an import cycle

    cut = (now - timedelta(days=effective_raw_days(runtime))).date()
    return datetime(cut.year, cut.month, cut.day, tzinfo=timezone.utc)


def _snapshot_levels(snapshot: Any) -> "Dict[Tuple[str, str], float]":
    """``supply_snapshot`` -> {(type, colour): level}, hostile-input tolerant.

    The snapshot is JSON an agent pushed. Every field is therefore attacker-
    shaped by this project's standing rule: a list of non-dicts, a level of
    ``"lots"``, a type of 40KB. Anything that does not parse into a bounded
    (str, str) -> float is skipped, because a scan that raises takes the whole
    worker cycle down for every other tenant.
    """
    out: Dict[Tuple[str, str], float] = {}
    if not isinstance(snapshot, list):
        return out
    for entry in snapshot:
        if not isinstance(entry, dict):
            continue
        raw_level = entry.get("level_pct")
        if raw_level is None or isinstance(raw_level, bool):
            continue
        try:
            level = float(raw_level)
        except (TypeError, ValueError):
            continue
        if level != level or level in (float("inf"), float("-inf")):
            continue  # NaN / inf: a level that is not a number is not a level
        stype = entry.get("type")
        if not isinstance(stype, str):
            continue
        out[(stype[:40], slot_color(entry.get("color")))] = level
    return out


def _series_for_printer(
    db: Session,
    printer_id: int,
    since: datetime,
    now: datetime,
    runtime: Optional[Dict[str, Any]] = None,
) -> "Dict[Tuple[str, str], List[_Point]]":
    """Every supply slot's observations for one printer, oldest first.

    ``since`` is EXCLUSIVE: it is the newest observation already folded into a
    cycle, so re-reading it would count its pages twice.

    **A day is read from exactly one source, and never from none.** Rollups
    supply the days at or below the raw cutoff; raw readings supply everything
    else, INCLUDING old days that have no rollup row. That last clause is not a
    nicety -- it is the difference between a measurement and a silently truncated
    one, and the end-to-end smoke is what found it:

    * ``retention.delete_enabled`` ships **False**, so on a default install the
      raw readings are all still there;
    * ``roll_up_readings`` only writes a rollup once a day has closed before
      ``now - raw_days``, and it works forward from a watermark, a bounded
      number of printer-days per cycle.

    So an install with a year of history and a worker that has not yet caught up
    has raw rows for days below the cutoff and no rollup for them. Reading only
    rollups there discarded that history entirely -- the cartridges measured
    shortest were the ones with the most history, which is precisely backwards.

    The rollup contributes its end-of-day point: ``last_ts`` with the day's last
    meter and its end-of-day supply snapshot, which are the same instant. A daily
    sample is sufficient because the unit of measurement is a cartridge -- toner
    does not move meaningfully inside one day.
    """
    cutoff = raw_cutoff(now, runtime)
    series: Dict[Tuple[str, str], List[_Point]] = {}

    def _add(ts: Optional[datetime], page_count: Optional[int], snapshot: Any) -> None:
        ts = _aware(ts)
        if ts is None:
            return
        for slot, level in _snapshot_levels(snapshot).items():
            series.setdefault(slot, []).append(_Point(ts, page_count, level))

    # Which old days a rollup already covers. Queried for the whole window
    # rather than derived from the rows below, so a rollup whose ``last_ts`` has
    # already been folded still masks its own day's raw rows -- otherwise the
    # pass after a rollup lands would count that day twice.
    rolled_days = set()
    if since < cutoff:
        rolled_days = set(db.scalars(
            select(m.ReadingRollup.day).where(
                m.ReadingRollup.printer_id == printer_id,
                m.ReadingRollup.day >= since.date(),
                m.ReadingRollup.day < cutoff.date(),
            )
        ))
        for row in db.scalars(
            select(m.ReadingRollup)
            .where(
                m.ReadingRollup.printer_id == printer_id,
                m.ReadingRollup.last_ts > since,
                m.ReadingRollup.last_ts < cutoff,
                m.ReadingRollup.supply_snapshot.is_not(None),
            )
            .order_by(m.ReadingRollup.day.asc())
        ):
            _add(row.last_ts, row.page_count, row.supply_snapshot)

    for row in db.scalars(
        select(m.Reading)
        .where(
            m.Reading.printer_id == printer_id,
            m.Reading.ts > since,
            m.Reading.supply_snapshot.is_not(None),
        )
        .order_by(m.Reading.ts.asc())
    ):
        ts = _aware(row.ts)
        if ts is not None and ts < cutoff and ts.date() in rolled_days:
            continue  # this day is the rollup's; taking both would double-count
        _add(row.ts, row.page_count, row.supply_snapshot)

    for points in series.values():
        points.sort(key=lambda p: p.ts)
    return series


def _open_cycles(db: Session, printer_id: int) -> "Dict[Tuple[str, str], m.SupplyCycle]":
    """This printer's still-running cycles, one per slot.

    A slot can only have one open cycle -- the cartridge that is in the machine.
    If a bug or a hand-edited row ever produced two, the NEWEST wins and the
    older is closed as incomplete rather than left to accumulate a second copy
    of every page forever.
    """
    rows = list(
        db.scalars(
            select(m.SupplyCycle)
            .where(
                m.SupplyCycle.printer_id == printer_id,
                m.SupplyCycle.ended_at.is_(None),
            )
            .order_by(m.SupplyCycle.started_at.asc(), m.SupplyCycle.id.asc())
        )
    )
    out: Dict[Tuple[str, str], m.SupplyCycle] = {}
    for row in rows:
        slot = (row.supply_type, row.color)
        stale = out.get(slot)
        if stale is not None:
            stale.ended_at = stale.last_ts
            stale.complete = False
            log.warning(
                "printer %s slot %s had two open supply cycles; closing #%s",
                printer_id, slot, stale.id,
            )
        out[slot] = row
    return out


def _watermark(db: Session, printer_id: int, now: datetime) -> datetime:
    """Newest observation already folded into any cycle for this printer.

    The scan reads strictly after this, so a poll is counted exactly once and
    the history is walked once rather than on every pass.

    Falls back to ``HISTORY_DAYS`` ago for a printer with no cycles at all. A
    printer that never reports a supply level therefore re-reads that window
    every pass and finds nothing -- which is bounded, indexed on
    ``(printer_id, ts)``, and strictly cheaper than the depletion forecast next
    door, which reads 30 days of the same table every 60 seconds.
    """
    newest = db.scalar(
        select(m.SupplyCycle.last_ts)
        .where(m.SupplyCycle.printer_id == printer_id)
        .order_by(m.SupplyCycle.last_ts.desc())
        .limit(1)
    )
    newest = _aware(newest)
    return newest if newest is not None else now - timedelta(days=HISTORY_DAYS)


def _start_cycle(printer_id: int, slot: "Tuple[str, str]", point: _Point,
                 now: datetime) -> m.SupplyCycle:
    return m.SupplyCycle(
        printer_id=printer_id,
        supply_type=slot[0],
        color=slot[1],
        started_at=point.ts,
        last_ts=point.ts,
        start_level_pct=point.level_pct,
        end_level_pct=point.level_pct,
        min_level_pct=point.level_pct,
        start_page_count=point.page_count,
        end_page_count=point.page_count,
        pages=0,
        readings_count=1,
        complete=False,
        created_at=now,
        updated_at=now,
    )


def _extend(cycle: m.SupplyCycle, point: _Point, now: datetime) -> None:
    """Fold one observation into the running cycle.

    Pages accumulate as POSITIVE deltas only, matching ``queries.positive_delta``:
    a meter that goes backwards is a reset or a replaced formatter board, not
    negative printing, and subtracting endpoints instead would let a reset
    manufacture a cartridge that yielded minus forty thousand pages.
    """
    if (
        point.page_count is not None
        and cycle.end_page_count is not None
        and point.page_count > cycle.end_page_count
    ):
        cycle.pages = (cycle.pages or 0) + (point.page_count - cycle.end_page_count)
    if point.page_count is not None:
        if cycle.start_page_count is None:
            # First meter we have seen in this cycle -- it opens the run rather
            # than contributing a delta against nothing.
            cycle.start_page_count = point.page_count
        cycle.end_page_count = point.page_count
    cycle.end_level_pct = point.level_pct
    if point.level_pct < cycle.min_level_pct:
        cycle.min_level_pct = point.level_pct
    cycle.last_ts = point.ts
    cycle.readings_count = (cycle.readings_count or 0) + 1
    cycle.updated_at = now


@dataclass
class ScanResult:
    """What one printer's scan did. ``closed`` is the cartridges that came out.

    The closed cycles are returned rather than re-queried because only the scan
    knows which rows IT closed: a query for "recently ended cycles" would also
    pick up ones an earlier pass closed, and re-announce a replacement that was
    already reported.
    """

    points: int = 0
    extended: int = 0
    opened: int = 0
    closed: "List[m.SupplyCycle]" = field(default_factory=list)

    @property
    def counts(self) -> "Dict[str, int]":
        return {
            "points": self.points,
            "extended": self.extended,
            "opened": self.opened,
            "closed": len(self.closed),
        }


def scan_printer(
    db: Session,
    printer: m.Printer,
    *,
    now: Optional[datetime] = None,
    runtime: Optional[Dict[str, Any]] = None,
    refill_tolerance: float = REFILL_TOLERANCE_PCT,
) -> ScanResult:
    """Fold this printer's new readings into its cartridge cycles.

    Adds and mutates rows on the session; the CALLER commits, so a failure on one
    printer does not half-write another's history.

    The state machine is deliberately tiny, because it is the whole measurement:
    the first observation of a slot OPENS a cycle (left-truncated -- we joined
    partway through some cartridge's life, which is why ``start_level_pct`` is
    recorded rather than assumed to be 100); a rise past the tolerance CLOSES the
    running cycle and opens a fresh one at that point; anything else EXTENDS.
    """
    now = _aware(now) or _now()
    result = ScanResult()
    since = _watermark(db, printer.id, now)
    series = _series_for_printer(db, printer.id, since, now, runtime)
    if not series:
        return result

    open_by_slot = _open_cycles(db, printer.id)

    for slot, points in series.items():
        cycle = open_by_slot.get(slot)
        # The boundary test runs over the previous level FOLLOWED BY the new
        # points, so a replacement that happened between two passes is seen. Off
        # by one deliberately: index i in `boundaries` refers to `levels[i]`,
        # and levels[0] is the carried-over end level when a cycle is open.
        carried = [cycle.end_level_pct] if cycle is not None else []
        levels = carried + [p.level_pct for p in points]
        boundary_at = set(refill_boundaries(levels, refill_tolerance=refill_tolerance))
        offset = len(carried)

        for i, point in enumerate(points):
            result.points += 1
            if cycle is None:
                cycle = _start_cycle(printer.id, slot, point, now)
                db.add(cycle)
                result.opened += 1
                continue
            if (i + offset) in boundary_at:
                cycle.ended_at = point.ts
                cycle.complete = True
                cycle.updated_at = now
                result.closed.append(cycle)
                cycle = _start_cycle(printer.id, slot, point, now)
                db.add(cycle)
                result.opened += 1
                continue
            _extend(cycle, point, now)
            result.extended += 1
    return result


# --------------------------------------------------------------------------- #
# Reading a cycle: what did this cartridge actually yield?
# --------------------------------------------------------------------------- #
def consumed_pct(cycle: m.SupplyCycle) -> float:
    """Level points this cartridge got through, 0-100.

    Measured to ``min_level_pct`` rather than ``end_level_pct``: a cartridge that
    read 2% and blipped back to 4% before it was pulled consumed 98 points, not
    96. Clamped at 0 because a slot whose level only ever rose consumed nothing.
    """
    return max(0.0, float(cycle.start_level_pct) - float(cycle.min_level_pct))


def observed_yield(cycle: m.SupplyCycle) -> Optional[float]:
    """Pages this cartridge would have delivered over a FULL cartridge, or None.

    Scaled by the level actually consumed, so a cartridge run from 100% to 20%
    and one run from 90% to 5% are comparable. That scaling assumes level falls
    linearly with pages, which is the same assumption the depletion forecast
    already makes across this codebase -- and it is why ``min_consumed_pct``
    exists: below some fraction the scaling multiplies noise faster than signal.

    ``None`` when the cycle has no page movement at all. Zero pages over a spent
    cartridge is not a yield of zero, it is a device whose meter we never read.
    """
    used = consumed_pct(cycle)
    if used <= 0 or not cycle.pages:
        return None
    return float(cycle.pages) * 100.0 / used


def qualifies(cycle: m.SupplyCycle, thresholds: YieldThresholds) -> bool:
    """Is this cycle evidence about yield at all?

    Three ways to be recorded and still say nothing: still running (the cartridge
    is in the machine), too little consumed (pulled early, or fitted half-used),
    or no metered pages. Each is kept as a row -- a cartridge history is worth
    having whether or not it is evidence -- and excluded from the calculation.
    """
    if not cycle.complete:
        return False
    if consumed_pct(cycle) < thresholds.min_consumed_pct:
        return False
    return observed_yield(cycle) is not None


# --------------------------------------------------------------------------- #
# Expected yield
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Expectation:
    """An expected pages-per-cartridge, and where it came from.

    ``source`` is carried rather than inferred because the two are not equally
    strong evidence: an operator's datasheet number is a fact about the
    hardware, a fleet median is a comparison against peers -- and a fleet all
    running non-OEM calibrates to non-OEM. An operator acting on a finding has
    to be able to tell which they are looking at.
    """

    pages: Optional[float]
    source: str
    #: How many cartridges the fleet baseline was computed from, and from how
    #: many other printers. Zero for an operator-entered number, which needs no
    #: sample. Shown, so nobody has to trust a median without its n.
    sample_cycles: int = 0
    sample_printers: int = 0
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.pages is not None and self.pages > 0


def _tag_matches(model: Optional[str], tag: str) -> bool:
    tag = (tag or "").strip().lower()
    if len(tag) < MIN_MODEL_TAG:
        return False
    return tag in (model or "").strip().lower()


def expectation_for(
    expectations: "Sequence[m.SupplyYieldExpectation]",
    model: Optional[str],
    supply_type: str,
    color: str,
) -> Optional[Expectation]:
    """The operator-entered expected yield for this model + slot, if any.

    Matching is the driver-package rule, and for the same reason -- SNMP model
    strings vary between firmware revisions, so a substring is the only workable
    match:

    * case-insensitive substring of ``printers.model``, at least 3 characters;
    * a row naming this exact colour beats a row naming none;
    * then the LONGEST tag wins, because it is the more specific claim;
    * an exact tie is **REFUSED**, not guessed. Two equally-specific rated
      yields for one cartridge is a coin flip whose losing side looks exactly
      like data, and the resulting gap would be an artefact of row order.

    Returns ``None`` when nothing matches -- which is a different fact from an
    ambiguous match, and is reported differently.
    """
    candidates = [
        row
        for row in expectations
        if row.supply_type == supply_type
        and row.color in ("", color)
        and _tag_matches(model, row.model_tag)
    ]
    if not candidates:
        return None

    def rank(row: m.SupplyYieldExpectation) -> "Tuple[int, int]":
        return (1 if row.color else 0, len(row.model_tag or ""))

    best = max(rank(row) for row in candidates)
    tied = [row for row in candidates if rank(row) == best]
    if len(tied) > 1:
        return Expectation(
            pages=None,
            source=SOURCE_AMBIGUOUS,
            detail=(
                "%d equally specific expected yields match this model "
                "(%s) -- refusing to guess between them"
                % (len(tied), ", ".join(sorted(r.model_tag for r in tied))[:160])
            ),
        )
    row = tied[0]
    return Expectation(
        pages=float(row.expected_pages),
        source=SOURCE_OPERATOR,
        detail="operator-entered for model tag %r" % (row.model_tag,),
    )


def fleet_baseline(
    samples: "Sequence[Tuple[int, float]]",
    exclude_printer_id: int,
    thresholds: YieldThresholds,
) -> Optional[Expectation]:
    """Median pages-per-cartridge across the REST of the fleet, or None.

    ``samples`` is [(printer_id, observed_yield)] for qualifying cycles of the
    same model and slot.

    The subject printer is excluded from its own baseline, which is not a nicety:
    include it and a printer running non-OEM contributes its own low yields to
    the expectation it is measured against, so the more consistently it
    under-delivers the less it looks like it does.

    Below ``baseline_min_cycles`` samples from ``baseline_min_printers`` distinct
    printers there is no baseline and this returns ``None``. It never falls back
    to a smaller sample with a caveat attached: a caveat on a number is read as a
    number.
    """
    peers = [(pid, y) for pid, y in samples if pid != exclude_printer_id]
    if len(peers) < thresholds.baseline_min_cycles:
        return None
    printers = {pid for pid, _ in peers}
    if len(printers) < thresholds.baseline_min_printers:
        return None
    return Expectation(
        pages=_median([y for _, y in peers]),
        source=SOURCE_FLEET,
        sample_cycles=len(peers),
        sample_printers=len(printers),
        detail=(
            "median of %d cartridge(s) across %d other printer(s) of this model"
            % (len(peers), len(printers))
        ),
    )


# --------------------------------------------------------------------------- #
# The assessment (computed on read, stored nowhere)
# --------------------------------------------------------------------------- #
@dataclass
class SlotAssessment:
    """One printer's one supply slot, measured against what it should deliver."""

    printer_id: int
    printer_label: str
    printer_name: str
    client_id: int
    client_name: str
    site_name: str
    model: str
    supply_type: str
    color: str
    supply_label: str

    cycles_total: int          # every completed cartridge we recorded
    cycles_counted: int        # ...of which qualified as evidence
    observed_pages: Optional[float]   # median observed yield across those
    expected: Expectation
    verdict: str
    #: observed/expected. None whenever either side is missing -- never 1.0 as a
    #: stand-in, which would render as "exactly as expected".
    ratio: Optional[float] = None
    last_replaced_at: Optional[datetime] = None
    reasons: List[str] = field(default_factory=list)

    @property
    def is_below(self) -> bool:
        return self.verdict == VERDICT_BELOW

    @property
    def has_verdict(self) -> bool:
        return self.verdict in (VERDICT_BELOW, VERDICT_WITHIN)

    @property
    def shortfall_pct(self) -> Optional[float]:
        """How far under expected, in percent. ``None`` without both numbers."""
        if self.ratio is None:
            return None
        return max(0.0, (1.0 - self.ratio) * 100.0)

    @property
    def dedupe_key(self) -> str:
        return "supply.yield:printer:%s:slot:%s:%s" % (
            self.printer_id, self.supply_type, self.color,
        )


def slot_label(supply_type: str, color: str) -> str:
    return ("%s %s" % (color, supply_type)).strip() if color else supply_type


def _one_line(value: Optional[str], limit: int = 120) -> str:
    """Collapse a device- or operator-supplied string to one clean line.

    Model, hostname and colour are SNMP strings from a device on a customer LAN.
    Jinja escapes them for HTML and the catalogue normalises them for the event
    payload; this is what keeps a newline out of a label built here.
    """
    if not value:
        return ""
    return " ".join(str(value).split())[:limit]


def _printer_name(printer: m.Printer) -> str:
    return _one_line(
        printer.display_name or printer.model or printer.hostname, limit=80
    ) or "printer"


def _printer_label(printer: m.Printer) -> str:
    return "%s @ %s" % (_printer_name(printer), _one_line(printer.ip, limit=64))


def _judge(
    counted: "List[m.SupplyCycle]",
    expectation: Optional[Expectation],
    thresholds: YieldThresholds,
) -> "Tuple[str, Optional[float], Optional[float], List[str]]":
    """(verdict, observed, ratio, reasons) for one slot's qualifying cycles.

    Order matters and is the whole confidence rule: not enough cartridges is
    INSUFFICIENT DATA whether or not an expectation exists, because the number we
    would compare is not one we are willing to stand behind. Only then does a
    missing expectation become the answer.
    """
    reasons: List[str] = []
    yields = [y for y in (observed_yield(c) for c in counted) if y is not None]

    if len(yields) < thresholds.min_cycles:
        reasons.append(
            "%d of %d completed cartridge(s) needed before a verdict"
            % (len(yields), thresholds.min_cycles)
        )
        observed = _median(yields) if yields else None
        return VERDICT_INSUFFICIENT, observed, None, reasons

    # Median across this slot's cartridges, so one short cartridge -- a heavy
    # month, a jam-heavy week, a starter cartridge -- cannot carry the verdict.
    observed = _median(yields)
    reasons.append(
        "median of %d completed cartridge(s): ~%s pages each"
        % (len(yields), "{:,.0f}".format(observed))
    )

    if expectation is not None and expectation.source == SOURCE_AMBIGUOUS:
        reasons.append(expectation.detail)
        return VERDICT_NO_EXPECTATION, observed, None, reasons
    if expectation is None or not expectation.known:
        reasons.append(
            "no expected yield: none entered for this model, and the fleet has "
            "too few comparable cartridges to derive one"
        )
        return VERDICT_NO_EXPECTATION, observed, None, reasons

    ratio = observed / float(expectation.pages)
    reasons.append(
        "expected ~%s pages (%s)"
        % ("{:,.0f}".format(expectation.pages), expectation.detail)
    )
    if ratio <= thresholds.shortfall_ratio:
        reasons.append(
            "%.0f%% below expected, at or past the %.0f%% shortfall threshold"
            % ((1.0 - ratio) * 100.0, thresholds.shortfall_pct)
        )
        return VERDICT_BELOW, observed, ratio, reasons
    reasons.append(
        "%.0f%% of expected, inside the %.0f%% shortfall threshold"
        % (ratio * 100.0, thresholds.shortfall_pct)
    )
    return VERDICT_WITHIN, observed, ratio, reasons


def assessments(
    db: Session,
    *,
    client_id: Optional[int] = None,
    thresholds: Optional[YieldThresholds] = None,
) -> "List[SlotAssessment]":
    """Every measured supply slot, worst shortfall first.

    ``client_id`` scopes the RESULT in SQL. The fleet BASELINE deliberately is
    not scoped: a rated yield is a property of the hardware, so the honest
    comparison for one customer's Brother is every Brother of that model this MSP
    manages. Scoping the baseline per tenant would leave a customer with two
    printers permanently un-assessable, and would let one customer's non-OEM
    habit define what "normal" means for their own devices.

    Only approved printers are considered: a device in the discovery queue is
    not yet part of anybody's fleet.
    """
    thresholds = thresholds or YieldThresholds.load(db)
    expectations = list(db.scalars(select(m.SupplyYieldExpectation)))

    # Every completed cycle in the fleet, once. The baseline needs cross-tenant
    # samples, so this cannot be narrowed by client_id even when the report is.
    stmt = (
        select(m.SupplyCycle)
        .join(m.SupplyCycle.printer)
        .join(m.Printer.client)
        .join(m.Printer.site)
        .options(
            contains_eager(m.SupplyCycle.printer).contains_eager(m.Printer.client),
            contains_eager(m.SupplyCycle.printer).contains_eager(m.Printer.site),
        )
        .where(
            m.SupplyCycle.complete.is_(True),
            m.Printer.discovery_state == m.DiscoveryState.approved,
        )
        .order_by(m.SupplyCycle.ended_at.asc())
    )

    by_slot: Dict[Tuple[int, str, str], List[m.SupplyCycle]] = {}
    printers: Dict[int, m.Printer] = {}
    # (model_key, type, colour) -> [(printer_id, observed)] for the baseline.
    fleet: Dict[Tuple[str, str, str], List[Tuple[int, float]]] = {}
    for cycle in db.scalars(stmt):
        printer = cycle.printer
        printers[printer.id] = printer
        by_slot.setdefault(
            (printer.id, cycle.supply_type, cycle.color), []
        ).append(cycle)
        if qualifies(cycle, thresholds):
            observed = observed_yield(cycle)
            model_key = _one_line(printer.model, 200).lower()
            if model_key and observed is not None:
                fleet.setdefault(
                    (model_key, cycle.supply_type, cycle.color), []
                ).append((printer.id, observed))

    out: List[SlotAssessment] = []
    for (printer_id, supply_type, color), cycles in by_slot.items():
        printer = printers[printer_id]
        if client_id is not None and printer.client_id != client_id:
            continue
        counted = [c for c in cycles if qualifies(c, thresholds)]
        model_key = _one_line(printer.model, 200).lower()

        expectation = expectation_for(expectations, printer.model, supply_type, color)
        if expectation is None:
            expectation = fleet_baseline(
                fleet.get((model_key, supply_type, color), []),
                printer_id,
                thresholds,
            )
        if expectation is None:
            expectation = Expectation(pages=None, source="", detail="")

        verdict, observed, ratio, reasons = _judge(counted, expectation, thresholds)
        ended = [_aware(c.ended_at) for c in cycles if c.ended_at is not None]
        out.append(
            SlotAssessment(
                printer_id=printer_id,
                printer_label=_printer_label(printer),
                printer_name=_printer_name(printer),
                client_id=printer.client_id,
                client_name=_one_line(printer.client.name if printer.client else "", 80),
                site_name=_one_line(printer.site.name if printer.site else "", 80),
                model=_one_line(printer.model, 80),
                supply_type=supply_type,
                color=color,
                supply_label=slot_label(supply_type, _one_line(color, 40)),
                cycles_total=len(cycles),
                cycles_counted=len(counted),
                observed_pages=observed,
                expected=expectation,
                verdict=verdict,
                ratio=ratio,
                last_replaced_at=max(ended) if ended else None,
                reasons=reasons,
            )
        )
    return sort_assessments(out)


def sort_assessments(rows: "Sequence[SlotAssessment]") -> "List[SlotAssessment]":
    """Findings first, deepest shortfall first, then everything else.

    A slot with no verdict sorts after every slot that has one, rather than
    interleaving by ratio: a page whose first rows are "we cannot say" teaches an
    operator that the page has nothing to say. The final tie-break is the
    printer id and slot, so two renders of identical data do not reshuffle.
    """
    rank = {VERDICT_BELOW: 0, VERDICT_WITHIN: 1, VERDICT_INSUFFICIENT: 2,
            VERDICT_NO_EXPECTATION: 3}
    return sorted(
        rows,
        key=lambda r: (
            rank.get(r.verdict, 9),
            r.ratio if r.ratio is not None else float("inf"),
            r.printer_id,
            r.supply_type,
            r.color,
        ),
    )


def summarize(rows: "Sequence[SlotAssessment]") -> "Dict[str, int]":
    """Counts for the summary strip. Never raises on an empty list.

    ``below`` and ``no_verdict`` are separate counters on purpose. "We measured
    this and it falls short" and "we could not measure this" are different
    claims, and a report that adds them together is the security-posture mistake
    (absence counted as a finding) wearing a commercial hat.
    """
    return {
        "slots": len(rows),
        "below": sum(1 for r in rows if r.verdict == VERDICT_BELOW),
        "within": sum(1 for r in rows if r.verdict == VERDICT_WITHIN),
        "insufficient": sum(1 for r in rows if r.verdict == VERDICT_INSUFFICIENT),
        "no_expectation": sum(1 for r in rows if r.verdict == VERDICT_NO_EXPECTATION),
        "no_verdict": sum(1 for r in rows if not r.has_verdict),
        "printers": len({r.printer_id for r in rows}),
        "clients": len({r.client_id for r in rows}),
    }


def recent_replacements(
    db: Session,
    *,
    client_id: Optional[int] = None,
    limit: int = 25,
) -> "List[m.SupplyCycle]":
    """The fleet's most recent cartridge changes, newest first.

    This is the operator-visible form of LibreNMS's "Toner X was replaced" log
    line, and it is what makes the yield numbers above checkable: a technician
    who can see when a cartridge went in can tell a genuine short yield from a
    cartridge somebody swapped early.

    Deliberately NOT written to ``printer_events``. That table is the counting
    source for occurrence-rate alert rules, and a rule matching every event above
    a severity would start counting toner changes as printer faults -- a new
    feature silently changing an existing one's numbers.
    """
    stmt = (
        select(m.SupplyCycle)
        .join(m.SupplyCycle.printer)
        .options(contains_eager(m.SupplyCycle.printer))
        .where(m.SupplyCycle.ended_at.is_not(None))
        .order_by(m.SupplyCycle.ended_at.desc(), m.SupplyCycle.id.desc())
        .limit(max(1, min(200, limit)))
    )
    if client_id is not None:
        stmt = stmt.where(m.Printer.client_id == client_id)
    return list(db.scalars(stmt))
