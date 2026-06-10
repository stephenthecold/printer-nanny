"""Read-only aggregate queries shared by the reporting API and the dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from central import models as m

DEFAULT_LOW_SUPPLY_PCT = 20.0


def fleet_summary(db: Session, client_id: Optional[int] = None) -> dict:
    """Counts of printers by status, plus agent and alert tallies."""
    stmt = select(m.Printer.status, func.count()).where(
        m.Printer.discovery_state == m.DiscoveryState.approved
    )
    if client_id is not None:
        stmt = stmt.where(m.Printer.client_id == client_id)
    stmt = stmt.group_by(m.Printer.status)
    by_status = {status.value: 0 for status in m.PrinterStatus}
    total = 0
    for status, count in db.execute(stmt):
        by_status[status.value] = count
        total += count

    pending = db.scalar(
        select(func.count())
        .select_from(m.Printer)
        .where(m.Printer.discovery_state == m.DiscoveryState.pending)
    )
    open_alerts = db.scalar(
        select(func.count()).select_from(m.Alert).where(m.Alert.state == m.AlertState.open)
    )
    agents_offline = db.scalar(
        select(func.count()).select_from(m.Agent).where(m.Agent.status == m.AgentStatus.offline)
    )
    return {
        "total_printers": total,
        "by_status": by_status,
        "pending_discovery": pending or 0,
        "open_alerts": open_alerts or 0,
        "agents_offline": agents_offline or 0,
    }


def low_supplies(db: Session, threshold: float = DEFAULT_LOW_SUPPLY_PCT) -> list[m.Supply]:
    """Supplies at or below the threshold percentage, lowest first."""
    return list(
        db.scalars(
            select(m.Supply)
            .join(m.Printer, m.Supply.printer_id == m.Printer.id)
            .where(
                m.Supply.level_pct.is_not(None),
                m.Supply.level_pct <= threshold,
                m.Printer.discovery_state == m.DiscoveryState.approved,
            )
            .order_by(m.Supply.level_pct.asc())
        )
    )


# Days of polling history a printer needs before the consumption slope is
# trustworthy enough to display. Below this, the UI shows "est. in ~Nd".
RUNWAY_MIN_HISTORY_DAYS = 3.0


def supply_runway(db: Session, printer_ids: list[int]) -> dict:
    """Per-printer supply-depletion forecast for fleet listings.

    Returns {printer_id: {"days": float|None, "history_days": float|None}}:

      days          minimum days-to-empty across the printer's supplies
                    (refill-aware linear extrapolation, same math as the
                    worker's forecast job); None when not yet computable or
                    nothing is depleting.
      history_days  age of the oldest supply snapshot we hold -- lets the UI
                    say "estimate available in ~N days" while history builds
                    instead of an unexplained dash, and "stable" when there
                    IS enough history but no measurable depletion.

    Caps history at the most recent 60 snapshot readings per printer so a
    long-lived fleet page stays cheap.
    """
    from central.worker.jobs import forecast_days_to_empty  # lazy: avoid cycle

    now = datetime.now(timezone.utc)
    out: dict = {}
    for pid in printer_ids:
        rows = list(
            db.scalars(
                select(m.Reading)
                .where(m.Reading.printer_id == pid, m.Reading.supply_snapshot.is_not(None))
                .order_by(m.Reading.ts.desc())
                .limit(60)
            )
        )
        series: dict = {}
        oldest_ts = None
        for r in rows:
            ts = r.ts if r.ts.tzinfo else r.ts.replace(tzinfo=timezone.utc)
            for snap in r.supply_snapshot or []:
                lvl = snap.get("level_pct")
                if lvl is None:
                    continue
                key = f"{snap.get('type')}:{snap.get('color')}"
                series.setdefault(key, []).append((ts, float(lvl)))
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
        days = [
            d for d in (forecast_days_to_empty(points) for points in series.values())
            if d is not None
        ]
        out[pid] = {
            "days": min(days) if days else None,
            "history_days": (
                (now - oldest_ts).total_seconds() / 86400.0 if oldest_ts else None
            ),
        }
    return out


def recent_errors(db: Session, limit: int = 50) -> list[m.PrinterEvent]:
    return list(
        db.scalars(
            select(m.PrinterEvent)
            .where(m.PrinterEvent.severity != m.EventSeverity.info)
            .order_by(m.PrinterEvent.ts.desc())
            .limit(limit)
        )
    )


def open_alerts(db: Session, limit: int = 100) -> list[m.Alert]:
    return list(
        db.scalars(
            select(m.Alert)
            .where(m.Alert.state == m.AlertState.open)
            .order_by(m.Alert.created_at.desc())
            .limit(limit)
        )
    )


def maintenance_due(db: Session, now: Optional[datetime] = None) -> list[m.MaintenanceSchedule]:
    now = now or datetime.now(timezone.utc)
    return list(
        db.scalars(
            select(m.MaintenanceSchedule)
            .where(
                m.MaintenanceSchedule.next_due.is_not(None),
                m.MaintenanceSchedule.next_due <= now,
            )
            .order_by(m.MaintenanceSchedule.next_due.asc())
        )
    )


def per_client_rollup(db: Session) -> list[dict]:
    """One row per client: counts of approved printers, open alerts, low supplies.

    Used by the Overview "Clients" card so an operator scanning the page can
    see at a glance which client has fires burning, instead of clicking
    through each client to find out.
    """
    out: list[dict] = []
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    for client in clients:
        printer_count = db.scalar(
            select(func.count())
            .select_from(m.Printer)
            .where(
                m.Printer.client_id == client.id,
                m.Printer.discovery_state == m.DiscoveryState.approved,
            )
        ) or 0
        offline_count = db.scalar(
            select(func.count())
            .select_from(m.Printer)
            .where(
                m.Printer.client_id == client.id,
                m.Printer.discovery_state == m.DiscoveryState.approved,
                m.Printer.status.in_([m.PrinterStatus.offline, m.PrinterStatus.error]),
            )
        ) or 0
        # Open alerts join through Printer because Alert.printer_id may be null
        # for agent-scope alerts that aren't a per-client signal.
        open_alerts = db.scalar(
            select(func.count())
            .select_from(m.Alert)
            .join(m.Printer, m.Printer.id == m.Alert.printer_id)
            .where(
                m.Printer.client_id == client.id,
                m.Alert.state == m.AlertState.open,
            )
        ) or 0
        low_supplies = db.scalar(
            select(func.count())
            .select_from(m.Supply)
            .join(m.Printer, m.Printer.id == m.Supply.printer_id)
            .where(
                m.Printer.client_id == client.id,
                m.Printer.discovery_state == m.DiscoveryState.approved,
                m.Supply.level_pct.is_not(None),
                m.Supply.level_pct <= DEFAULT_LOW_SUPPLY_PCT,
            )
        ) or 0
        out.append({
            "client": client,
            "printer_count": printer_count,
            "offline_count": offline_count,
            "open_alerts": open_alerts,
            "low_supplies": low_supplies,
            "sites_count": len(client.sites),
        })
    return out


def recent_activity(db: Session, limit: int = 8) -> list[dict]:
    """Recent events that an operator on /overview would want to scan:

      * Printer status transitions (warnings/criticals only)
      * Open + resolved alerts (latest changes)
      * Newly discovered (pending) printers

    Each row carries a ts, a kind ('event'|'alert'|'discovery'), a severity,
    a one-line message, and a destination link. Sorted newest first.
    """
    def _label(printer_id) -> str:
        printer = db.get(m.Printer, printer_id) if printer_id else None
        if printer is None:
            return ""
        name = printer.display_name or printer.model or printer.hostname or "printer"
        return f"{name} @ {printer.ip}"

    items: list[dict] = []
    for ev in db.scalars(
        select(m.PrinterEvent)
        .where(m.PrinterEvent.severity != m.EventSeverity.info)
        .order_by(m.PrinterEvent.ts.desc())
        .limit(limit)
    ):
        where = _label(ev.printer_id)
        items.append({
            "ts": ev.ts,
            "kind": "event",
            "severity": ev.severity.value,
            # Always say WHICH printer -- a bare "Replace Drum" times twelve
            # is exactly the vagueness operators complain about.
            "message": f"{ev.message} — {where}" if where else ev.message,
            "link": f"/printers/{ev.printer_id}",
        })
    for alert in db.scalars(
        select(m.Alert).order_by(m.Alert.created_at.desc()).limit(limit)
    ):
        items.append({
            "ts": alert.created_at,
            "kind": "alert",
            "severity": alert.severity.value,
            "message": f"{alert.title} ({alert.state.value})",
            "link": f"/printers/{alert.printer_id}" if alert.printer_id else "/alerts",
        })
    for pending in db.scalars(
        select(m.Printer)
        .where(m.Printer.discovery_state == m.DiscoveryState.pending)
        .order_by(m.Printer.created_at.desc())
        .limit(limit)
    ):
        items.append({
            "ts": pending.created_at,
            "kind": "discovery",
            "severity": "info",
            "message": (
                f"Discovered {pending.brand or 'printer'} "
                f"{pending.model or ''} at {pending.ip}".strip()
            ),
            "link": "/approvals",
        })
    items.sort(
        key=lambda r: r["ts"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    # Dedupe identical messages (a printer re-reporting the same condition
    # every poll would otherwise fill the whole card with one line).
    seen: set = set()
    unique: list[dict] = []
    for item in items:
        key = (item["kind"], item["message"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique[:limit]


def page_count_history(db: Session, printer_id: int, limit: int = 90) -> list[m.Reading]:
    """Oldest→newest readings with a page count, for trend charts."""
    rows = list(
        db.scalars(
            select(m.Reading)
            .where(m.Reading.printer_id == printer_id, m.Reading.page_count.is_not(None))
            .order_by(m.Reading.ts.desc())
            .limit(limit)
        )
    )
    return list(reversed(rows))
