"""Business-day policy for supply delivery and order-now decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy.orm import Session

MAX_BUSINESS_DAYS = 60
MAX_CALENDAR_OVERRIDE_DAYS = 365
MAX_CUSTOM_CLOSURES = 366


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    day = date(year, month, 1)
    return day + timedelta(days=(weekday - day.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    day = next_month - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def us_federal_holidays(year: int) -> set[date]:
    """Observed US federal holidays for one calendar year."""
    return {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        _observed(date(year, 11, 11)),
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    }


def parse_closures(raw: Any) -> frozenset[date]:
    """Parse comma/whitespace separated ISO dates; invalid entries are ignored."""
    values = str(raw or "").replace(",", " ").split()
    result: set[date] = set()
    for value in values[:MAX_CUSTOM_CLOSURES]:
        try:
            result.add(date.fromisoformat(value))
        except ValueError:
            continue
    return frozenset(result)


def _bounded_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(MAX_BUSINESS_DAYS, max(0, parsed))


def _calendar_override(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return min(MAX_CALENDAR_OVERRIDE_DAYS, max(0, parsed))


def _as_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    return str(value).strip().casefold() in {"1", "true", "on", "yes"}


@dataclass(frozen=True)
class ProcurementCalendar:
    delivery_business_days: int = 5
    safety_business_days: int = 2
    observe_us_holidays: bool = True
    custom_closures: frozenset[date] = frozenset()

    @classmethod
    def from_runtime(cls, values: Optional[dict[str, Any]]) -> "ProcurementCalendar":
        rt = values or {}
        return cls(
            delivery_business_days=_bounded_int(
                rt.get("procurement.delivery_business_days"), 5
            ),
            safety_business_days=_bounded_int(
                rt.get("procurement.safety_business_days"), 2
            ),
            observe_us_holidays=_as_bool(
                rt.get("procurement.observe_us_holidays"), True
            ),
            custom_closures=parse_closures(rt.get("procurement.closed_dates")),
        )

    @classmethod
    def load(cls, db: Session) -> "ProcurementCalendar":
        from central.runtime import load_settings

        return cls.from_runtime(load_settings(db))

    @property
    def order_window_business_days(self) -> int:
        return self.delivery_business_days + self.safety_business_days

    def holidays_for(self, years: Iterable[int]) -> set[date]:
        holidays = set(self.custom_closures)
        if self.observe_us_holidays:
            for year in years:
                holidays.update(us_federal_holidays(year))
        return holidays

    def is_business_day(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        return day not in self.holidays_for({day.year - 1, day.year, day.year + 1})

    def add_business_days(self, start: date, business_days: int) -> date:
        day = start
        remaining = max(0, business_days)
        while remaining:
            day += timedelta(days=1)
            if self.is_business_day(day):
                remaining -= 1
        return day

    def estimated_delivery(self, ordered_on: date) -> date:
        return self.add_business_days(ordered_on, self.delivery_business_days)

    def order_window_days(self, today: date) -> int:
        deadline = self.add_business_days(today, self.order_window_business_days)
        return (deadline - today).days


def effective_order_window_days(values: Optional[dict[str, Any]], today: date) -> int:
    """Calendar-day forecast horizon after business policy + optional override."""
    policy = ProcurementCalendar.from_runtime(values)
    return max(
        policy.order_window_days(today),
        _calendar_override((values or {}).get("alerts.reorder_lead_days")),
    )
