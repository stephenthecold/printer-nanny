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
