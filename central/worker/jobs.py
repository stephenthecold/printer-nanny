"""Background jobs: heartbeat/offline detection, alert evaluation, maintenance,
and supply-depletion forecasting. Each function is independently runnable and
returns a small summary dict so the worker loop (and tests) can assert on it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import queries
from central.channels import (
    Notification,
    routable_channels,
    route_channels,
)
from central.channels.delivery import record_dispatch
from central.channels.delivery import retry_due as _retry_due
from central.channels.freescout import FreeScoutChannel
from central.runtime import load_settings

# Alert states that still represent an outstanding condition. An ACKNOWLEDGED
# alert is "seen but not fixed", so a cleared condition must resolve it too --
# otherwise an ack'd alert whose condition clears is stuck open forever.
_LIVE_STATES = (m.AlertState.open, m.AlertState.acknowledged)

_SEVERITY_RANK = {
    m.EventSeverity.info: 0,
    m.EventSeverity.warning: 1,
    m.EventSeverity.critical: 2,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; treat them as UTC for comparisons."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# Heartbeat / offline detection
# --------------------------------------------------------------------------- #
def mark_offline_agents(db: Session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    grace_seconds = load_settings(db).get("alerts.offline_grace_seconds", 300)
    grace = timedelta(seconds=grace_seconds)
    changed = 0
    for agent in db.scalars(select(m.Agent)):
        last = _aware(agent.last_heartbeat)
        is_offline = last is None or (now - last) > grace
        new_status = (
            m.AgentStatus.offline
            if is_offline and last is not None
            else m.AgentStatus.never_seen
            if last is None
            else m.AgentStatus.online
        )
        if new_status != agent.status:
            agent.status = new_status
            changed += 1
    db.commit()
    return {"agents_updated": changed}


# --------------------------------------------------------------------------- #
# Alert evaluation
# --------------------------------------------------------------------------- #
def _printer_label(printer: m.Printer) -> str:
    name = printer.display_name or printer.model or printer.hostname or "printer"
    return f"{name} @ {printer.ip}"


def _printers_in_scope(db: Session, rule: m.AlertRule):
    stmt = select(m.Printer).where(m.Printer.discovery_state == m.DiscoveryState.approved)
    if rule.scope == m.AlertScope.client and rule.scope_id:
        stmt = stmt.where(m.Printer.client_id == rule.scope_id)
    elif rule.scope == m.AlertScope.site and rule.scope_id:
        stmt = stmt.where(m.Printer.site_id == rule.scope_id)
    elif rule.scope == m.AlertScope.printer and rule.scope_id:
        stmt = stmt.where(m.Printer.id == rule.scope_id)
    return db.scalars(stmt)


def _external_ref_from(results, channels) -> Optional[str]:
    """Pull the FreeScout ticket id out of a dispatch result set.

    ``dispatch`` returns ``(channel_name, ChannelResult)`` tuples; the FreeScout
    channel sets ``ChannelResult.external_ref`` to the new conversation id. Match
    the result back to its channel by name so only a real FreeScout ticket id is
    persisted (a dry-run create returns no external_ref, so nothing is stored).
    """
    fs_names = {c.name for c in channels if isinstance(c, FreeScoutChannel)}
    for name, res in results:
        if name in fs_names and res.ok and res.external_ref:
            return res.external_ref
    return None


def _find_open_alert(db: Session, dedupe_key: str) -> Optional[m.Alert]:
    """Find a still-live (open OR acknowledged) alert for this dedupe key.

    Acknowledged is included so acking an alert doesn't make the worker re-open
    a fresh duplicate on the next cycle while the condition still holds.
    """
    return db.scalar(
        select(m.Alert).where(
            m.Alert.dedupe_key == dedupe_key,
            m.Alert.state.in_(_LIVE_STATES),
        )
    )


def _notify_alert(
    db: Session,
    alert: m.Alert,
    *,
    rule: Optional[m.AlertRule],
    printer: Optional[m.Printer],
    candidates: list,
    now: datetime,
    runtime: Optional[dict] = None,
) -> None:
    """Build the notification, route it, deliver durably, and stamp bookkeeping.

    Routing honors ``rule.channel_ids`` and per-channel scope/severity (see
    central.channels.route_channels); the routed channels are then delivered
    through ``record_dispatch`` so a transient channel outage is retried by
    retry_deliveries instead of being silently dropped. Captures the FreeScout
    ticket id on ``external_ref`` and sets ``last_notified_at`` so the escalation
    pass can measure how long an alert has gone un-escalated.
    """
    client_name = site_name = None
    if printer is not None:
        client = db.get(m.Client, printer.client_id)
        site = db.get(m.Site, printer.site_id)
        client_name = client.name if client else None
        site_name = site.name if site else None

    note = Notification(
        title=alert.title,
        body=alert.detail or "",
        severity=alert.severity.value,
        client_name=client_name,
        site_name=site_name,
        printer_label=_printer_label(printer) if printer else None,
        alert_id=alert.id,
    )
    channels = route_channels(
        candidates, rule=rule, printer=printer, severity=alert.severity.value
    )
    results = record_dispatch(
        db, alert.id, note, channels, runtime=runtime or load_settings(db)
    )
    alert.notified_channels = [
        {"channel": name, "ok": res.ok, "detail": res.detail} for name, res in results
    ]
    alert.external_ref = _external_ref_from(results, channels)
    alert.last_notified_at = now


def _open_alert(
    db: Session,
    rule: m.AlertRule,
    dedupe_key: str,
    title: str,
    detail: str,
    *,
    printer: Optional[m.Printer] = None,
    agent: Optional[m.Agent] = None,
    candidates: Optional[list] = None,
    now: Optional[datetime] = None,
    runtime: Optional[dict] = None,
) -> Optional[m.Alert]:
    """Open an alert if one isn't already live for this dedupe_key. Returns it (or None)."""
    if _find_open_alert(db, dedupe_key) is not None:
        return None
    now = now or _now()
    alert = m.Alert(
        rule_id=rule.id,
        printer_id=printer.id if printer else None,
        agent_id=agent.id if agent else None,
        type=rule.condition_type,
        severity=rule.severity,
        state=m.AlertState.open,
        title=title,
        detail=detail,
        dedupe_key=dedupe_key,
        escalation_level=0,
    )
    db.add(alert)
    db.flush()  # assign alert.id
    _notify_alert(
        db,
        alert,
        rule=rule,
        printer=printer,
        candidates=candidates or [],
        now=now,
        runtime=runtime,
    )
    return alert


