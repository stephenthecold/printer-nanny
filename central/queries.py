"""Read-only aggregate queries shared by the reporting API and the dashboard."""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, contains_eager

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


def low_supply_threshold(db: Session) -> float:
    """The operator's low-supply percentage, from Settings.

    ``alerts.low_supply_pct`` is editable in the Alerts settings tab but was
    read by nothing: the dashboard counts and the weekly report used the
    hard-coded ``DEFAULT_LOW_SUPPLY_PCT`` instead, so moving the slider changed
    nothing an operator could see. Resolve it here so every caller picks up the
    configured value, with the constant kept as the fallback default.
    """
    from central.runtime import load_settings  # lazy: avoid an import cycle

    try:
        return float(load_settings(db).get("alerts.low_supply_pct", DEFAULT_LOW_SUPPLY_PCT))
    except (TypeError, ValueError):
        return DEFAULT_LOW_SUPPLY_PCT


def low_supplies(
    db: Session,
    threshold: Optional[float] = None,
    client_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[m.Supply]:
    """Supplies at or below the threshold percentage, lowest first.

    ``threshold=None`` means "use the operator's configured value"; callers
    that genuinely want a specific cutoff (the reporting API) still pass one.

    ``client_id`` scopes the result to one tenant IN SQL, and ``limit`` caps it
    IN SQL. Both must stay in the query rather than being applied to the result
    in Python: a tenant-scoped caller that fetches the whole fleet and then
    narrows it is at best loading every other customer's rows to display ten of
    its own, and -- the moment a cap is involved -- silently shows a tenant
    nothing at all because the cap was spent on rows belonging to somebody else.
    See ``open_alerts`` for the version of that bug which actually shipped.

    The printer is eager-loaded via the join we already make, because every
    caller's template renders ``supply.printer`` -- without it each row costs
    its own SELECT.
    """
    if threshold is None:
        threshold = low_supply_threshold(db)
    stmt = (
        select(m.Supply)
        .join(m.Supply.printer)
        .options(contains_eager(m.Supply.printer))
        .where(
            m.Supply.level_pct.is_not(None),
            m.Supply.level_pct <= threshold,
            m.Printer.discovery_state == m.DiscoveryState.approved,
        )
    )
    if client_id is not None:
        stmt = stmt.where(m.Printer.client_id == client_id)
    stmt = stmt.order_by(m.Supply.level_pct.asc())
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(db.scalars(stmt))


# Days of polling history a printer needs before the consumption slope is
# trustworthy enough to display. Below this, the UI shows "est. in ~Nd".
RUNWAY_MIN_HISTORY_DAYS = 3.0
# Matches the worker's FORECAST_HISTORY_WINDOW_DAYS so the portal and the
# alerting engine fit over the same history and can't disagree about runway.
RUNWAY_HISTORY_WINDOW_DAYS = 30
RUNWAY_MAX_ROWS = 2000


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

    Reads a time window rather than a fixed row count. A 60-row cap looked
    cheap but silently disabled the feature: at the default 300s poll interval
    60 readings span under 5 hours, so the fit never met
    ``RUNWAY_MIN_HISTORY_DAYS`` and every printer reported ``None`` forever.
    Windowing by time makes the span independent of how fast the fleet polls;
    ``RUNWAY_MAX_ROWS`` stays as a safety valve for very fast pollers so a
    long-lived fleet page still stays cheap.
    """
    from central.worker.jobs import forecast_days_to_empty  # lazy: avoid cycle

    now = datetime.now(timezone.utc)
    out: dict = {}
    for pid in printer_ids:
        rows = list(
            db.scalars(
                select(m.Reading)
                .where(
                    m.Reading.printer_id == pid,
                    m.Reading.supply_snapshot.is_not(None),
                    m.Reading.ts >= now - timedelta(days=RUNWAY_HISTORY_WINDOW_DAYS),
                )
                .order_by(m.Reading.ts.desc())
                .limit(RUNWAY_MAX_ROWS)
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


def open_alerts(
    db: Session, limit: int = 100, client_id: Optional[int] = None
) -> list[m.Alert]:
    """Newest open alerts, optionally scoped to one tenant.

    ``client_id`` filters IN SQL, joined through the alert's printer, and the
    LIMIT is applied to the scoped set. That ordering is the whole point and it
    is not a micro-optimisation: the customer portal used to take the newest 30
    alerts FLEET-WIDE and narrow them to the tenant in Python afterwards, so a
    customer with a live critical fault was shown "No open issues right now"
    whenever thirty newer alerts happened to belong to other customers. A
    tenant-scoped caller must never spend its limit on rows it is not allowed
    to see.

    The join is INNER, so agent-scope alerts (``printer_id IS NULL``) are
    excluded when scoping to a client -- they are a fleet signal about an
    agent, not a per-tenant one, and the portal already dropped them. Unscoped
    callers keep the fleet-wide behaviour, agent-scope alerts included.
    """
    stmt = select(m.Alert).where(m.Alert.state == m.AlertState.open)
    if client_id is not None:
        stmt = stmt.join(m.Printer, m.Printer.id == m.Alert.printer_id).where(
            m.Printer.client_id == client_id
        )
    stmt = stmt.order_by(m.Alert.created_at.desc()).limit(limit)
    return list(db.scalars(stmt))


def undelivered_notifications(db: Session) -> dict:
    """Notifications an operator still owes somebody, for the Alerts-page banner.

    ``owed`` counts channel-less deliveries (see
    ``central.channels.delivery.UNROUTED_CHANNEL_KEY``): an alert fired while
    every channel was disabled or excluded, so nothing was sent and nothing will
    be until a channel exists. That state is otherwise invisible -- the alert
    looks merely un-notified -- which is exactly how alerts used to be lost.

    ``dead`` counts per-channel deliveries that exhausted their retry cap while
    their alert is STILL live -- a channel that is broken, not merely off. Dead
    letters for resolved alerts are history rather than a to-do, and channel-less
    rows are excluded because their terminal states (the alert resolved first, a
    sibling delivery superseded them, or the give-up window elapsed) are either
    already accounted for or recorded in the audit log; counting them here would
    keep the banner red over notifications that did in fact go out.
    """
    from central.channels.delivery import UNROUTED_CHANNEL_KEY

    owed = db.scalar(
        select(func.count())
        .select_from(m.NotificationDelivery)
        .where(
            m.NotificationDelivery.channel_key == UNROUTED_CHANNEL_KEY,
            m.NotificationDelivery.status == m.DeliveryStatus.pending,
        )
    ) or 0
    dead = db.scalar(
        select(func.count())
        .select_from(m.NotificationDelivery)
        .join(m.Alert, m.Alert.id == m.NotificationDelivery.alert_id)
        .where(
            m.NotificationDelivery.status == m.DeliveryStatus.dead,
            m.NotificationDelivery.channel_key != UNROUTED_CHANNEL_KEY,
            m.Alert.state != m.AlertState.resolved,
        )
    ) or 0
    return {"owed": owed, "dead": dead}


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
    low_pct = low_supply_threshold(db)
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
                m.Supply.level_pct <= low_pct,
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


# --------------------------------------------------------------------------- #
# Fleet-wide printer search
#
# The gap this closes, in the words of the audit that found it: "a tech handed
# '10.4.7.23 is jamming' must already know the client". Every printer listing in
# this app was reachable only by first choosing a client, so the one identifier
# a caller actually gives you was the one thing you could not look up.
#
# ON INDEXES, HONESTLY -- BECAUSE NO NEW ONE IS ADDED HERE. This is a
# *substring* match, because SNMP strings vary ("HP LaserJet MFP M428fdw" vs
# "M428"), and a leading-wildcard LIKE is not servable by a B-tree on any
# backend: not on SQLite, not on Postgres, and not by an expression index on
# `lower(col)` either, which serves equality only. Adding B-trees on the seven
# searched columns would look like diligence and would be read by exactly none
# of the statements below. The one thing that *would* work is a Postgres
# `pg_trgm` GIN index, and it is deliberately NOT taken: revision 0001 is
# `Base.metadata.create_all()`, so an index in ORM metadata is emitted on every
# fresh install, and a trgm index first requires `CREATE EXTENSION pg_trgm` to
# have succeeded. That lets a search page block a new installation outright on
# any server whose role cannot create extensions.
#
# What bounds this instead is the page. Measured on Postgres 16 at fleet scale
# (10,000 printers / 200 clients, warm, best of five):
#
#   no term, page 1 .............  2.5 ms      q='M428' (1429 hits) .. 21.5 ms
#   no term, page 100 ........... 14.2 ms      q='M428' one client ...  2.4 ms
#   term matching nothing ....... 50.7 ms  <-- the worst case
#
# The *page* query is the cheap half and stays cheap as the fleet grows: ordering
# by `clients.name` lets Postgres walk the clients index and stop at 50 rows
# (EXPLAIN: Incremental Sort over a nested loop on `ix_printers_client_id`,
# 0.7 ms). The COUNT is the half that scans, because a count cannot carry a
# LIMIT -- so the honest statement is that this is O(rows-in-`printers`) with a
# very small constant, on a table that is fleet-sized (thousands, versus the
# millions in `readings`), and it is O(page) in what it renders and transfers.
# If a fleet ever outgrows that, the upgrade is a Postgres-only migration adding
# `USING gin (col gin_trgm_ops)` outside the ORM metadata and guarded on the
# extension being available -- not a change to this function.
# --------------------------------------------------------------------------- #

# Escaping, not stripping. A serial really can contain an underscore, and an
# operator searching for "_" who silently got the entire fleet back would have
# no way to tell the search was broken rather than the data.
_LIKE_ESCAPE = "\\"

# Every field the audit named, and nothing else -- widening this silently
# changes what a saved search means.
_PRINTER_SEARCH_FIELDS = (
    "ip",
    "hostname",
    "serial",
    "asset_tag",
    "model",
    "brand",
    "display_name",
)

PRINTER_SEARCH_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200
# Long enough for the longest thing anyone pastes (a URL-ish sysDescr fragment),
# short enough that the pattern can't be used to make the LIKE itself expensive.
MAX_SEARCH_TERM = 120


def _like_contains(term: str) -> str:
    """A ``%term%`` pattern with LIKE's own metacharacters neutralised."""
    escaped = (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def search_printers(
    db: Session,
    q: str = "",
    client_id: Optional[int] = None,
    states: Optional[list] = None,
    page: int = 1,
    per_page: int = PRINTER_SEARCH_PAGE_SIZE,
) -> dict:
    """One page of the fleet, optionally narrowed by a substring ``q``.

    ``client_id`` is a hard tenant scope applied **in SQL**, never a filter over
    an already-limited result: this app has shipped that bug once already (the
    portal took the newest 30 alerts fleet-wide and only then kept the client's,
    so a customer with an open fault saw "no open issues"). Filtering after a
    LIMIT is not a slow correct answer, it is a wrong one.

    Matching is case-insensitive on both backends. ``ilike`` renders as Postgres
    ``ILIKE`` and as ``lower(col) LIKE lower(?)`` on SQLite; ASCII -- which is
    what SNMP identifiers, IPs and asset tags are -- folds identically under
    both. Non-ASCII does not: SQLite's ``lower()`` is ASCII-only, so a Cyrillic
    or accented capital matches its lowercase form on Postgres and not on SQLite.
    That is a genuine dialect difference, recorded rather than papered over;
    equal-case matching works everywhere.

    Returns ``{"rows", "total", "page", "pages", "per_page", "offset", "q"}``
    where each row is ``{"printer", "client", "site"}``.
    """
    term = (q or "").strip()[:MAX_SEARCH_TERM]
    per_page = max(1, min(int(per_page or PRINTER_SEARCH_PAGE_SIZE), _MAX_PAGE_SIZE))

    filters = []
    if client_id is not None:
        filters.append(m.Printer.client_id == client_id)
    if states:
        filters.append(m.Printer.discovery_state.in_(list(states)))
    if term:
        pattern = _like_contains(term)
        filters.append(
            or_(
                *[
                    getattr(m.Printer, field).ilike(pattern, escape=_LIKE_ESCAPE)
                    for field in _PRINTER_SEARCH_FIELDS
                ]
            )
        )

    total = db.scalar(
        select(func.count()).select_from(m.Printer).where(*filters)
    ) or 0
    pages = max(1, (total + per_page - 1) // per_page)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    # Clamped to a real page rather than 404'd: a stale bookmark pointing past
    # the end of a shrunken fleet should show the last page, not an error.
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page

    rows = db.execute(
        select(m.Printer, m.Client, m.Site)
        .join(m.Client, m.Client.id == m.Printer.client_id)
        .join(m.Site, m.Site.id == m.Printer.site_id)
        .where(*filters)
        # Printer.id last so the total order is strict. Without a unique
        # tiebreaker, two printers equal on every sort key can swap between
        # requests and OFFSET paging then repeats one and skips the other.
        .order_by(m.Client.name, m.Site.name, m.Printer.ip, m.Printer.id)
        .limit(per_page)
        .offset(offset)
    ).all()

    return {
        "rows": [{"printer": p, "client": c, "site": s} for p, c, s in rows],
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "offset": offset,
        "q": term,
    }


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
