"""Reusable domain logic shared by the API, the seed script, and tests.

Keeping the "apply a reading" path here (rather than inline in a router) means the
seed script and unit tests exercise exactly the same code the agent ingest uses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import schemas as s


def _now() -> datetime:
    return datetime.now(timezone.utc)


def find_printer_by_ip(db: Session, site_id: int, ip: str) -> Optional[m.Printer]:
    return db.scalar(
        select(m.Printer).where(m.Printer.site_id == site_id, m.Printer.ip == ip)
    )


def upsert_supply(db: Session, printer: m.Printer, supply: s.SupplyIn) -> m.Supply:
    """Insert or update a printer's supply row, keyed by (type, color)."""
    existing = db.scalar(
        select(m.Supply).where(
            m.Supply.printer_id == printer.id,
            m.Supply.type == supply.type,
            m.Supply.color == supply.color,
        )
    )
    if existing is None:
        existing = m.Supply(printer_id=printer.id, type=supply.type, color=supply.color)
        db.add(existing)
    existing.description = supply.description
    existing.level_pct = supply.level_pct
    existing.current = supply.current
    existing.max_capacity = supply.max_capacity
    existing.unit = supply.unit
    existing.updated_at = _now()
    return existing


def apply_reading(db: Session, site_id: int, reading: s.ReadingIn) -> Optional[m.Printer]:
    """Apply one poll result to a known, approved printer.

    Returns the printer, or None if no matching approved printer exists at the site
    (discovery happens via the separate /discovered endpoint, not here).
    """
    printer = find_printer_by_ip(db, site_id, reading.ip)
    if printer is None or printer.discovery_state != m.DiscoveryState.approved:
        return None

    ts = reading.ts or _now()
    # Refresh identity fields the agent learned over SNMP.
    for attr in ("hostname", "brand", "model", "serial"):
        val = getattr(reading, attr)
        if val:
            setattr(printer, attr, val)

    printer.status = reading.status
    if reading.page_count is not None:
        printer.page_count = reading.page_count
    printer.last_seen = ts

    snapshot = []
    for supply in reading.supplies:
        upsert_supply(db, printer, supply)
        snapshot.append(
            {"type": supply.type.value, "color": supply.color, "level_pct": supply.level_pct}
        )

    db.add(
        m.Reading(
            printer_id=printer.id,
            ts=ts,
            page_count=reading.page_count,
            status=reading.status,
            supply_snapshot=snapshot or None,
        )
    )

    for event in reading.events:
        db.add(
            m.PrinterEvent(
                printer_id=printer.id,
                ts=ts,
                code=event.code,
                severity=event.severity,
                source=event.source,
                message=event.message,
            )
        )
    return printer


def record_discovered(
    db: Session, agent: m.Agent, device: s.DiscoveredIn
) -> tuple[m.Printer, bool]:
    """Record a discovered device as a pending printer. Returns (printer, created)."""
    existing = find_printer_by_ip(db, agent.site_id, device.ip)
    if existing is not None:
        # Refresh identity but never downgrade an approved/ignored device to pending.
        for attr in ("mac", "hostname", "brand", "model", "serial"):
            val = getattr(device, attr)
            if val and not getattr(existing, attr):
                setattr(existing, attr, val)
        return existing, False

    site = db.get(m.Site, agent.site_id)
    printer = m.Printer(
        client_id=site.client_id,
        site_id=agent.site_id,
        discovered_by_agent_id=agent.id,
        ip=device.ip,
        mac=device.mac,
        hostname=device.hostname,
        brand=device.brand,
        model=device.model,
        serial=device.serial,
        discovery_state=m.DiscoveryState.pending,
        status=m.PrinterStatus.unknown,
    )
    db.add(printer)
    return printer, True
