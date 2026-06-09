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
    items: list[dict] = []
    for ev in db.scalars(
        select(m.PrinterEvent)
        .where(m.PrinterEvent.severity != m.EventSeverity.info)
        .order_by(m.PrinterEvent.ts.desc())
        .limit(limit)
    ):
        items.append({
            "ts": ev.ts,
            "kind": "event",
            "severity": ev.severity.value,
            "message": ev.message,
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
    return items[:limit]


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
