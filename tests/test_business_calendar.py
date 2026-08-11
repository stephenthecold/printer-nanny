"""Business-day supply delivery and order-window policy."""

from __future__ import annotations

from datetime import date

from central import reorder, runtime
from central.business_calendar import (
    ProcurementCalendar,
    effective_order_window_days,
    parse_closures,
    us_federal_holidays,
)
from central.supply_orders import default_delivery_date


def test_us_federal_holidays_include_observed_dates():
    holidays = us_federal_holidays(2026)
    assert date(2026, 7, 3) in holidays  # July 4 is Saturday
    assert date(2026, 11, 26) in holidays  # fourth Thursday
    assert date(2026, 12, 25) in holidays


def test_five_day_eta_skips_weekend_holiday_and_custom_closure():
    policy = ProcurementCalendar(
        delivery_business_days=5,
        safety_business_days=2,
        custom_closures=frozenset({date(2026, 7, 8)}),
    )
    # Thu Jul 2 -> Jul 3 observed holiday -> weekend -> Jul 8 custom closure.
    assert policy.estimated_delivery(date(2026, 7, 2)) == date(2026, 7, 13)
    assert default_delivery_date(date(2026, 7, 2), policy=policy) == date(
        2026, 7, 13
    )


def test_order_window_adds_two_business_day_safety_margin():
    policy = ProcurementCalendar()
    assert policy.order_window_business_days == 7
    assert policy.order_window_days(date(2026, 7, 2)) == 12
    assert policy.add_business_days(date(2026, 7, 2), 7) == date(2026, 7, 14)


def test_custom_closures_are_bounded_deduplicated_and_defensive():
    assert parse_closures("2026-08-12, nonsense 2026-08-12 2026-08-13") == {
        date(2026, 8, 12),
        date(2026, 8, 13),
    }
    policy = ProcurementCalendar.from_runtime(
        {
            "procurement.delivery_business_days": "999",
            "procurement.safety_business_days": "bad",
            "procurement.observe_us_holidays": "false",
        }
    )
    assert policy.delivery_business_days == 60
    assert policy.safety_business_days == 2
    assert policy.observe_us_holidays is False


def test_calendar_day_override_only_widens_the_computed_window():
    today = date(2026, 7, 2)
    assert effective_order_window_days({}, today) == 12
    assert effective_order_window_days({"alerts.reorder_lead_days": 5}, today) == 12
    assert effective_order_window_days({"alerts.reorder_lead_days": 45}, today) == 45


def test_runtime_settings_drive_reorder_threshold_and_eta(db):
    runtime.save_settings(
        db,
        {
            "procurement.delivery_business_days": "3",
            "procurement.safety_business_days": "1",
            "procurement.observe_us_holidays": "on",
            "procurement.closed_dates": "2026-07-08",
        },
        sections={"Supply ordering"},
    )
    policy = ProcurementCalendar.load(db)
    assert policy.estimated_delivery(date(2026, 7, 2)) == date(2026, 7, 9)
    thresholds = reorder.ReorderThresholds.load(db, today=date(2026, 7, 2))
    # Jul 6, 7, 9, 10 are the four delivery+safety business days.
    assert thresholds.lead_days == 8
