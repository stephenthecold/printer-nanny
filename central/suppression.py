"""Decide whether a notification may go out now, wait, or be dropped.

Quiet hours and maintenance windows did not exist before this module (see
``docs/audit-2026-07.md``); every alert notified immediately, at any hour. The
public entry point is :func:`evaluate`, which answers one question for one
notification and returns a :class:`Decision`.

Three things here are easy to get wrong, so they are handled explicitly:

**Wall-clock, not UTC.** "Don't call after 18:00" means the *client's* 18:00. Every
recurring window is evaluated in the scoped client's timezone
(``clients.timezone``, falling back to the global ``alerts.default_timezone``).
Timezone resolution is defensive: an unknown, stale or hand-edited zone name
degrades to UTC rather than raising, because a bad string in one client row must
never take the whole worker down.

**A wrapped window's weekday mask gates its START day.** An 18:00->07:00 window
scoped to Mon-Fri is still active at 03:00 on Saturday -- that is Friday's window
running out. Testing the mask against "today" would end the window at local
midnight, silently un-silencing the small hours, which is exactly the time the
feature exists to protect.

**A deferral must never resolve to the past.** The end instant is what the retry
sweeper wakes on, so an end that computes to <= now would flush immediately in a
tight loop. Every result is asserted forward of ``now`` and clamped to a maximum
horizon, so a misconfigured or DST-skewed window can't hold a notification
indefinitely either.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m

log = logging.getLogger("central.suppression")

MINUTES_PER_DAY = 24 * 60

# A recurring window can be at most 24h long, plus an hour of DST slack. Anything
# beyond that is a misconfiguration, and holding a notification on it would be a
# silent outage; the clamp turns that into a bounded, visible delay instead.
_MAX_QUIET_DEFER = timedelta(hours=26)
# A dated maintenance window may legitimately span days, but not forever. Matches
# the spirit of delivery.UNROUTED_GIVE_UP: bound the wait so nothing is held past
# the point an operator would still want it.
_MAX_MAINTENANCE_DEFER = timedelta(days=7)

_SEVERITY_RANK = {
    m.EventSeverity.info: 0,
    m.EventSeverity.warning: 1,
    m.EventSeverity.critical: 2,
}

# Cache resolved ZoneInfo objects. Zone lookups hit the filesystem, and the
# evaluator runs per notification per cycle.
_ZONE_CACHE: dict = {}


@dataclass
class Decision:
    """What to do with one notification right now.

    Exactly one of the three is true. ``deferred_until`` is set only when
    ``defer`` is, and is always strictly after the ``now`` passed to
    :func:`evaluate`.
    """

    dispatch: bool
    defer: bool
    suppress: bool
    deferred_until: Optional[datetime] = None
    # Human-readable reason, surfaced on the alert badge and the delivery row so
    # an operator can see WHY nothing was sent rather than inferring it.
    reason: str = ""
    window_id: Optional[int] = None

    @classmethod
    def send(cls) -> "Decision":
        return cls(dispatch=True, defer=False, suppress=False)


def _zone(name: Optional[str]):
    """Resolve an IANA zone name to a tzinfo, falling back to UTC.

    Never raises. A missing tz database (slim containers) or a bad name resolves
    to UTC and logs once per distinct name -- being an hour off is a cosmetic
    bug, taking the worker down over a config string is not.
    """
    if not name:
        return timezone.utc
    if name in _ZONE_CACHE:
        return _ZONE_CACHE[name]
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001 - any lookup failure means "use UTC"
        log.warning("unknown timezone %r (%s); falling back to UTC", name, exc)
        # Deliberately NOT cached: invalid names are unbounded (they come from a
        # settings string), so caching them would let bad input grow this dict
        # without limit. Valid zones are capped at ~500 by the tz database.
        return timezone.utc
    _ZONE_CACHE[name] = zone
    return zone


def valid_timezone(name: Optional[str]) -> bool:
    """Is ``name`` a resolvable IANA zone? Used to validate operator input."""
    if not name:
        return True  # blank == inherit the global default
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def client_timezone_name(
    db: Session, printer: Optional[m.Printer], runtime: Optional[dict]
) -> str:
    """The zone name governing ``printer``: its client's, else the global default."""
    default = ((runtime or {}).get("alerts.default_timezone") or "UTC").strip() or "UTC"
    if printer is None or printer.client_id is None:
        return default
    client = db.get(m.Client, printer.client_id)
    if client is not None and (client.timezone or "").strip():
        return client.timezone.strip()
    return default


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; treat them as UTC."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _scope_matches(
    window: m.SuppressionWindow, printer: Optional[m.Printer]
) -> bool:
    """Does this window cover ``printer``? Mirrors channels.registry._scope_matches."""
    if window.scope == m.AlertScope.global_:
        return True
    if printer is None or window.scope_id is None:
        # A scoped window has nothing to match against on an agent-level alert
        # (no printer), so it does not apply. Global windows still do.
        return False
    if window.scope == m.AlertScope.client:
        return printer.client_id == window.scope_id
    if window.scope == m.AlertScope.site:
        return printer.site_id == window.scope_id
    if window.scope == m.AlertScope.printer:
        return printer.id == window.scope_id
    return False


