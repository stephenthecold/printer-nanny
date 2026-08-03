"""Readings retention: daily rollup, then opt-in, audited, batched pruning.

``readings`` is append-only and, until this module, had no retention whatsoever:
at 500 printers on the default 300s poll interval that is ~52M rows a year, on
the largest table in the schema, growing forever. The policy implemented here is
**raw readings kept 90 days; everything older collapsed to one
``ReadingRollup`` per printer per UTC day, kept forever**.

Four rules govern this file. Each of them exists because its opposite is a way
to lose a customer's data quietly.

1. **Deletion never precedes rollup, structurally.** A printer-day's rollup
   UPSERT and the DELETE of the exact raw rows it summarises happen in ONE
   transaction. There is no ordering to get wrong and no window to crash in: the
   rollup is committed if and only if the readings are gone. A crash mid-pass
   re-does that printer-day from unmodified raw rows.

2. **Deletion is off by default and stays off until an operator says otherwise.**
   ``retention.delete_enabled`` ships False. With it off this module is purely
   additive -- it writes rollups and touches not one reading -- so the rollups
   an operator needs for billing accumulate long before anything is destroyed.

3. **The work is bounded per cycle.** The worker runs under a single-leader
   advisory lock, so one long statement stalls alerting for the whole fleet.
   A single ``DELETE`` over a 52M-row table is exactly that. Every delete here
   is scoped to one printer's one day (~288 rows at the default poll interval)
   and chunked further by id; the number of batches per cycle is capped, so a
   multi-year backlog drains over many cycles instead of in one lock-holding
   statement.

4. **Deletion by explicit id, never by predicate.** The rows are aggregated and
   then deleted BY THE IDS THAT WERE AGGREGATED. Re-issuing the range predicate
   would be shorter and is wrong: ``ReadingIn.ts`` is client-supplied
   (``services.apply_reading`` does ``ts = reading.ts or now``), so an agent can
   backdate a push, and under READ COMMITTED a row inserted between the SELECT
   and the DELETE would be destroyed without ever being counted. Deleting the
   measured set makes that impossible -- a straggler simply survives to the next
   pass, where the merge rule below folds it in.

**What deliberately does NOT change: the supply forecast.**
``FORECAST_HISTORY_WINDOW_DAYS`` (worker/jobs.py) and
``RUNWAY_HISTORY_WINDOW_DAYS`` (queries.py) are both 30, gated at
``MIN_HISTORY_DAYS = 3.0``. A 90-day raw window clears the forecast's reach by
3x, so both keep reading ``readings`` exclusively and no forecast ever sees a
rolled-up row. That is not an accident to be tidied up later -- it is the
invariant ``tests/test_readings_retention.py`` pins by computing a forecast
before and after a full rollup+prune pass and asserting the two are identical.
Rollup rows carry per-supply levels anyway; see ``ReadingRollup`` for why
storing data nothing reads is the correct call here.

**Progress marking.** How the pass finds work depends on whether deletion is on,
because the two modes have different notions of "done":

* deletion ON  -- ``MIN(readings.ts)`` is the whole answer. It is an O(1) btree
  probe, it advances on its own as rows are removed, and because it points at
  the genuinely oldest surviving row it also sweeps up anything backdated below
  a watermark. Self-clearing, so it terminates.
* deletion OFF -- nothing is ever removed, so ``MIN(readings.ts)`` never moves
  and would re-roll the entire history on every cycle. A watermark
  (``retention.rollup_watermark`` in ``app_settings``, the last UTC date fully
  rolled up) carries progress instead. Days backdated below the watermark are
  then not rolled up, which is safe precisely because nothing is being deleted:
  the raw readings are all still there, and enabling deletion later starts from
  ``MIN(ts)`` and rolls them up before touching them.

The watermark is stored in ``app_settings`` under a key that is NOT in
``runtime.SPECS`` -- ``save_settings`` only ever writes spec keys and
``load_settings`` only ever reads them, so a marker row there is inert: it
cannot appear in the settings UI and an operator cannot clobber it by saving a
form.

**No interval gate, on purpose.** An idle pass costs a single ``MIN(ts)`` index
probe and returns, so running it every worker cycle is cheaper than the state a
gate would need. Nothing here should acquire a schedule of its own.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from central import audit
from central import models as m
from central.runtime import load_settings

log = logging.getLogger("central.retention")

#: Progress marker for the deletion-disabled path. Not a ``runtime.Spec`` -- see
#: the module docstring for why that is what keeps it safe.
WATERMARK_KEY = "retention.rollup_watermark"

#: Ids per ``DELETE ... WHERE id IN (...)``. Postgres caps a statement at 65535
#: bound parameters, and a smaller statement is a shorter lock besides.
_DELETE_CHUNK = 1000


def min_raw_days() -> int:
    """Floor for ``retention.raw_days``, derived from the forecast's own reach.

    The forecast reads raw readings exclusively over a 30-day window. A raw
    window shorter than that does not fail, warn, or look wrong anywhere -- it
    quietly starves every supply-runway estimate, which is the precise failure
    mode this codebase keeps paying for. So the floor is COMPUTED from the two
    window constants rather than written down as 30: widen either of them and
    this moves with it, instead of becoming a stale number that silently stops
    protecting anything.

    +1 because whole UTC days are rolled: a day is eligible once it closed
    before ``now - raw_days``, so equality would put the window's own edge in
    play. Imported lazily for the same reason ``queries.supply_runway`` does it
    -- ``worker.jobs`` pulls in the channel stack, and ``worker.run`` already
    imports this module.
    """
    from central.queries import RUNWAY_HISTORY_WINDOW_DAYS
    from central.worker.jobs import FORECAST_HISTORY_WINDOW_DAYS

    return max(FORECAST_HISTORY_WINDOW_DAYS, RUNWAY_HISTORY_WINDOW_DAYS) + 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; treat them as UTC (same as jobs.py)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _day_bounds(day: date) -> Tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


# --------------------------------------------------------------------------- #
# Watermark (deletion-disabled progress marker)
# --------------------------------------------------------------------------- #
def read_watermark(db: Session) -> Optional[date]:
    """Last UTC date fully rolled up, or None. A malformed value reads as None.

    Never raises: a marker this pass cannot parse must degrade to "start from
    the oldest reading" (correct, merely slower), never take the worker down.
    """
    row = db.get(m.AppSetting, WATERMARK_KEY)
    if row is None or not row.value:
        return None
    try:
        return date.fromisoformat(str(row.value))
    except (TypeError, ValueError):
        log.warning("ignoring unparseable %s: %r", WATERMARK_KEY, row.value)
        return None


def write_watermark(db: Session, day: date) -> None:
    """Advance the marker. Monotonic -- an earlier day never moves it back."""
    row = db.get(m.AppSetting, WATERMARK_KEY)
    if row is None:
        db.add(m.AppSetting(key=WATERMARK_KEY, value=day.isoformat()))
        return
    current = read_watermark(db)
    if current is not None and current >= day:
        return
    row.value = day.isoformat()
    row.updated_at = _now()


# --------------------------------------------------------------------------- #
# Aggregation (pure -- no session, so the rules are unit-testable on their own)
# --------------------------------------------------------------------------- #
#: One raw reading, reduced to the columns a rollup needs:
#: ``(id, ts, page_count, mono_count, color_count, supply_snapshot)``. The pass
#: selects these columns rather than whole ORM entities: a printer-day is ~288
#: rows, and there is no reason to hydrate ``Reading`` objects (or fill the
#: identity map with rows that are about to be deleted) to compute six numbers.
RawRow = Tuple[Any, ...]

_METERS = ("page_count", "mono_count", "color_count")


def aggregate(rows: Sequence[RawRow]) -> Optional[Dict[str, Any]]:
    """Collapse one printer-day's raw rows into rollup field values.

    ``rows`` must be ordered oldest-first. Returns None for an empty input --
    "no readings" is not a day worth a row, and writing one would make
    ``readings_count = 0`` ambiguous between "device was silent" and "not yet
    rolled up".

    Each meter is tracked as first-non-null and last-non-null INDEPENDENTLY of
    the others and of ``first_ts``/``last_ts``. Devices report a mono/colour
    split intermittently (see ``ReadingIn``: "None when the device/provider
    reports no split -- we never synthesize it"), so taking the meters from
    whichever row happens to be first or last would write NULL for a day that
    reported perfectly good counts an hour later.
    """
    if not rows:
        return None

    out: Dict[str, Any] = {
        "readings_count": len(rows),
        "first_ts": _aware(rows[0][1]),
        "last_ts": _aware(rows[0][1]),
        "supply_snapshot": None,
    }
    for name in _METERS:
        out[name] = None
        out[name + "_start"] = None

    for _rid, ts, page, mono, color, snap in rows:
        ts = _aware(ts)
        if ts < out["first_ts"]:
            out["first_ts"] = ts
        if ts > out["last_ts"]:
            out["last_ts"] = ts
        for name, value in zip(_METERS, (page, mono, color)):
            if value is None:
                continue
            if out[name + "_start"] is None:
                out[name + "_start"] = value
            out[name] = value
        if snap is not None:
            out["supply_snapshot"] = snap
    return out


def merge(existing: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """Fold a straggler aggregate into an already-pruned day's stored values.

    Only reached when ``raw_pruned`` is True: the raw rows behind ``existing``
    are gone, so recomputing would keep the straggler and discard the day. Which
    side supplies an END value is decided by ``last_ts`` and a START value by
    ``first_ts`` -- with a non-null fallback to the other side, because "later"
    does not imply "reported this meter".
    """
    out = dict(existing)
    out["readings_count"] = (existing.get("readings_count") or 0) + new["readings_count"]

    if new["first_ts"] < existing["first_ts"]:
        out["first_ts"] = new["first_ts"]
        for name in _METERS:
            key = name + "_start"
            out[key] = new[key] if new[key] is not None else existing[key]
    else:
        for name in _METERS:
            key = name + "_start"
            out[key] = existing[key] if existing[key] is not None else new[key]

    if new["last_ts"] > existing["last_ts"]:
        out["last_ts"] = new["last_ts"]
        for name in _METERS:
            out[name] = new[name] if new[name] is not None else existing[name]
        out["supply_snapshot"] = (
            new["supply_snapshot"]
            if new["supply_snapshot"] is not None
            else existing["supply_snapshot"]
        )
    else:
        for name in _METERS:
            out[name] = existing[name] if existing[name] is not None else new[name]
        out["supply_snapshot"] = (
            existing["supply_snapshot"]
            if existing["supply_snapshot"] is not None
            else new["supply_snapshot"]
        )
    return out


def _fields_of(row: m.ReadingRollup) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "readings_count": row.readings_count or 0,
        "first_ts": _aware(row.first_ts),
        "last_ts": _aware(row.last_ts),
        "supply_snapshot": row.supply_snapshot,
    }
    for name in _METERS:
        out[name] = getattr(row, name)
        out[name + "_start"] = getattr(row, name + "_start")
    return out


def _apply(row: m.ReadingRollup, fields: Dict[str, Any]) -> None:
    for key, value in fields.items():
        setattr(row, key, value)
    row.updated_at = _now()


# --------------------------------------------------------------------------- #
# One printer-day: aggregate, upsert, and (optionally) delete -- one transaction
# --------------------------------------------------------------------------- #
def _process_printer_day(
    db: Session, printer_id: int, day: date, delete_enabled: bool
) -> Tuple[int, int]:
    """Roll up one printer's one day. Returns ``(rows_rolled, rows_pruned)``.

    Commits on success; rolls back and returns ``(0, 0)`` if the safety check
    below refuses. The whole point of this function's shape is that its commit
    is the single atomic event: after it, either the rollup exists and the raw
    rows are gone, or neither happened.
    """
    start_ts, end_ts = _day_bounds(day)
    rows: List[RawRow] = list(
        db.execute(
            select(
                m.Reading.id,
                m.Reading.ts,
                m.Reading.page_count,
                m.Reading.mono_count,
                m.Reading.color_count,
                m.Reading.supply_snapshot,
            )
            .where(
                m.Reading.printer_id == printer_id,
                m.Reading.ts >= start_ts,
                m.Reading.ts < end_ts,
            )
            .order_by(m.Reading.ts.asc(), m.Reading.id.asc())
        ).all()
    )
    fields = aggregate(rows)
    if fields is None:
        return 0, 0
    ids = [r[0] for r in rows]

    row = db.scalar(
        select(m.ReadingRollup).where(
            m.ReadingRollup.printer_id == printer_id,
            m.ReadingRollup.day == day,
        )
    )
    if row is None:
        row = m.ReadingRollup(printer_id=printer_id, day=day, raw_pruned=False)
        db.add(row)
        _apply(row, fields)
    elif row.raw_pruned:
        # Raw rows for this day are already gone -- these are stragglers.
        _apply(row, merge(_fields_of(row), fields))
    else:
        # Raw rows are all still present, so this recomputation IS the day.
        _apply(row, fields)

    pruned = 0
    if delete_enabled:
        db.flush()
        # Belt and braces over rule 1. The transaction already guarantees this,
        # but the cost of re-asking is one indexed read and the cost of being
        # wrong is a customer's history. A rollup that is absent, or that claims
        # to cover fewer readings than we are about to destroy, is not a rollup
        # we may delete against.
        covered = db.scalar(
            select(m.ReadingRollup.readings_count).where(
                m.ReadingRollup.printer_id == printer_id,
                m.ReadingRollup.day == day,
            )
        )
        if covered is None or covered < len(ids):
            db.rollback()
            log.error(
                "retention refused to prune printer %s %s: rollup covers %r of %d readings",
                printer_id, day.isoformat(), covered, len(ids),
            )
            audit.record(
                db, None, None, "readings.prune_refused",
                target=f"printer:{printer_id} {day.isoformat()}",
                detail=(
                    f"rollup covers {covered!r} of {len(ids)} readings; "
                    "raw rows left in place"
                ),
            )
            db.commit()
            return 0, 0
        row.raw_pruned = True
        for i in range(0, len(ids), _DELETE_CHUNK):
            db.execute(
                sql_delete(m.Reading)
                .where(m.Reading.id.in_(ids[i:i + _DELETE_CHUNK]))
                .execution_options(synchronize_session=False)
            )
        pruned = len(ids)

    db.commit()
    return len(ids), pruned


# --------------------------------------------------------------------------- #
# The worker job
# --------------------------------------------------------------------------- #
def roll_up_readings(db: Session, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Worker job: roll eligible readings into daily rollups, then optionally prune.

    Registered in ``central.worker.run.JOBS``. Returns a summary dict for the
    cycle log (empty when there was nothing to do, so an idle fleet does not
    print a line of zeroes every cycle).

    Eligibility is by WHOLE UTC DAY, and only days that closed strictly before
    ``now - raw_days``. A day still in progress is never rolled up: its rollup
    would be written from a partial day and -- with deletion on -- the rest of
    that day would then arrive as stragglers to be merged. Bucketing whole days
    also means the retained raw window is *at least* ``raw_days``, up to 24h
    more. Erring long is the right direction when the alternative is deleting
    something the 30-day forecast still wants.
    """
    now = _aware(now) or _now()
    rt = load_settings(db)
    if not rt.get("retention.rollup_enabled", True):
        return {}

    try:
        raw_days = int(rt.get("retention.raw_days", 90) or 90)
    except (TypeError, ValueError):
        raw_days = 90
    floor = min_raw_days()
    if raw_days < floor:
        # Said out loud, every pass, rather than clamped in silence: an operator
        # who typed 7 needs to know why they are still getting 31 days, and a
        # guard nobody can observe is indistinguishable from no guard at all.
        log.warning(
            "retention.raw_days=%s is below the %s-day floor the supply forecast "
            "reads over; using %s. Lower it further only by narrowing the "
            "forecast window first.",
            raw_days, floor, floor,
        )
        raw_days = floor
    try:
        budget = max(1, int(rt.get("retention.max_batches_per_cycle", 200) or 200))
    except (TypeError, ValueError):
        budget = 200
    delete_enabled = bool(rt.get("retention.delete_enabled", False))

    cutoff_day = (now - timedelta(days=raw_days)).date()

    # O(1) on the btree over readings.ts. This is the whole cost of an idle pass.
    oldest = _aware(db.scalar(select(func.min(m.Reading.ts))))
    if oldest is None:
        return {}
    start_day = oldest.date()
    if not delete_enabled:
        watermark = read_watermark(db)
        if watermark is not None:
            start_day = max(start_day, watermark + timedelta(days=1))
    if start_day >= cutoff_day:
        return {}

    day = start_day
    used = 0
    days_done = 0
    rolled_rows = 0
    pruned_rows = 0
    printer_days = 0
    last_complete: Optional[date] = None
    first_touched: Optional[date] = None

    while day < cutoff_day and used < budget:
        day_start, day_end = _day_bounds(day)
        printer_ids = list(
            db.scalars(
                select(m.Reading.printer_id)
                .where(m.Reading.ts >= day_start, m.Reading.ts < day_end)
                .distinct()
            )
        )
        used += 1  # the day scan itself is a unit of work, empty or not
        complete = True
        for printer_id in printer_ids:
            if used >= budget:
                complete = False
                break
            n_rolled, n_pruned = _process_printer_day(db, printer_id, day, delete_enabled)
            used += 1
            if n_rolled:
                printer_days += 1
                rolled_rows += n_rolled
                pruned_rows += n_pruned
                if first_touched is None:
                    first_touched = day
        if not complete:
            break
        days_done += 1
        last_complete = day
        day += timedelta(days=1)

    if last_complete is not None:
        write_watermark(db, last_complete)

    if pruned_rows:
        # Destructive and irreversible, so it is on the audit trail with the
        # window that authorised it. Counts and dates only -- no device data,
        # nothing secret.
        audit.record(
            db, None, None, "readings.pruned",
            target=f"days:{first_touched.isoformat()}..{last_complete or day}",
            detail=(
                f"deleted {pruned_rows} raw reading(s) across {printer_days} "
                f"printer-day(s) after rollup; retention window {raw_days}d "
                f"(cutoff {cutoff_day.isoformat()})"
            ),
        )
    db.commit()

    if not (rolled_rows or days_done):
        return {}
    return {
        "readings_rolled_up": rolled_rows,
        "readings_pruned": pruned_rows,
        "rollup_printer_days": printer_days,
        "rollup_days_complete": days_done,
    }
