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


def sites_served_by_agent(db: Session, agent: m.Agent) -> set[int]:
    """The set of site_ids this agent collects for.

    Always includes the agent's home site. Plus every site that has a Subnet
    row assigned to this agent (the multi-client pattern: one agent at HQ
    serving Beta, Gamma, ... over per-client tunnels).
    """
    extra = db.scalars(
        select(m.Subnet.site_id).where(m.Subnet.agent_id == agent.id).distinct()
    )
    served = {agent.site_id}
    served.update(extra)
    return served


def find_printer_by_ip_in_sites(
    db: Session, site_ids: set[int], ip: str
) -> Optional[m.Printer]:
    if not site_ids:
        return None
    return db.scalar(
        select(m.Printer).where(m.Printer.site_id.in_(site_ids), m.Printer.ip == ip)
    )


def find_subnet_for_agent_cidr(
    db: Session, agent_id: int, cidr: str
) -> Optional[m.Subnet]:
    """The Subnet row this agent is assigned that has the given CIDR -- used
    by ingest to resolve which site (and therefore which client) a discovered
    device or reading belongs to."""
    return db.scalar(
        select(m.Subnet).where(
            m.Subnet.agent_id == agent_id, m.Subnet.cidr == cidr
        )
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
    existing.status_note = supply.status_note
    existing.current = supply.current
    existing.max_capacity = supply.max_capacity
    existing.unit = supply.unit
    existing.updated_at = _now()
    return existing


def _reconcile_events(db: Session, printer: m.Printer, incoming, ts) -> None:
    """Keep one open PrinterEvent per active condition; resolve conditions that cleared.

    SNMP polling re-reports the same error bits every cycle. Without reconciliation
    the events table grows unbounded and the matching alert never auto-resolves
    (it filters on resolved_at IS NULL). We dedup snmp_alert events by code: update
    the existing open row, insert new ones, and resolve open rows whose condition is
    no longer present in this reading.
    """
    open_snmp = list(
        db.scalars(
            select(m.PrinterEvent).where(
                m.PrinterEvent.printer_id == printer.id,
                m.PrinterEvent.source == m.EventSource.snmp_alert,
                m.PrinterEvent.resolved_at.is_(None),
            )
        )
    )
    open_by_code = {ev.code: ev for ev in open_snmp}
    current_codes = {e.code for e in incoming if e.source == m.EventSource.snmp_alert}

    # Resolve conditions that have cleared.
    for ev in open_snmp:
        if ev.code not in current_codes:
            ev.resolved_at = ts

    for event in incoming:
        if event.source == m.EventSource.snmp_alert and event.code in open_by_code:
            ev = open_by_code[event.code]  # refresh the standing condition in place
            ev.ts = ts
            ev.severity = event.severity
            ev.message = event.message
            ev.resolved_at = None
        else:
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


def apply_reading(db: Session, site_id, reading: s.ReadingIn) -> Optional[m.Printer]:
    """Apply one poll result to a known, approved printer.

    ``site_id`` may be a single int (legacy) or a set of int (the multi-client
    agent path -- one agent serves several sites). The printer is looked up by
    IP across whichever site_ids are passed in.

    Returns the printer, or None if no matching approved printer exists at the
    site(s) (discovery happens via the separate /discovered endpoint, not here).
    """
    if isinstance(site_id, (set, list, tuple, frozenset)):
        printer = find_printer_by_ip_in_sites(db, set(site_id), reading.ip)
    else:
        printer = find_printer_by_ip(db, site_id, reading.ip)
    if printer is None or printer.discovery_state != m.DiscoveryState.approved:
        return None

    ts = reading.ts or _now()
    # Refresh identity fields the agent learned over SNMP.
    for attr in ("hostname", "brand", "model", "serial", "firmware"):
        val = getattr(reading, attr)
        if val:
            setattr(printer, attr, val)

    printer.status = reading.status
    if reading.page_count is not None:
        printer.page_count = reading.page_count
    # Cache the latest mono/color split for dashboards/reports. Billing diffs the
    # append-only readings series (below), not this cache, so we simply mirror the
    # latest reported value -- same treatment as page_count.
    if reading.mono_count is not None:
        printer.mono_count = reading.mono_count
    if reading.color_count is not None:
        printer.color_count = reading.color_count
    printer.last_seen = ts
    if reading.provider_trace is not None:
        # Always overwrite with the latest poll's trace. Old traces don't
        # accumulate because the diagnostic value is "what happened on the
        # most recent poll" -- a stale 6-hour-old trace is misleading.
        printer.last_provider_trace = reading.provider_trace

    snapshot = []
    seen_keys: set = set()
    seen_types: set = set()
    for supply in reading.supplies:
        upsert_supply(db, printer, supply)
        seen_keys.add((supply.type, supply.color))
        seen_types.add(supply.type)
        snapshot.append(
            {"type": supply.type.value, "color": supply.color, "level_pct": supply.level_pct}
        )

    # Prune orphaned duplicate supply rows. upsert_supply keys on (type, color),
    # so when a cartridge's key changes across agent/parser versions -- e.g. a
    # colorless generic "toner"/"Drum Unit" that a newer agent now reports with a
    # real color -- the old row is never updated again and lingers forever as a
    # "not reported" line sitting next to the real one. Drop any row whose
    # (type, color) is no longer reported but whose TYPE still is (so it's a
    # superseded duplicate of a same-type supply we DID report this cycle), while
    # leaving a genuinely intermittent unique supply (e.g. a fuser missing from
    # one degraded poll, with no same-type sibling) alone so it isn't flapped
    # away. Mirrors how _reconcile_events drops conditions absent this reading.
    if seen_types:
        for stale in list(
            db.scalars(select(m.Supply).where(m.Supply.printer_id == printer.id))
        ):
            if (stale.type, stale.color) not in seen_keys and stale.type in seen_types:
                db.delete(stale)

    db.add(
        m.Reading(
            printer_id=printer.id,
            ts=ts,
            page_count=reading.page_count,
            mono_count=reading.mono_count,
            color_count=reading.color_count,
            meter_snapshot=reading.meter_snapshot or None,
            status=reading.status,
            supply_snapshot=snapshot or None,
        )
    )

    _reconcile_events(db, printer, reading.events, ts)
    return printer


def update_subnet_discovery_stats(
    db: Session, site_id: int, cidr: str, *, found: int, new: int
) -> Optional[m.Subnet]:
    """Stamp the (site, cidr) Subnet row with the last discovery batch's results.

    Operators read these on the Discovery page so they can tell which subnets
    are actively producing devices (and which agents are silent). No-op for
    agents reporting a CIDR that isn't enrolled as a Subnet -- silently ignored
    so a stale config doesn't break ingest.
    """
    subnet = db.scalar(
        select(m.Subnet).where(m.Subnet.site_id == site_id, m.Subnet.cidr == cidr)
    )
    if subnet is None:
        return None
    subnet.last_discovery_at = _now()
    subnet.last_discovery_found_count = found
    subnet.last_discovery_new_count = new
    return subnet


def record_discovered(
    db: Session, agent: m.Agent, device: s.DiscoveredIn
) -> tuple[m.Printer, bool]:
    """Record a discovered device as a pending printer. Returns (printer, created).

    Site/client assignment: when the device reports a subnet_cidr that matches
    a Subnet row assigned to this agent, the printer is recorded at THAT subnet's
    site (the multi-client agent path -- HQ agent collecting for Beta drops the
    printer into Beta's site, not HQ's). Falls back to agent.site_id when no
    subnet_cidr is given or the CIDR isn't enrolled.
    """
    target_site_id = agent.site_id
    if device.subnet_cidr:
        sub = find_subnet_for_agent_cidr(db, agent.id, device.subnet_cidr)
        if sub is not None:
            target_site_id = sub.site_id

    existing = find_printer_by_ip_in_sites(
        db, sites_served_by_agent(db, agent), device.ip
    )
    if existing is not None:
        # Refresh identity but never downgrade an approved/ignored device to pending.
        for attr in ("mac", "hostname", "brand", "model", "serial", "firmware"):
            val = getattr(device, attr)
            if val and not getattr(existing, attr):
                setattr(existing, attr, val)
        return existing, False

    site = db.get(m.Site, target_site_id)
    printer = m.Printer(
        client_id=site.client_id,
        site_id=target_site_id,
        discovered_by_agent_id=agent.id,
        ip=device.ip,
        mac=device.mac,
        hostname=device.hostname,
        brand=device.brand,
        model=device.model,
        serial=device.serial,
        firmware=device.firmware,
        discovery_state=m.DiscoveryState.pending,
        status=m.PrinterStatus.unknown,
    )
    db.add(printer)
    return printer, True
