"""Read-only aggregate queries shared by the reporting API and the dashboard."""

from __future__ import annotations

import ipaddress
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from central import models as m

DEFAULT_LOW_SUPPLY_PCT = 20.0

# SNMP versions that transmit the community string / data in the clear. v3 USM
# adds authentication + (optionally) privacy, so we treat it as the only
# "secure" transport for the device security-posture report.
INSECURE_SNMP_VERSIONS = {"1", "v1", "2c", "v2c", "2", "v2"}


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


def component_maintenance_schedules(db: Session) -> list[m.MaintenanceSchedule]:
    """Schedules that trigger on component-life percentage (not a date/page).

    These are evaluated against the matching component-life Supply rows on the
    target printer(s) rather than ``next_due``, so they don't require a date.
    """
    return list(
        db.scalars(
            select(m.MaintenanceSchedule)
            .where(
                m.MaintenanceSchedule.component_type.is_not(None),
                m.MaintenanceSchedule.life_threshold.is_not(None),
            )
            .order_by(m.MaintenanceSchedule.id.asc())
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


def _normalize_snmp_version(version: Optional[str]) -> str:
    """Canonicalize a stored SNMP version string to '1' / '2c' / '3'."""
    if not version:
        return "2c"  # central/agent default when nothing is configured
    v = version.strip().lower().lstrip("v")
    if v in ("1",):
        return "1"
    if v in ("2", "2c"):
        return "2c"
    if v in ("3",):
        return "3"
    return v or "2c"


def _subnet_snmp_version_for(printer: m.Printer, subnets: list[m.Subnet]) -> tuple[str, Optional[str]]:
    """Effective SNMP version for a printer, derived from its SUBNET config.

    The anchor signal for the posture report is "what SNMP version does this
    device actually talk over", which is owned by the subnet the printer sits
    in (each subnet row carries its own creds). We match the printer's IP
    against the CIDRs of the subnets in its own site; the matching subnet's
    ``snmp_version`` wins. Falls back to the printer's own ``snmp_version``
    column when no subnet contains the IP (e.g. a manually-added device, or an
    IP outside any enrolled CIDR).

    Returns (version, source) where source is the subnet label/cidr or
    "printer" so the UI can show where the determination came from.
    """
    try:
        ip = ipaddress.ip_address(printer.ip)
    except ValueError:
        ip = None
    if ip is not None:
        for sub in subnets:
            if sub.site_id != printer.site_id:
                continue
            try:
                net = ipaddress.ip_network(sub.cidr, strict=False)
            except ValueError:
                continue
            if ip in net:
                return _normalize_snmp_version(sub.snmp_version), (sub.label or sub.cidr)
    return _normalize_snmp_version(printer.snmp_version), "printer"


def security_posture_rollup(db: Session, client_id: Optional[int] = None) -> dict:
    """Per-device security posture + a fleet summary -- "treat printers like
    endpoints".

    Grounded entirely in data we already hold:

      * insecure_snmp -- derived from the SNMP version the device talks over
        (subnet config; v1/v2c are cleartext, v3 USM is authenticated). This is
        the anchor signal and is fully available today.
      * firmware -- best-effort version string captured during polling
        (sysDescr / vendor field); honestly "unknown" when the device exposes
        nothing, never fabricated.

    Posture is COMPUTED on read (not denormalized): the SNMP version follows
    the live subnet config, so a row would otherwise go stale the moment an
    operator flips a subnet to v3. Firmware is the only stored input and it's a
    fact the agent collected, not a derived verdict.

    Returns ``{"rows": [...], "summary": {...}}`` scoped to ``client_id`` when
    given. Each row: printer, client, site, snmp_version, snmp_secure (bool),
    snmp_source, firmware (str|None), firmware_known (bool), flags (list[str]).
    """
    stmt = (
        select(m.Printer)
        .where(m.Printer.discovery_state == m.DiscoveryState.approved)
    )
    if client_id is not None:
        stmt = stmt.where(m.Printer.client_id == client_id)
    stmt = stmt.order_by(m.Printer.client_id, m.Printer.site_id, m.Printer.ip)
    printers = list(db.scalars(stmt))

    subnets = list(db.scalars(select(m.Subnet)))
    clients = {c.id: c for c in db.scalars(select(m.Client))}
    sites = {s.id: s for s in db.scalars(select(m.Site))}

    rows: list[dict] = []
    insecure_count = 0
    secure_count = 0
    unknown_fw_count = 0
    for printer in printers:
        version, source = _subnet_snmp_version_for(printer, subnets)
        secure = version not in INSECURE_SNMP_VERSIONS
        firmware = (printer.firmware or "").strip() or None
        firmware_known = firmware is not None

        flags: list[str] = []
        if not secure:
            flags.append("insecure-snmp")
            insecure_count += 1
        else:
            secure_count += 1
        if not firmware_known:
            flags.append("firmware-unknown")
            unknown_fw_count += 1

        rows.append({
            "printer": printer,
            "client": clients.get(printer.client_id),
            "site": sites.get(printer.site_id),
            "snmp_version": version,
            "snmp_secure": secure,
            "snmp_source": source,
            "firmware": firmware,
            "firmware_known": firmware_known,
            "flags": flags,
        })

    summary = {
        "total": len(rows),
        "insecure_snmp": insecure_count,
        "secure_snmp": secure_count,
        "firmware_unknown": unknown_fw_count,
        "firmware_known": len(rows) - unknown_fw_count,
        # Devices with at least one posture flag raised.
        "flagged": sum(1 for r in rows if r["flags"]),
    }
    return {"rows": rows, "summary": summary}


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


def _printed_pages_for_printer(rows: "list[tuple[int]]") -> int:
    """Sum positive page-count deltas across one printer's reading series.

    ``rows`` is oldest→newest ``(page_count,)`` tuples. We only add a delta
    when it's positive, so a counter reset (firmware reflash) or a printer
    swap onto the same DB row (page_count drops to a smaller absolute number)
    contributes 0 for that step instead of a large NEGATIVE number that would
    cancel out real prints. The trade-off — pages printed between the last
    reading before a reset and the reset itself are lost — is the safe
    direction for a billing-adjacent estimate: never invent prints, never go
    negative.
    """
    total = 0
    prev: Optional[int] = None
    for (pc,) in rows:
        if pc is None:
            continue
        if prev is not None and pc > prev:
            total += pc - prev
        prev = pc
    return total


def sustainability_rollup(
    db: Session,
    client_id: Optional[int] = None,
    since: Optional[datetime] = None,
) -> dict:
    """Estimated print footprint derived from page-count history.

    Sums physical pages printed (positive page_count deltas per printer, so
    counter resets / printer swaps can't produce negative or inflated totals),
    then converts to sheets, paper mass, CO2e, energy, and tree-equivalents
    using the operator-tunable factors in ``runtime.SPECS`` (``esg.*``). Every
    derived number is an ESTIMATE; the factors carry defensible public defaults
    (see runtime.py and the figures below).

    Scope: approved printers only, optionally narrowed to one ``client_id``
    (tenant scoping for the customer portal) and/or to readings on/after
    ``since``.

    Returns a flat dict::

        {
          "pages":        int,    # raw impressions (page-count deltas)
          "sheets":       float,  # physical sheets after the duplex nudge
          "paper_g":      float,  # paper mass, grams
          "paper_kg":     float,  # convenience: paper_g / 1000
          "co2_kg":       float,  # CO2e, kilograms
          "kwh":          float,  # print energy, kilowatt-hours
          "trees":        float,  # tree-equivalents of paper consumed
          "printers":     int,    # approved printers in scope
          "duplex_nudge": float,  # the esg.sheets_per_page factor applied
          "factors":      {...},  # the esg.* factors used, for transparency
          "estimated":    True,   # these are estimates, label them as such
        }
    """
    from central.runtime import load_settings  # lazy: avoid import cycle

    rt = load_settings(db)
    sheets_per_page = float(rt.get("esg.sheets_per_page") or 0.0)
    paper_g_per_sheet = float(rt.get("esg.paper_g_per_sheet") or 0.0)
    co2_g_per_sheet = float(rt.get("esg.co2_g_per_sheet") or 0.0)
    kwh_per_page = float(rt.get("esg.kwh_per_page") or 0.0)
    sheets_per_tree = float(rt.get("esg.sheets_per_tree") or 0.0)

    printer_q = select(m.Printer.id).where(
        m.Printer.discovery_state == m.DiscoveryState.approved
    )
    if client_id is not None:
        printer_q = printer_q.where(m.Printer.client_id == client_id)
    printer_ids = list(db.scalars(printer_q))

    pages = 0
    for pid in printer_ids:
        stmt = (
            select(m.Reading.page_count)
            .where(m.Reading.printer_id == pid, m.Reading.page_count.is_not(None))
            .order_by(m.Reading.ts.asc())
        )
        if since is not None:
            stmt = stmt.where(m.Reading.ts >= since)
        pages += _printed_pages_for_printer(list(db.execute(stmt)))

    sheets = pages * sheets_per_page
    paper_g = sheets * paper_g_per_sheet
    co2_kg = (sheets * co2_g_per_sheet) / 1000.0
    kwh = pages * kwh_per_page
    trees = (sheets / sheets_per_tree) if sheets_per_tree else 0.0

    return {
        "pages": pages,
        "sheets": sheets,
        "paper_g": paper_g,
        "paper_kg": paper_g / 1000.0,
        "co2_kg": co2_kg,
        "kwh": kwh,
        "trees": trees,
        "printers": len(printer_ids),
        "duplex_nudge": sheets_per_page,
        "factors": {
            "sheets_per_page": sheets_per_page,
            "paper_g_per_sheet": paper_g_per_sheet,
            "co2_g_per_sheet": co2_g_per_sheet,
            "kwh_per_page": kwh_per_page,
            "sheets_per_tree": sheets_per_tree,
        },
        "estimated": True,
    }
