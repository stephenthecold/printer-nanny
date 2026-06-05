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
from central.channels import Notification, dispatch
from central.runtime import load_settings

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
def _channels_for_rule(db: Session, rule: m.AlertRule) -> list[m.NotificationChannel]:
    if rule.channel_ids:
        rows = [db.get(m.NotificationChannel, cid) for cid in rule.channel_ids]
        return [r for r in rows if r is not None and r.enabled]
    # Fall back to all enabled global channels.
    return list(
        db.scalars(
            select(m.NotificationChannel).where(
                m.NotificationChannel.enabled.is_(True),
                m.NotificationChannel.scope == m.AlertScope.global_,
            )
        )
    )


def _printer_label(printer: m.Printer) -> str:
    name = printer.model or printer.hostname or "printer"
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


def _find_open_alert(db: Session, dedupe_key: str) -> Optional[m.Alert]:
    return db.scalar(
        select(m.Alert).where(
            m.Alert.dedupe_key == dedupe_key, m.Alert.state == m.AlertState.open
        )
    )


def _open_alert(
    db: Session,
    rule: m.AlertRule,
    dedupe_key: str,
    title: str,
    detail: str,
    *,
    printer: Optional[m.Printer] = None,
    agent: Optional[m.Agent] = None,
    runtime: Optional[dict] = None,
) -> Optional[m.Alert]:
    """Open an alert if one isn't already open for this dedupe_key. Returns it (or None)."""
    if _find_open_alert(db, dedupe_key) is not None:
        return None
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
    )
    db.add(alert)
    db.flush()  # assign alert.id

    note = Notification(
        title=title,
        body=detail,
        severity=rule.severity.value,
        printer_label=_printer_label(printer) if printer else None,
        alert_id=alert.id,
    )
    results = dispatch(note, _channels_for_rule(db, rule), runtime)
    alert.notified_channels = [
        {"channel": name, "ok": res.ok, "detail": res.detail} for name, res in results
    ]
    return alert


def _resolve_stale(db: Session, active_keys: set[str], rule_ids: set[int]) -> int:
    """Resolve open alerts whose condition no longer holds (key not seen this run)."""
    resolved = 0
    stmt = select(m.Alert).where(m.Alert.state == m.AlertState.open)
    for alert in db.scalars(stmt):
        if alert.rule_id in rule_ids and alert.dedupe_key not in active_keys:
            alert.state = m.AlertState.resolved
            alert.resolved_at = _now()
            resolved += 1
    return resolved


def evaluate_alerts(db: Session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    rules = list(db.scalars(select(m.AlertRule).where(m.AlertRule.enabled.is_(True))))
    runtime = load_settings(db)
    opened = 0
    active_keys: set[str] = set()
    rule_ids = {r.id for r in rules}

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
                    if _open_alert(db, rule, key, title, detail, agent=agent, runtime=runtime):
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
                        if _open_alert(db, rule, key, title, detail, printer=printer, runtime=runtime):
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
                    if _open_alert(db, rule, key, title, detail, printer=printer, runtime=runtime):
                        opened += 1

    resolved = _resolve_stale(db, active_keys, rule_ids)
    db.commit()
    return {"alerts_opened": opened, "alerts_resolved": resolved}


# --------------------------------------------------------------------------- #
# Maintenance due
# --------------------------------------------------------------------------- #
def check_maintenance_due(db: Session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    due = 0
    for sched in queries.maintenance_due(db, now):
        printer = db.get(m.Printer, sched.printer_id) if sched.printer_id else None
        # Page-threshold rules also gate on page count when set.
        if sched.page_threshold and printer and printer.page_count is not None:
            if printer.page_count < sched.page_threshold:
                continue
        due += 1
    db.commit()
    return {"maintenance_due": due}


# --------------------------------------------------------------------------- #
# Supply-depletion forecast (days-to-empty from the recent slope)
# --------------------------------------------------------------------------- #
def forecast_days_to_empty(readings: list[tuple[datetime, float]]) -> Optional[float]:
    """Linear extrapolation of level→0 from (ts, level_pct) points. None if rising/flat."""
    points = [(t, lvl) for t, lvl in readings if lvl is not None]
    if len(points) < 2:
        return None
    points.sort(key=lambda p: p[0])
    (t0, l0), (t1, l1) = points[0], points[-1]
    days = (t1 - t0).total_seconds() / 86400.0
    if days <= 0:
        return None
    rate = (l0 - l1) / days  # percent consumed per day
    if rate <= 0:
        return None  # not depleting
    return round(l1 / rate, 1)


def forecast_supplies(db: Session) -> dict:
    """Annotate supplies with a days-to-empty estimate from their reading history."""
    flagged = 0
    for printer in db.scalars(
        select(m.Printer).where(m.Printer.discovery_state == m.DiscoveryState.approved)
    ):
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
            dte = forecast_days_to_empty(pts)
            if dte is not None and dte <= 14:
                flagged += 1
    return {"supplies_forecast_low": flagged}