def _freescout_channel(channels) -> Optional[FreeScoutChannel]:
    for c in channels or []:
        if isinstance(c, FreeScoutChannel):
            return c
    return None


def _close_ticket_for(alert: m.Alert, channels) -> Optional[dict]:
    """Close the FreeScout ticket tied to ``alert`` when it auto-resolves.

    No-op (returns None) when the alert never opened a ticket. Returns a small
    record describing the close call so callers can stash it and tests can
    assert exactly-once. Errors are swallowed into the record -- a flaky
    FreeScout must never block the worker from marking the alert resolved.
    """
    ref = alert.external_ref
    if not ref:
        return None
    ch = _freescout_channel(channels)
    if ch is None:
        return None
    note = f"Auto-resolved by Printer Nanny: {alert.title}"
    try:
        res = ch.close_ticket(ref, note)
        return {"external_ref": ref, "ok": res.ok, "detail": res.detail}
    except Exception as exc:  # noqa: BLE001 - closing a ticket must never break resolve
        return {"external_ref": ref, "ok": False, "detail": f"unhandled: {exc}"}


def _resolve_stale(db: Session, active_keys: set[str], channels=None) -> int:
    """Resolve live rule-driven alerts whose condition no longer holds this run.

    Covers both cleared conditions (key not re-added) and alerts orphaned by a
    rule that was disabled/deleted (its key is never re-added). Resolves alerts
    in BOTH open and acknowledged states -- an ack'd alert whose condition clears
    must still auto-resolve, otherwise it's stuck open forever. Maintenance and
    predicted-depletion alerts own their own lifecycle (check_maintenance_due /
    forecast_supplies) and are skipped; a resolved alert that opened a FreeScout
    ticket gets it auto-closed.
    """
    resolved = 0
    stmt = select(m.Alert).where(m.Alert.state.in_(_LIVE_STATES))
    for alert in db.scalars(stmt):
        if alert.type in (
            m.AlertConditionType.maintenance_due,
            m.AlertConditionType.predicted_depletion,
        ):
            continue
        if alert.dedupe_key not in active_keys:
            alert.state = m.AlertState.resolved
            alert.resolved_at = _now()
            _close_ticket_for(alert, channels)
            resolved += 1
    return resolved