def _local_end_to_utc(local_day: date, end_minute: int, zone) -> datetime:
    """Convert "``end_minute`` on ``local_day``, in ``zone``" to a UTC instant.

    ``end_minute`` may be 1440 (== local midnight ending the day), which
    ``datetime.time`` cannot represent, so it is carried as a day rollover.
    Crossing a DST boundary is left to ``astimezone``; for a nonexistent or
    ambiguous local time the result can be up to an hour off, which is
    acceptable for "when does quiet time end" and is why callers clamp.
    """
    day = local_day
    minute = end_minute
    if minute >= MINUTES_PER_DAY:
        day = day + timedelta(days=1)
        minute -= MINUTES_PER_DAY
    naive = datetime.combine(day, time(hour=minute // 60, minute=minute % 60))
    return naive.replace(tzinfo=zone).astimezone(timezone.utc)


def _weekday_allowed(window: m.SuppressionWindow, day: date) -> bool:
    """Is ``day`` in the window's weekday mask? Empty/NULL mask means every day."""
    mask = window.weekdays
    if not mask:
        return True
    try:
        allowed = {int(d) for d in mask}
    except (TypeError, ValueError):
        # A hand-edited or corrupt mask must not silently match nothing (which
        # would disable the window) nor raise. Treat it as "every day" and say so.
        log.warning("suppression window %s has an unreadable weekday mask %r; "
                    "treating as every day", window.id, mask)
        return True
    return day.weekday() in allowed


def _quiet_hours_end(
    window: m.SuppressionWindow, now: datetime, zone
) -> Optional[datetime]:
    """If ``now`` is inside this recurring window, when does it end (UTC)?

    Returns None when the window is not active. The weekday mask is tested
    against the day the window STARTED, which for a wrapped window in its
    small hours is yesterday -- see the module docstring.
    """
    start = window.start_minute
    end = window.end_minute
    if start is None or end is None:
        return None
    start %= MINUTES_PER_DAY
    # end == start would be a zero-length window read as wrapping to 24h; treat
    # end <= start as a wrap, and a literal equal pair as "all day".
    end = end if 0 <= end <= MINUTES_PER_DAY else end % MINUTES_PER_DAY

    local = now.astimezone(zone)
    local_minute = local.hour * 60 + local.minute
    today = local.date()

    if start < end:
        # Same-day window, e.g. 09:00 -> 17:00.
        if start <= local_minute < end and _weekday_allowed(window, today):
            return _local_end_to_utc(today, end, zone)
        return None

    # Wrapped (or all-day) window, e.g. 18:00 -> 07:00.
    if local_minute >= start:
        # Started today; ends after local midnight, i.e. tomorrow's end_minute.
        if _weekday_allowed(window, today):
            return _local_end_to_utc(today + timedelta(days=1), end, zone)
        return None
    if local_minute < end:
        # We are in the tail of a window that started YESTERDAY. The mask gates
        # the start day, so check yesterday -- not today.
        yesterday = today - timedelta(days=1)
        if _weekday_allowed(window, yesterday):
            return _local_end_to_utc(today, end, zone)
    return None


def _maintenance_end(
    window: m.SuppressionWindow, now: datetime
) -> Optional[datetime]:
    """If ``now`` is inside this dated window, when does it end (UTC)?"""
    starts = _aware(window.starts_at)
    ends = _aware(window.ends_at)
    if starts is None or ends is None or ends <= starts:
        return None
    if starts <= now < ends:
        return ends
    return None


def active_windows(
    db: Session,
    printer: Optional[m.Printer],
    now: datetime,
    *,
    runtime: Optional[dict] = None,
    windows: Optional[Iterable[m.SuppressionWindow]] = None,
) -> List[tuple]:
    """Every enabled window covering ``printer`` that is active at ``now``.

    Returns ``[(window, ends_at_utc), ...]``, unordered. ``windows`` may be
    passed to avoid re-querying when evaluating many notifications in one cycle.

    Note that a *global* recurring window is still evaluated in the scoped
    printer's client timezone, not the server's. That is the useful reading of
    "quiet hours 18:00-07:00, everywhere": each client gets its own local night,
    so one policy covers a fleet spread across zones without an operator cloning
    it per client.
    """
    if windows is None:
        windows = db.scalars(
            select(m.SuppressionWindow).where(m.SuppressionWindow.enabled.is_(True))
        )
    zone = _zone(client_timezone_name(db, printer, runtime))
    out: List[tuple] = []
    for window in windows:
        if not window.enabled or not _scope_matches(window, printer):
            continue
        if window.kind == m.SuppressionKind.maintenance:
            ends = _maintenance_end(window, now)
        else:
            ends = _quiet_hours_end(window, now, zone)
        if ends is not None:
            out.append((window, ends))
    return out


def evaluate(
    db: Session,
    printer: Optional[m.Printer],
    severity: str,
    now: datetime,
    *,
    runtime: Optional[dict] = None,
    windows: Optional[Iterable[m.SuppressionWindow]] = None,
) -> Decision:
    """Should this notification go out now, wait, or be dropped?

    ``severity`` is the alert's severity string ("info"/"warning"/"critical").

    Resolution when several windows overlap is deliberately flat rather than
    scope-ranked, because a ranked scheme implies exclusions ("this printer is
    exempt from the client policy") that this model cannot express -- and a
    precedence rule that silently *reduces* suppression would be the dangerous
    direction to be wrong in. So: every covering window that the severity does
    not break through is considered, and

      1. any ``suppress`` window wins -- it is the more deliberate instruction
         (an operator naming a maintenance window means "expect this noise, bin
         it"), and it is the option that cannot later surprise them with a
         digest of 40 expected alerts;
      2. otherwise the notification defers until the LATEST end among them, so
         it stays quiet until every window that covers it has closed.
    """
    try:
        sev = m.EventSeverity(severity)
    except ValueError:
        sev = m.EventSeverity.warning
    sev_rank = _SEVERITY_RANK.get(sev, 1)

    candidates = []
    for window, ends in active_windows(
        db, printer, now, runtime=runtime, windows=windows
    ):
        if window.allow_breakthrough:
            floor = _SEVERITY_RANK.get(window.min_severity_breakthrough, 2)
            if sev_rank >= floor:
                # Severe enough to ignore this window entirely.
                continue
        # allow_breakthrough=False is total silence: the window applies to every
        # severity, critical included. See SuppressionWindow's docstring for why
        # this is a flag rather than a null floor.
        candidates.append((window, ends))

    if not candidates:
        return Decision.send()

    suppressors = [(w, e) for w, e in candidates if w.action == m.SuppressionAction.suppress]
    if suppressors:
        # Lowest id, so the reason string is stable across cycles rather than
        # depending on row order.
        window = min((w for w, _ in suppressors), key=lambda w: w.id or 0)
        return Decision(
            dispatch=False, defer=False, suppress=True,
            reason="suppressed by window %r" % window.name,
            window_id=window.id,
        )

    window, ends = max(candidates, key=lambda pair: pair[1])
    horizon = (
        _MAX_MAINTENANCE_DEFER
        if window.kind == m.SuppressionKind.maintenance
        else _MAX_QUIET_DEFER
    )
    # Never hand back an end at or before `now`: it is what the retry sweeper
    # wakes on, so a past instant would flush in a tight loop. DST arithmetic and
    # clock skew can both produce one, so this is a guard, not a formality.
    if ends <= now:
        ends = now + timedelta(minutes=1)
    if ends > now + horizon:
        log.warning(
            "suppression window %s would defer past the %s horizon; clamping",
            window.id, horizon,
        )
        ends = now + horizon
    return Decision(
        dispatch=False, defer=True, suppress=False, deferred_until=ends,
        reason="held by window %r until %s" % (window.name, ends.isoformat(timespec="minutes")),
        window_id=window.id,
    )
