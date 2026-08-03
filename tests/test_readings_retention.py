"""Readings retention: daily rollup, opt-in pruning, and the forecast invariant.

The load-bearing test in here is ``test_forecast_is_identical_across_a_full_pass``.
Everything else guards a rule that, if it broke, would delete a customer's
history quietly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from central import models as m
from central import retention
from central.queries import RUNWAY_HISTORY_WINDOW_DAYS, supply_runway
from central.runtime import SPECS, load_settings, save_settings
from central.worker.jobs import FORECAST_HISTORY_WINDOW_DAYS, forecast_supplies

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _printer(db, ip="10.0.0.10", name="P1"):
    client = m.Client(name=f"C-{ip}")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name=f"S-{ip}")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip=ip, display_name=name,
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    return printer


def _snapshot(level):
    return [{"type": "toner", "color": "black", "level_pct": round(level, 2)}]


def _seed_series(db, printer, *, days, per_day=4, now=NOW, start_pages=10_000):
    """``days`` days of readings ending at ``now``, ``per_day`` readings a day.

    Toner declines linearly ~0.5%/day from 100 and page counts climb, so the
    regression in ``forecast_days_to_empty`` has a clean slope to find.
    """
    made = 0
    for d in range(days):
        day_offset = days - 1 - d
        for k in range(per_day):
            ts = now - timedelta(days=day_offset, hours=k * 3)
            elapsed = days - 1 - day_offset
            level = max(1.0, 100.0 - 0.5 * (elapsed + k / float(per_day)))
            pages = start_pages + made * 25
            db.add(m.Reading(
                printer_id=printer.id, ts=ts,
                page_count=pages, mono_count=pages - 500, color_count=500,
                status=m.PrinterStatus.ok, supply_snapshot=_snapshot(level),
            ))
            made += 1
    db.commit()
    return made


def _enable(db, **overrides):
    """Save the retention section.

    ``retention.rollup_enabled`` is spelled out every time on purpose: with
    ``sections`` given, ``save_settings`` reads an ABSENT checkbox as unchecked
    (that is the whole point of the ``sections`` argument), so omitting it here
    would quietly switch rollup off and every test using this helper would pass
    against a feature that never ran.
    """
    form = {
        "retention.rollup_enabled": "on",
        "retention.raw_days": "90",
        "retention.max_batches_per_cycle": "5000",
    }
    form.update({k: str(v) for k, v in overrides.items()})
    save_settings(db, form, sections={"Data retention"})
    db.commit()
    assert load_settings(db)["retention.rollup_enabled"] is True


def _readings(db, printer_id=None):
    stmt = select(func.count()).select_from(m.Reading)
    if printer_id is not None:
        stmt = stmt.where(m.Reading.printer_id == printer_id)
    return db.scalar(stmt)


# --------------------------------------------------------------------------- #
# The invariant the whole feature is built around
# --------------------------------------------------------------------------- #
def test_forecast_is_identical_across_a_full_pass(db):
    """A rollup+prune pass over data older than the window changes no forecast.

    This is the constraint the retention policy was designed around: the raw
    window (90d) clears the forecast's reach (30d) by 3x, so a forecast can
    never be reading a row that retention is allowed to touch. Asserted on both
    forecast entry points -- the worker's ``forecast_supplies`` (which persists
    days_to_empty onto the Supply row) and ``queries.supply_runway`` (which the
    fleet listing and the customer portal render).
    """
    assert FORECAST_HISTORY_WINDOW_DAYS == 30
    assert RUNWAY_HISTORY_WINDOW_DAYS == 30

    printer = _printer(db)
    supply = m.Supply(
        printer_id=printer.id, type=m.SupplyType.toner, color="black", level_pct=40.0
    )
    db.add(supply)
    db.commit()

    total = _seed_series(db, printer, days=200)
    assert _readings(db) == total

    cutoff = NOW - timedelta(days=FORECAST_HISTORY_WINDOW_DAYS)
    in_window_before = db.scalar(
        select(func.count()).select_from(m.Reading).where(m.Reading.ts >= cutoff)
    )
    assert in_window_before > 0

    before_runway = supply_runway(db, [printer.id])
    forecast_supplies(db, now=NOW)
    db.refresh(supply)
    before_dte = supply.days_to_empty
    assert before_dte is not None, "fixture must produce a real forecast to compare"

    _enable(db, **{"retention.delete_enabled": "on"})
    summary = retention.roll_up_readings(db, now=NOW)

    after_raw = _readings(db)
    rollups = db.scalar(select(func.count()).select_from(m.ReadingRollup))
    assert summary["readings_pruned"] > 0
    assert after_raw < total, "the pass must actually have deleted something"
    assert rollups > 0

    # Nothing inside the forecast window was touched: same row count, and the
    # oldest surviving reading is still older than the window's edge.
    assert db.scalar(
        select(func.count()).select_from(m.Reading).where(m.Reading.ts >= cutoff)
    ) == in_window_before
    oldest = retention._aware(db.scalar(select(func.min(m.Reading.ts))))
    assert oldest >= NOW - timedelta(days=91), oldest
    assert oldest < cutoff, "the raw window must still extend past the forecast window"

    # ...so both forecasts are identical.
    after_runway = supply_runway(db, [printer.id])
    assert after_runway[printer.id]["days"] == before_runway[printer.id]["days"]

    forecast_supplies(db, now=NOW)
    db.refresh(supply)
    assert supply.days_to_empty == before_dte


def test_forecast_history_window_stays_well_inside_the_raw_window(db):
    """The 3x margin is a design constraint, not a coincidence -- pin it."""
    default = {s.key: s.default for s in SPECS}["retention.raw_days"]
    assert default == 90
    assert default >= 3 * FORECAST_HISTORY_WINDOW_DAYS
    assert default >= 3 * RUNWAY_HISTORY_WINDOW_DAYS


def test_raw_days_below_the_forecast_window_is_refused(db, caplog):
    """A too-short window would starve every forecast without failing anywhere."""
    assert retention.min_raw_days() == max(
        FORECAST_HISTORY_WINDOW_DAYS, RUNWAY_HISTORY_WINDOW_DAYS
    ) + 1

    printer = _printer(db)
    _seed_series(db, printer, days=200, per_day=2)
    _enable(db, **{"retention.raw_days": "7", "retention.delete_enabled": "on"})

    with caplog.at_level("WARNING", logger="central.retention"):
        for _ in range(40):
            if not retention.roll_up_readings(db, now=NOW):
                break
    assert any("below the" in r.message for r in caplog.records), \
        "clamping in silence is indistinguishable from no guard at all"

    kept = retention._aware(db.scalar(select(func.min(m.Reading.ts))))
    # 7 days was refused, so the forecast's 30-day window is still fully covered.
    assert kept <= NOW - timedelta(days=FORECAST_HISTORY_WINDOW_DAYS)


# --------------------------------------------------------------------------- #
# Aggregation rules (pure)
# --------------------------------------------------------------------------- #
def test_aggregate_takes_each_meter_first_and_last_non_null_independently():
    """A device reporting a colour split intermittently must not roll up as NULL."""
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        (1, t, 100, None, None, None),
        (2, t + timedelta(hours=1), 110, 90, 20, _snapshot(50)),
        (3, t + timedelta(hours=2), 120, None, None, _snapshot(49)),
    ]
    agg = retention.aggregate(rows)
    assert agg["readings_count"] == 3
    assert agg["first_ts"] == t and agg["last_ts"] == t + timedelta(hours=2)
    assert (agg["page_count_start"], agg["page_count"]) == (100, 120)
    # Reported only in the middle row -- both ends must still be that value,
    # not the None the first/last reading happened to carry.
    assert (agg["mono_count_start"], agg["mono_count"]) == (90, 90)
    assert (agg["color_count_start"], agg["color_count"]) == (20, 20)
    assert agg["supply_snapshot"] == _snapshot(49)


def test_aggregate_of_nothing_is_nothing():
    assert retention.aggregate([]) is None


def test_aggregate_is_order_independent():
    """Ordering comes from the query, but the rules must not silently depend on it."""
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        (1, t, 100, None, None, _snapshot(60)),
        (2, t + timedelta(hours=5), 150, 10, 5, _snapshot(55)),
    ]
    assert retention.aggregate(rows)["page_count"] == 150
    assert retention.aggregate(list(reversed(rows)))["last_ts"] == t + timedelta(hours=5)


def test_merge_folds_a_later_straggler_onto_a_pruned_day():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    existing = retention.aggregate([(1, t, 100, 90, 10, _snapshot(50))])
    new = retention.aggregate([(2, t + timedelta(hours=6), 140, None, None, _snapshot(48))])
    out = retention.merge(existing, new)
    assert out["readings_count"] == 2
    assert out["first_ts"] == t and out["last_ts"] == t + timedelta(hours=6)
    assert out["page_count_start"] == 100 and out["page_count"] == 140
    # The straggler reported no split, so the earlier day's values survive.
    assert out["mono_count"] == 90 and out["color_count"] == 10
    assert out["supply_snapshot"] == _snapshot(48)


def test_merge_folds_an_earlier_straggler_onto_a_pruned_day():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    existing = retention.aggregate([(1, t + timedelta(hours=6), 140, 120, 20, _snapshot(48))])
    new = retention.aggregate([(2, t, 100, None, None, _snapshot(50))])
    out = retention.merge(existing, new)
    assert out["first_ts"] == t and out["last_ts"] == t + timedelta(hours=6)
    assert out["page_count_start"] == 100 and out["page_count"] == 140
    assert out["supply_snapshot"] == _snapshot(48)  # end-of-day is still the later one


# --------------------------------------------------------------------------- #
# Rollup behaviour
# --------------------------------------------------------------------------- #
def test_rollup_is_non_destructive_by_default(db):
    """Deletion ships OFF. A default install rolls up and deletes nothing."""
    printer = _printer(db)
    total = _seed_series(db, printer, days=120)
    assert load_settings(db)["retention.delete_enabled"] is False

    summary = retention.roll_up_readings(db, now=NOW)

    assert _readings(db) == total, "default config must not delete a single reading"
    assert summary["readings_rolled_up"] > 0
    assert db.scalar(select(func.count()).select_from(m.ReadingRollup)) > 0
    assert all(
        r.raw_pruned is False for r in db.scalars(select(m.ReadingRollup))
    )


def test_rollup_carries_both_meter_ends_and_the_supply_snapshot(db):
    printer = _printer(db)
    day = date(2026, 1, 5)
    base = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)
    for k, (pages, level) in enumerate([(1000, 80.0), (1200, 78.0), (1500, 76.0)]):
        db.add(m.Reading(
            printer_id=printer.id, ts=base + timedelta(hours=k * 4),
            page_count=pages, mono_count=pages - 100, color_count=100,
            supply_snapshot=_snapshot(level),
        ))
    db.commit()

    retention.roll_up_readings(db, now=NOW)

    row = db.scalar(select(m.ReadingRollup).where(m.ReadingRollup.day == day))
    assert row is not None
    assert row.readings_count == 3
    assert (row.page_count_start, row.page_count) == (1000, 1500)
    assert (row.mono_count_start, row.mono_count) == (900, 1400)
    assert (row.color_count_start, row.color_count) == (100, 100)
    assert row.supply_snapshot == _snapshot(76.0)  # end-of-day levels, F1 requirement
    assert row.first_ts.replace(tzinfo=timezone.utc) == base
    assert row.last_ts.replace(tzinfo=timezone.utc) == base + timedelta(hours=8)


def test_rollup_never_touches_a_day_inside_the_raw_window(db):
    printer = _printer(db)
    _seed_series(db, printer, days=95, per_day=2)
    before = _readings(db)

    _enable(db, **{"retention.delete_enabled": "on"})
    retention.roll_up_readings(db, now=NOW)

    kept_from = retention._aware(db.scalar(select(func.min(m.Reading.ts))))
    assert kept_from >= NOW - timedelta(days=91)
    assert _readings(db) < before
    # Whole-day bucketing means we keep >= raw_days, never less.
    assert kept_from <= NOW - timedelta(days=89)


def test_rollup_is_idempotent_while_raw_rows_remain(db):
    """Re-running with deletion off must recompute, not accumulate."""
    printer = _printer(db)
    _seed_series(db, printer, days=120, per_day=3)

    retention.roll_up_readings(db, now=NOW)
    first = {(r.printer_id, r.day): r.readings_count for r in db.scalars(select(m.ReadingRollup))}
    count_first = len(first)

    # Clear the watermark so the second pass really re-processes the same days.
    db.query(m.AppSetting).filter(m.AppSetting.key == retention.WATERMARK_KEY).delete()
    db.commit()
    retention.roll_up_readings(db, now=NOW)

    second = {(r.printer_id, r.day): r.readings_count for r in db.scalars(select(m.ReadingRollup))}
    assert len(second) == count_first
    assert second == first, "a re-run must recompute, not double-count"


def test_a_straggler_after_pruning_is_merged_not_lost(db):
    """The case that makes ``raw_pruned`` load-bearing rather than decorative."""
    printer = _printer(db)
    day = date(2026, 1, 5)
    base = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)
    db.add(m.Reading(printer_id=printer.id, ts=base, page_count=1000,
                     supply_snapshot=_snapshot(80.0)))
    db.commit()

    _enable(db, **{"retention.delete_enabled": "on"})
    retention.roll_up_readings(db, now=NOW)
    row = db.scalar(select(m.ReadingRollup).where(m.ReadingRollup.day == day))
    assert row.raw_pruned is True and row.readings_count == 1
    assert _readings(db) == 0

    # An agent backdates a push into a day whose raw rows are already gone.
    db.add(m.Reading(printer_id=printer.id, ts=base + timedelta(hours=10),
                     page_count=1400, supply_snapshot=_snapshot(70.0)))
    db.commit()
    retention.roll_up_readings(db, now=NOW)

    db.refresh(row)
    assert row.readings_count == 2, "the straggler must be merged, not replace the day"
    assert row.page_count_start == 1000 and row.page_count == 1400
    assert row.supply_snapshot == _snapshot(70.0)


def test_only_one_rollup_row_per_printer_day(db):
    printer = _printer(db)
    _seed_series(db, printer, days=120, per_day=3)
    retention.roll_up_readings(db, now=NOW)
    retention.roll_up_readings(db, now=NOW)
    rows = list(db.scalars(select(m.ReadingRollup)))
    assert len(rows) == len({(r.printer_id, r.day) for r in rows})


def test_rollups_are_per_printer(db):
    a = _printer(db, ip="10.0.0.1", name="A")
    b = _printer(db, ip="10.0.0.2", name="B")
    _seed_series(db, a, days=120, per_day=2, start_pages=1000)
    _seed_series(db, b, days=120, per_day=2, start_pages=9000)
    retention.roll_up_readings(db, now=NOW)
    by_printer = {}
    for r in db.scalars(select(m.ReadingRollup)):
        by_printer.setdefault(r.printer_id, []).append(r)
    assert set(by_printer) == {a.id, b.id}
    assert all(r.readings_count == 2 for rows in by_printer.values() for r in rows)


# --------------------------------------------------------------------------- #
# Deletion safety
# --------------------------------------------------------------------------- #
def test_deletion_requires_a_rollup_that_covers_the_rows(db, monkeypatch):
    """If the rollup is not really there, nothing may be deleted."""
    printer = _printer(db)
    _seed_series(db, printer, days=120, per_day=2)
    before = _readings(db)
    _enable(db, **{"retention.delete_enabled": "on"})

    # Simulate a rollup that under-covers: the guard must refuse and leave the
    # raw rows alone rather than trust the transaction it is standing in.
    real = retention.aggregate

    def undercount(rows):
        out = real(rows)
        if out is not None:
            out["readings_count"] = 0
        return out

    monkeypatch.setattr(retention, "aggregate", undercount)
    retention.roll_up_readings(db, now=NOW)

    assert _readings(db) == before, "a refused prune must delete nothing"
    refusals = list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "readings.prune_refused")
    ))
    assert refusals, "a refusal is a security-relevant boundary; audit it"


def test_pruning_is_audited(db):
    printer = _printer(db)
    _seed_series(db, printer, days=120, per_day=2)
    _enable(db, **{"retention.delete_enabled": "on"})
    retention.roll_up_readings(db, now=NOW)

    rows = list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "readings.pruned")
    ))
    assert len(rows) == 1
    detail = rows[0].detail
    assert "deleted" in detail and "90d" in detail
    assert rows[0].user_id is None  # system actor, same as printer.auto_approve


def test_no_audit_row_when_nothing_was_deleted(db):
    printer = _printer(db)
    _seed_series(db, printer, days=120, per_day=2)
    retention.roll_up_readings(db, now=NOW)  # deletion off by default
    assert not list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "readings.pruned")
    ))


def test_rollup_disabled_does_nothing_at_all(db):
    printer = _printer(db)
    total = _seed_series(db, printer, days=120, per_day=2)
    # Deliberately NOT via _enable: omitting rollup_enabled from a sectioned save
    # is how an operator switches it off (unchecked box posts nothing). Deletion
    # is on, so this also proves the kill switch outranks it.
    save_settings(db, {"retention.delete_enabled": "on"}, sections={"Data retention"})
    db.commit()
    assert load_settings(db)["retention.rollup_enabled"] is False
    assert load_settings(db)["retention.delete_enabled"] is True

    assert retention.roll_up_readings(db, now=NOW) == {}
    assert _readings(db) == total
    assert db.scalar(select(func.count()).select_from(m.ReadingRollup)) == 0


# --------------------------------------------------------------------------- #
# Batching / progress
# --------------------------------------------------------------------------- #
def test_work_is_bounded_by_the_batch_budget(db):
    printer = _printer(db)
    _seed_series(db, printer, days=200, per_day=2)
    before = _readings(db)

    _enable(db, **{
        "retention.delete_enabled": "on",
        "retention.max_batches_per_cycle": "6",
    })

    summary = retention.roll_up_readings(db, now=NOW)
    assert 0 < summary["rollup_printer_days"] <= 6
    partial = _readings(db)
    assert 0 < before - partial < before, "one cycle must not drain the whole backlog"

    # ...and successive cycles make progress rather than stalling.
    for _ in range(5):
        retention.roll_up_readings(db, now=NOW)
    assert _readings(db) < partial


def test_backlog_drains_over_repeated_cycles(db):
    printer = _printer(db)
    _seed_series(db, printer, days=140, per_day=2)
    _enable(db, **{
        "retention.delete_enabled": "on",
        "retention.max_batches_per_cycle": "8",
    })

    for _ in range(60):
        if not retention.roll_up_readings(db, now=NOW):
            break
    else:
        pytest.fail("retention never reached a steady state")

    kept = retention._aware(db.scalar(select(func.min(m.Reading.ts))))
    assert kept >= NOW - timedelta(days=91)


def test_watermark_carries_progress_when_deletion_is_off(db):
    """With nothing deleted, MIN(ts) never moves -- the watermark is what advances."""
    printer = _printer(db)
    _seed_series(db, printer, days=140, per_day=2)
    _enable(db, **{"retention.max_batches_per_cycle": "8"})
    assert load_settings(db)["retention.delete_enabled"] is False

    retention.roll_up_readings(db, now=NOW)
    first = retention.read_watermark(db)
    assert first is not None

    retention.roll_up_readings(db, now=NOW)
    assert retention.read_watermark(db) > first, "a second cycle must move forward"


def test_watermark_never_moves_backwards(db):
    db.add(m.AppSetting(key=retention.WATERMARK_KEY, value="2026-05-01"))
    db.commit()
    retention.write_watermark(db, date(2026, 4, 1))
    db.commit()
    assert retention.read_watermark(db) == date(2026, 5, 1)
    retention.write_watermark(db, date(2026, 6, 1))
    db.commit()
    assert retention.read_watermark(db) == date(2026, 6, 1)


def test_unparseable_watermark_degrades_instead_of_raising(db):
    db.add(m.AppSetting(key=retention.WATERMARK_KEY, value="not-a-date"))
    db.commit()
    assert retention.read_watermark(db) is None

    printer = _printer(db)
    _seed_series(db, printer, days=120, per_day=2)
    assert retention.roll_up_readings(db, now=NOW)["readings_rolled_up"] > 0


def test_watermark_is_not_a_settings_spec(db):
    """It lives in app_settings but out of SPECS, so save_settings cannot clobber it."""
    assert retention.WATERMARK_KEY not in {s.key: s for s in SPECS}
    db.add(m.AppSetting(key=retention.WATERMARK_KEY, value="2026-05-01"))
    db.commit()
    save_settings(db, {"retention.raw_days": "60"}, sections={"Data retention"})
    db.commit()
    assert retention.read_watermark(db) == date(2026, 5, 1)
    assert retention.WATERMARK_KEY not in load_settings(db)


def test_idle_pass_is_a_no_op(db):
    printer = _printer(db)
    _seed_series(db, printer, days=5, per_day=2)  # everything inside the raw window
    assert retention.roll_up_readings(db, now=NOW) == {}
    assert db.scalar(select(func.count()).select_from(m.ReadingRollup)) == 0


def test_empty_database_is_a_no_op(db):
    assert retention.roll_up_readings(db, now=NOW) == {}


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_job_is_registered_last_in_the_worker_cycle(db):
    from central.worker.run import JOBS

    assert retention.roll_up_readings in JOBS
    assert JOBS[-1] is retention.roll_up_readings, (
        "retention is budget-bounded and the only job that deletes; it must never "
        "be why alerting was late in a cycle"
    )


def test_settings_are_operator_editable(db):
    keys = {s.key: s for s in SPECS}
    for key in (
        "retention.rollup_enabled", "retention.raw_days",
        "retention.delete_enabled", "retention.max_batches_per_cycle",
    ):
        assert keys[key].section == "Data retention"
    from central.runtime import SETTINGS_GROUPS

    assert "Data retention" in SETTINGS_GROUPS["polling"][1]
    assert load_settings(db)["retention.delete_enabled"] is False
    assert load_settings(db)["retention.raw_days"] == 90