def evaluate_alerts(db: Session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    rules = list(db.scalars(select(m.AlertRule).where(m.AlertRule.enabled.is_(True))))
    runtime = load_settings(db)
    candidates = routable_channels(db, runtime)
    opened = 0
    active_keys: set[str] = set()

    for rule in rules:
        if rule.condition_type == m.AlertConditionType.offline_minutes:
            limit = timedelta(minutes=rule.threshold or 0)
            for agent in db.scalars(select(m.Agent)):
                last = _aware(agent.last_heartbeat)
                if last is None or (now - last) >= limit:
                    key = f"rule:{rule.id}:agent:{agent.id}:offline"
                    active_keys.add(key)
                    title = f"Agent offline: {agent.name}"
                    detail = (
                        f"No heartbeat for over {rule.threshold} min "
                        f"(last: {last.isoformat() if last else 'never'})."
                    )
                    if _open_alert(db, rule, key, title, detail, agent=agent,
                                   candidates=candidates, now=now, runtime=runtime):
                        opened += 1
            continue

        for printer in _printers_in_scope(db, rule):
            if rule.condition_type == m.AlertConditionType.supply_below:
                threshold = rule.threshold or 0
                for supply in printer.supplies:
                    if supply.level_pct is not None and supply.level_pct <= threshold:
                        key = f"rule:{rule.id}:printer:{printer.id}:supply:{supply.id}"
                        active_keys.add(key)
                        label = supply.color or supply.type.value
                        title = f"Low {label} on {_printer_label(printer)}"
                        detail = f"{label} at {supply.level_pct:.0f}% (threshold {threshold:.0f}%)."
                        if _open_alert(db, rule, key, title, detail, printer=printer,
                                       candidates=candidates, now=now, runtime=runtime):
                            opened += 1

            elif rule.condition_type == m.AlertConditionType.error_severity:
                min_rank = _SEVERITY_RANK.get(rule.severity, 1)
                unresolved = [
                    e
                    for e in printer.events
                    if e.resolved_at is None and _SEVERITY_RANK.get(e.severity, 0) >= min_rank
                ]
                if unresolved:
                    latest = max(unresolved, key=lambda e: e.ts)
                    key = f"rule:{rule.id}:printer:{printer.id}:error:{latest.code or 'event'}"
                    active_keys.add(key)
                    title = f"Error on {_printer_label(printer)}"
                    detail = f"{latest.severity.value}: {latest.message}"
                    if _open_alert(db, rule, key, title, detail, printer=printer,
                                   candidates=candidates, now=now, runtime=runtime):
                        opened += 1

    # Pass the unwrapped candidate channels so a resolved alert's FreeScout
    # ticket can be auto-closed (route metadata isn't needed to close a ticket).
    close_channels = [rc.channel for rc in candidates]
    resolved = _resolve_stale(db, active_keys, close_channels)
    # Flush the resolutions so the escalation query (run on a session with
    # autoflush off) doesn't re-notify alerts we just resolved this same pass.
    db.flush()
    escalated = _escalate_alerts(db, runtime, candidates, now)
    db.commit()
    return {
        "alerts_opened": opened,
        "alerts_resolved": resolved,
        "alerts_escalated": escalated,
    }


def _escalate_alerts(
    db: Session, runtime: dict, candidates: list, now: datetime
) -> int:
    """Re-notify still-live alerts that have gone unresolved past the window.

    Controlled by ``alerts.escalate_after_minutes`` (0 = off). For each live
    (open or acknowledged) alert whose last notification is older than the
    window, re-dispatch through the same routing, bump ``escalation_level``,
    and stamp ``last_notified_at`` so the next escalation is measured from now.

    This is a deliberate re-send, distinct from dedupe suppression: dedupe stops
    a *duplicate OPEN* of the same condition, while escalation re-notifies an
    *already-open* alert that nobody has resolved.
    """
    minutes = int(runtime.get("alerts.escalate_after_minutes", 0) or 0)
    if minutes <= 0:
        return 0
    window = timedelta(minutes=minutes)
    escalated = 0
    for alert in db.scalars(select(m.Alert).where(m.Alert.state.in_(_LIVE_STATES))):
        # Baseline: an alert never (re)notified uses its creation time.
        baseline = _aware(alert.last_notified_at) or _aware(alert.created_at)
        if baseline is not None and (now - baseline) < window:
            continue
        rule = db.get(m.AlertRule, alert.rule_id) if alert.rule_id else None
        printer = db.get(m.Printer, alert.printer_id) if alert.printer_id else None
        alert.escalation_level = (alert.escalation_level or 0) + 1
        _notify_alert(
            db, alert, rule=rule, printer=printer,
            candidates=candidates, now=now, runtime=runtime,
        )
        escalated += 1
    return escalated


# --------------------------------------------------------------------------- #
# Component-life maintenance — match a schedule's component_type onto the
# component-life Supply rows the Brother provider writes (belt/fuser/laser/
# drum/PF-kit). See central.models.MaintenanceSchedule.COMPONENT_TYPES and the
# agent's brother_maintenance._EXTRA_PART_ROWS for the type/color labelling.
# --------------------------------------------------------------------------- #
def _component_supply_matches(supply: m.Supply, component_type: str) -> bool:
    """True when ``supply`` is the component-life row for ``component_type``."""
    if component_type == "fuser":
        return supply.type == m.SupplyType.fuser
    if component_type == "drum":
        return supply.type == m.SupplyType.drum
    if component_type == "belt":
        return supply.type == m.SupplyType.other and supply.color == "belt"
    if component_type == "laser":
        return supply.type == m.SupplyType.other and supply.color == "laser"
    if component_type == "pf_kit":
        return supply.type == m.SupplyType.other and supply.color in ("pf-kit-mp", "pf-kit-1")
    return False


def _component_schedule_printers(db: Session, sched: m.MaintenanceSchedule):
    """Approved printers a component schedule applies to (specific / model / fleet)."""
    stmt = select(m.Printer).where(m.Printer.discovery_state == m.DiscoveryState.approved)
    if sched.printer_id:
        stmt = stmt.where(m.Printer.id == sched.printer_id)
    elif sched.model:
        stmt = stmt.where(m.Printer.model.ilike(f"%{sched.model}%"))
    return db.scalars(stmt)


# --------------------------------------------------------------------------- #
# Maintenance due — schedule-driven (no alert_rule needed), with its own
# open/dispatch/resolve lifecycle so a due schedule actually notifies, and the
# alert clears once the schedule's next_due is rolled forward (e.g. service logged).
# --------------------------------------------------------------------------- #
def check_maintenance_due(db: Session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    runtime = load_settings(db)
    candidates = routable_channels(db, runtime)
    opened = 0
    active_keys: set[str] = set()

    def _open_maintenance_alert(key, title, detail, printer):
        """Open a maintenance-due alert for ``key`` unless one's already live.

        Routes by the printer's scope (no AlertRule), delivers durably, captures
        the FreeScout ticket id, and stamps escalation bookkeeping via
        ``_notify_alert`` -- same path as rule-driven alerts.
        """
        if _find_open_alert(db, key) is not None:
            return False
        alert = m.Alert(
            rule_id=None,
            printer_id=printer.id if printer else None,
            type=m.AlertConditionType.maintenance_due,
            severity=m.EventSeverity.warning,
            state=m.AlertState.open,
            title=title,
            detail=detail,
            dedupe_key=key,
            escalation_level=0,
        )
        db.add(alert)
        db.flush()
        _notify_alert(
            db, alert, rule=None, printer=printer,
            candidates=candidates, now=now, runtime=runtime,
        )
        return True

    for sched in queries.maintenance_due(db, now):
        printer = db.get(m.Printer, sched.printer_id) if sched.printer_id else None
        # Page-threshold schedules also require the page count to be reached.
        if sched.page_threshold and printer and printer.page_count is not None:
            if printer.page_count < sched.page_threshold:
                continue
        due_str = sched.next_due.date().isoformat() if sched.next_due else "due"
        key = f"maintenance:{sched.id}:{due_str}"
        active_keys.add(key)
        label = _printer_label(printer) if printer else (sched.model or "fleet")
        if _open_maintenance_alert(
            key,
            f"Maintenance due: {sched.name} ({label})",
            f"'{sched.name}' is due as of {due_str}.",
            printer,
        ):
            opened += 1

    # Component-life schedules: open when a matching component-life Supply row
    # has dropped to (or below) the schedule's life_threshold percent. Dedupe
    # per (schedule, printer); the key drops out — and the alert auto-resolves
    # below — once the part is serviced and its % climbs back above threshold.
    for sched in queries.component_maintenance_schedules(db):
        ctype = sched.component_type
        threshold = sched.life_threshold
        if ctype is None or threshold is None:
            continue
        for printer in _component_schedule_printers(db, sched):
            low = [
                s
                for s in printer.supplies
                if _component_supply_matches(s, ctype)
                and s.level_pct is not None
                and s.level_pct <= threshold
            ]
            if not low:
                continue
            worst = min(low, key=lambda s: s.level_pct)
            key = f"maintenance:component:{sched.id}:printer:{printer.id}:{ctype}"
            active_keys.add(key)
            part = (worst.description or ctype).strip()
            if _open_maintenance_alert(
                key,
                f"Maintenance due: {sched.name} ({_printer_label(printer)})",
                f"{part} life at {worst.level_pct:.0f}% "
                f"(threshold {threshold:.0f}%) for '{sched.name}'.",
                printer,
            ):
                opened += 1

    # Resolve maintenance alerts whose schedule is no longer due (next_due rolled
    # forward, or a component's life climbed back above threshold / was serviced).
    # Resolve in both open AND acknowledged states so an ack'd maintenance alert
    # clears once serviced.
    close_channels = [rc.channel for rc in candidates]
    resolved = 0
    for alert in db.scalars(
        select(m.Alert).where(
            m.Alert.state.in_(_LIVE_STATES),
            m.Alert.type == m.AlertConditionType.maintenance_due,
        )
    ):
        if alert.dedupe_key not in active_keys:
            alert.state = m.AlertState.resolved
            alert.resolved_at = now
            _close_ticket_for(alert, close_channels)
            resolved += 1

    db.commit()
    return {"maintenance_opened": opened, "maintenance_resolved": resolved}


# --------------------------------------------------------------------------- #
# Supply-depletion forecast (days-to-empty from a regression over recent levels)
# --------------------------------------------------------------------------- #
# Confidence gate: below these the consumption slope is too noisy to trust, so
# forecast_days_to_empty returns None rather than a number nobody should reorder
# against. Two points over a few days is the floor the older two-point estimate
# implicitly assumed; the regression keeps that floor while smoothing the rest.
FORECAST_MIN_POINTS = 2          # need at least a baseline + a follow-up reading
FORECAST_MIN_HISTORY_DAYS = 3.0  # ...spanning at least this long (matches RUNWAY_MIN_HISTORY_DAYS)


def forecast_days_to_empty(
    readings: list[tuple[datetime, float]], refill_tolerance: float = 5.0
) -> Optional[float]:
    """Days until level→0, from a least-squares fit over the recent depleting segment.

    Replaces the old first-point/last-point slope (which was maximally
    noise-sensitive) with an ordinary least-squares regression of level_pct on
    time across every point in the segment, so a single jittery reading no
    longer swings the estimate. The refill/cartridge-swap handling is preserved:
    a jump up of more than ``refill_tolerance`` points is treated as a fresh
    cartridge and resets the baseline, so a spent cartridge's slope isn't
    averaged against the new one.

    Returns ``None`` (the existing "no estimate" contract shared with
    ``central.queries.supply_runway``) when the series is rising/flat, or when
    the surviving segment doesn't clear the confidence gate
    (``FORECAST_MIN_POINTS`` points spanning ``FORECAST_MIN_HISTORY_DAYS`` days).
    The number returned is days-to-empty measured from the most recent reading,
    using the regression-predicted level there (== the observed level for a
    clean linear series, so legacy expectations are unchanged).
    """
    points = sorted([(t, lvl) for t, lvl in readings if lvl is not None], key=lambda p: p[0])
    if len(points) < FORECAST_MIN_POINTS:
        return None
    start = 0
    for i in range(1, len(points)):
        if points[i][1] > points[i - 1][1] + refill_tolerance:
            start = i  # refill detected — baseline resets here
    seg = points[start:]
    if len(seg) < FORECAST_MIN_POINTS:
        return None

    t0 = seg[0][0]
    # x in days since the segment's first reading; y in percent remaining.
    xs = [(t - t0).total_seconds() / 86400.0 for t, _ in seg]
    ys = [lvl for _, lvl in seg]
    span = xs[-1] - xs[0]
    if span < FORECAST_MIN_HISTORY_DAYS:
        return None  # not enough elapsed history to trust the slope yet

    n = len(seg)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0:
        return None  # all readings at the same instant — no slope
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov_xy / var_x  # percent change per day (negative when depleting)
    rate = -slope  # percent consumed per day
    if rate <= 0:
        return None  # not depleting (rising or flat fit)

    intercept = mean_y - slope * mean_x
    level_now = intercept + slope * xs[-1]  # fitted level at the latest reading
    if level_now <= 0:
        return 0.0  # already projected empty
    return round(level_now / rate, 1)


def forecast_supplies(db: Session, now: Optional[datetime] = None) -> dict:
    """Forecast each supply's days-to-empty, persist it, and raise reorder alerts.

    Three things happen per approved printer, per ``(type, color)`` supply:

      1. Fit ``forecast_days_to_empty`` over the supply's reading history.
      2. Persist the result onto the matching ``Supply`` row
         (``days_to_empty`` + ``forecast_at``) so dashboards/portal/reports read
         it instead of re-fitting on every render. A supply with no trustworthy
         estimate is cleared back to ``None``.
      3. If the estimate is at/under the operator's reorder lead-time
         (``alerts.reorder_lead_days``), open a ``predicted_depletion`` alert
         deduped PER (printer, supply) — not per printer, so a color device
         with three depleting toners raises three actionable alerts instead of
         one storm-prone aggregate. The dedupe / auto-resolve machinery is the
         same scaffolding the rule engine uses: keys re-added this pass stay
         open, keys that drop out (estimate recovered, or the cartridge was
         swapped/refilled so the recent segment no longer projects empty) are
         resolved.
    """
    now = now or _now()
    runtime = load_settings(db)
    candidates = routable_channels(db, runtime)
    lead_days = runtime.get("alerts.reorder_lead_days", 14)

    flagged = 0
    forecasted = 0
    opened = 0
    active_keys: set[str] = set()

    for printer in db.scalars(
        select(m.Printer).where(m.Printer.discovery_state == m.DiscoveryState.approved)
    ):
        # Index this printer's Supply rows by (type, color) so a forecast keyed
        # off the snapshot history lands on the right cartridge.
        supplies_by_key: dict[str, m.Supply] = {}
        for supply in printer.supplies:
            supplies_by_key[f"{supply.type.value}:{supply.color}"] = supply

        # Build per-(type,color) level series from supply_snapshot history.
        series: dict[str, list[tuple[datetime, float]]] = {}
        for r in db.scalars(
            select(m.Reading)
            .where(m.Reading.printer_id == printer.id, m.Reading.supply_snapshot.is_not(None))
            .order_by(m.Reading.ts.asc())
        ):
            for snap in r.supply_snapshot or []:
                lvl = snap.get("level_pct")
                if lvl is None:
                    continue
                key = f"{snap.get('type')}:{snap.get('color')}"
                series.setdefault(key, []).append((_aware(r.ts), float(lvl)))

        for key, pts in series.items():
            supply = supplies_by_key.get(key)
            dte = forecast_days_to_empty(pts)
            # Persist onto the supply row (None clears a stale estimate).
            if supply is not None:
                supply.days_to_empty = dte
                supply.forecast_at = now if dte is not None else None
                if dte is not None:
                    forecasted += 1
            if dte is None or dte > lead_days:
                continue
            flagged += 1
            if supply is None:
                continue  # snapshot for a cartridge we no longer track — nothing to alert on
            dedupe_key = f"forecast:printer:{printer.id}:supply:{supply.id}"
            active_keys.add(dedupe_key)
            label = supply.color or supply.type.value
            title = f"Reorder {label} for {_printer_label(printer)}"
            detail = (
                f"{label} is forecast to run out in ~{dte:.0f} day(s) "
                f"(reorder lead time {lead_days} day(s))."
            )
            if _open_forecast_alert(
                db, dedupe_key, title, detail, printer=printer,
                candidates=candidates, now=now, runtime=runtime,
            ):
                opened += 1

    close_channels = [rc.channel for rc in candidates]
    resolved = _resolve_stale_forecasts(db, active_keys, now, close_channels)
    db.commit()
    # ``supplies_forecast_low`` keeps its historical meaning (count at/under the
    # lead-time threshold) so existing callers/tests reading that key still work.
    return {
        "supplies_forecast_low": flagged,
        "supplies_forecasted": forecasted,
        "forecast_alerts_opened": opened,
        "forecast_alerts_resolved": resolved,
    }


# --------------------------------------------------------------------------- #
# Notification delivery retry / dead-letter
# --------------------------------------------------------------------------- #
def retry_deliveries(db: Session, now: Optional[datetime] = None) -> dict:
    """Re-send due failed/pending notification deliveries with exponential backoff.

    A channel send that failed when its alert opened was persisted as a
    NotificationDelivery row; this job re-sends it once its backoff window has
    elapsed, marks it delivered on success, and dead-letters it after the
    configured max-attempts cap. Idempotent and safe to run every cycle --
    delivered/dead rows are terminal and never re-sent (see channels.delivery).
    """
    return _retry_due(db, load_settings(db), now or _now())


def _open_forecast_alert(
    db: Session,
    dedupe_key: str,
    title: str,
    detail: str,
    *,
    printer: m.Printer,
    candidates: Optional[list] = None,
    now: Optional[datetime] = None,
    runtime: Optional[dict] = None,
) -> Optional[m.Alert]:
    """Open a predicted-depletion alert if one isn't already live for the key.

    Rule-less (no AlertRule), so it routes by the printer's scope and delivers
    durably via ``_notify_alert`` -- the same path as rule-driven and maintenance
    alerts (per-tenant routing, retry/dead-letter, FreeScout ticket capture, and
    escalation bookkeeping). Returns the new alert, or ``None`` if one was open.
    """
    if _find_open_alert(db, dedupe_key) is not None:
        return None
    now = now or _now()
    alert = m.Alert(
        rule_id=None,
        printer_id=printer.id,
        type=m.AlertConditionType.predicted_depletion,
        severity=m.EventSeverity.warning,
        state=m.AlertState.open,
        title=title,
        detail=detail,
        dedupe_key=dedupe_key,
        escalation_level=0,
    )
    db.add(alert)
    db.flush()  # assign alert.id
    _notify_alert(
        db, alert, rule=None, printer=printer,
        candidates=candidates or [], now=now, runtime=runtime,
    )
    return alert


def _resolve_stale_forecasts(
    db: Session, active_keys: set[str], now: datetime, channels=None
) -> int:
    """Resolve open predicted-depletion alerts whose forecast no longer holds.

    A key drops out of ``active_keys`` when the supply recovered above the
    lead-time (or the cartridge was swapped/refilled, so the refill-aware fit no
    longer projects it empty within the window). Scoped to forecast alerts so it
    can't touch rule-driven or maintenance alerts, which own their own lifecycle.
    A resolved alert that opened a FreeScout ticket gets it auto-closed too.
    """
    resolved = 0
    for alert in db.scalars(
        select(m.Alert).where(
            m.Alert.state == m.AlertState.open,
            m.Alert.type == m.AlertConditionType.predicted_depletion,
        )
    ):
        if alert.dedupe_key not in active_keys:
            _close_ticket_for(alert, channels)
            alert.state = m.AlertState.resolved
            alert.resolved_at = now
            resolved += 1
    return resolved
