"""Actionable supply forecasting: the regression fit + confidence gate, the
persisted Supply.days_to_empty/forecast_at columns, and the predicted_depletion
alert lifecycle (open at the reorder lead-time, dedupe per supply, auto-resolve
on refill).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from central import models as m
from central import runtime
from central.worker import jobs


def _approved_printer(db, ip="10.0.0.7"):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip=ip,
        model="HP Color M553",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    return printer


def _two_point_estimate(points):
    """The OLD first-point/last-point slope, reproduced here so a test can show
    the regression fit is genuinely different on a noisy series."""
    pts = sorted(points, key=lambda p: p[0])
    (t0, l0), (t1, l1) = pts[0], pts[-1]
    days = (t1 - t0).total_seconds() / 86400.0
    rate = (l0 - l1) / days
    return round(l1 / rate, 1)


# --------------------------------------------------------------------------- #
# 1) Regression fit vs. the old two-point slope
# --------------------------------------------------------------------------- #
def test_regression_fit_beats_two_point_on_noisy_series():
    now = datetime.now(timezone.utc)
    # A clean ~1.5%/day decline from 60% (so ~26 days of true runway), except the
    # FINAL reading is a downward noise spike (30 instead of ~39). The
    # endpoint-only slope over-weights that one spike into a steeper rate and a
    # pessimistic runway; the least-squares fit averages it out.
    levels = {0: 60.0, 2: 57.0, 4: 54.0, 6: 51.0, 8: 48.0, 10: 45.0, 12: 42.0, 14: 30.0}
    points = [(now - timedelta(days=14 - d), lvl) for d, lvl in levels.items()]

    regression = jobs.forecast_days_to_empty(points)
    two_point = _two_point_estimate(points)

    assert regression is not None
    # The regression does NOT collapse to the noisy endpoint-only answer...
    assert regression != two_point
    # ...it sees through the final spike to a longer, steadier runway, while the
    # two-point slope reports a markedly shorter (more pessimistic) one.
    assert two_point < 15.0
    assert regression > 16.0
    assert regression > two_point


def test_regression_matches_two_point_on_clean_line():
    """A perfectly linear series must reproduce the legacy answer exactly:
    the least-squares fit passes through every point, so nothing changes."""
    now = datetime.now(timezone.utc)
    points = [(now - timedelta(days=10), 50.0), (now, 30.0)]  # 2%/day -> 15.0
    assert jobs.forecast_days_to_empty(points) == 15.0


# --------------------------------------------------------------------------- #
# 2) Confidence gate
# --------------------------------------------------------------------------- #
def test_confidence_gate_returns_none_on_too_little_history():
    now = datetime.now(timezone.utc)
    # Two readings 12 hours apart: a real drop, but the window is far shorter
    # than FORECAST_MIN_HISTORY_DAYS, so the slope isn't trustworthy yet.
    points = [(now - timedelta(hours=12), 80.0), (now, 50.0)]
    assert jobs.forecast_days_to_empty(points) is None
    # The same drop spread over enough days clears the gate.
    spaced = [(now - timedelta(days=6), 80.0), (now, 50.0)]
    assert jobs.forecast_days_to_empty(spaced) is not None


def test_confidence_gate_none_with_a_single_point():
    now = datetime.now(timezone.utc)
    assert jobs.forecast_days_to_empty([(now, 50.0)]) is None


# --------------------------------------------------------------------------- #
# 3) Predicted-depletion alert: opens at threshold, dedupes per supply, persists
# --------------------------------------------------------------------------- #
def _add_declining_series(db, printer, *, supplies, start, drop_per_day, days=14):
    """Append `days` of readings whose snapshot has every supply declining."""
    now = datetime.now(timezone.utc)
    for d in range(days):
        ts = now - timedelta(days=(days - 1) - d)
        snap = []
        for sp in supplies:
            lvl = round(start[sp["color"]] - drop_per_day * d, 1)
            snap.append({"type": sp["type"].value, "color": sp["color"], "level_pct": lvl})
        db.add(m.Reading(printer_id=printer.id, ts=ts, status=m.PrinterStatus.ok,
                         supply_snapshot=snap))


def test_predicted_depletion_opens_and_dedupes_per_supply(db):
    printer = _approved_printer(db)
    # Two depleting toners on one (color) printer. Both end low enough that at
    # ~2%/day they're inside the default 14-day reorder window.
    specs = [
        {"type": m.SupplyType.toner, "color": "black"},
        {"type": m.SupplyType.toner, "color": "cyan"},
    ]
    for sp in specs:
        db.add(m.Supply(printer_id=printer.id, type=sp["type"], color=sp["color"], level_pct=18.0))
    _add_declining_series(
        db, printer, supplies=specs,
        start={"black": 46.0, "cyan": 46.0}, drop_per_day=2.0, days=14,
    )
    db.commit()

    res = jobs.forecast_supplies(db)
    # One actionable alert PER supply, not one aggregate for the printer.
    assert res["forecast_alerts_opened"] == 2
    assert res["supplies_forecast_low"] == 2
    opened = db.query(m.Alert).filter_by(
        state=m.AlertState.open, type=m.AlertConditionType.predicted_depletion
    ).all()
    assert len(opened) == 2
    assert {a.printer_id for a in opened} == {printer.id}
    # Distinct dedupe keys, one per (printer, supply).
    assert len({a.dedupe_key for a in opened}) == 2

    # Persisted forecast on the supply rows (both depleting -> both stamped).
    supplies = db.query(m.Supply).filter_by(printer_id=printer.id).all()
    assert res["supplies_forecasted"] == 2
    for sup in supplies:
        assert sup.days_to_empty is not None
        assert sup.days_to_empty <= 14
        assert sup.forecast_at is not None

    # Re-running does not duplicate the open alerts (dedupe per supply).
    res2 = jobs.forecast_supplies(db)
    assert res2["forecast_alerts_opened"] == 0
    assert db.query(m.Alert).filter_by(
        state=m.AlertState.open, type=m.AlertConditionType.predicted_depletion
    ).count() == 2


def test_predicted_depletion_skipped_outside_lead_time(db):
    printer = _approved_printer(db)
    specs = [{"type": m.SupplyType.toner, "color": "black"}]
    db.add(m.Supply(printer_id=printer.id, type=m.SupplyType.toner, color="black", level_pct=80.0))
    # Plenty of runway: 95 -> 81 over 14 days = 1%/day, ~80 days left.
    _add_declining_series(
        db, printer, supplies=specs,
        start={"black": 95.0}, drop_per_day=1.0, days=14,
    )
    db.commit()

    res = jobs.forecast_supplies(db)
    assert res["forecast_alerts_opened"] == 0
    assert res["supplies_forecast_low"] == 0
    # Still forecasted + persisted, just not alert-worthy yet.
    assert res["supplies_forecasted"] == 1
    sup = db.query(m.Supply).filter_by(printer_id=printer.id).one()
    assert sup.days_to_empty is not None and sup.days_to_empty > 14


def test_reorder_lead_days_setting_widens_window(db):
    printer = _approved_printer(db)
    specs = [{"type": m.SupplyType.toner, "color": "black"}]
    db.add(m.Supply(printer_id=printer.id, type=m.SupplyType.toner, color="black", level_pct=30.0))
    # ~30 days of runway (60 -> 32 at 2%/day): outside the default 14, inside 45.
    _add_declining_series(
        db, printer, supplies=specs,
        start={"black": 60.0}, drop_per_day=2.0, days=14,
    )
    db.commit()

    assert jobs.forecast_supplies(db)["forecast_alerts_opened"] == 0

    runtime.save_settings(db, {"alerts.reorder_lead_days": "45"}, sections={"Alerts"})
    res = jobs.forecast_supplies(db)
    assert res["forecast_alerts_opened"] == 1


# --------------------------------------------------------------------------- #
# 4) Auto-resolve on refill
# --------------------------------------------------------------------------- #
def test_predicted_depletion_auto_resolves_on_refill(db):
    printer = _approved_printer(db)
    specs = [{"type": m.SupplyType.toner, "color": "black"}]
    supply = m.Supply(printer_id=printer.id, type=m.SupplyType.toner, color="black", level_pct=18.0)
    db.add(supply)
    _add_declining_series(
        db, printer, supplies=specs,
        start={"black": 46.0}, drop_per_day=2.0, days=14,
    )
    db.commit()

    assert jobs.forecast_supplies(db)["forecast_alerts_opened"] == 1
    assert db.query(m.Alert).filter_by(
        state=m.AlertState.open, type=m.AlertConditionType.predicted_depletion
    ).count() == 1

    # Cartridge swapped: a fresh 100% reading (a jump up past refill_tolerance),
    # then a few more high, gently-declining readings spanning several days so the
    # recent segment is unambiguously the NEW cartridge -- with a long runway,
    # not projected to deplete within the lead-time.
    now = datetime.now(timezone.utc)
    for d, lvl in ((0, 100.0), (2, 99.0), (4, 98.0)):
        db.add(m.Reading(
            printer_id=printer.id, ts=now + timedelta(days=d), status=m.PrinterStatus.ok,
            supply_snapshot=[{"type": "toner", "color": "black", "level_pct": lvl}],
        ))
    supply.level_pct = 100.0
    db.commit()

    res = jobs.forecast_supplies(db)
    assert res["forecast_alerts_resolved"] == 1
    assert db.query(m.Alert).filter_by(
        state=m.AlertState.open, type=m.AlertConditionType.predicted_depletion
    ).count() == 0
    # The persisted estimate is cleared once the fresh cartridge is no longer
    # projected to run out (refill-aware fit + lead-time both pass).
    db.refresh(supply)
    assert supply.days_to_empty is None or supply.days_to_empty > 14
