"""Read-only aggregate queries shared by the reporting API and the dashboard."""

from __future__ import annotations

import ipaddress
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import and_, case, func, not_, or_, select
from sqlalchemy.orm import Session, contains_eager

from central import models as m

DEFAULT_LOW_SUPPLY_PCT = 20.0

# SNMP versions that transmit the community string / data in the clear.
INSECURE_SNMP_VERSIONS = {"1", "v1", "2c", "v2c", "2", "v2"}

# Grades for the transport OUR polling uses. Three, not two, because "v3" alone
# is not a security property: USM's noAuthNoPriv level is v3 with authentication
# and privacy both switched off, and the agent polls at exactly that level when
# no security_level is recorded (agent/printer_nanny_agent/snmp.py: `level =
# (params.v3_security_level or "noAuthNoPriv")`). Grading every v3 row "secure"
# therefore printed a green badge over an unauthenticated cleartext session --
# a posture report calling absence safety, which is worse than no report.
SNMP_GRADE_CLEARTEXT = "cleartext"          # v1/v2c, or v3 noAuthNoPriv
SNMP_GRADE_AUTHENTICATED = "authenticated"  # v3 authNoPriv: signed, not encrypted
SNMP_GRADE_ENCRYPTED = "encrypted"          # v3 authPriv: signed and encrypted

# Community strings every vendor ships. Unlike "is v2c bad", this needs no
# inference about the device: the string is in our own subnet row, and if it is
# still the factory default then every SNMP reader on that broadcast domain can
# read the device today.
DEFAULT_SNMP_COMMUNITIES = {"public", "private"}

# Posture flags. These are SECURITY FINDINGS about the monitoring path, and the
# wording of each is deliberately narrower than "this printer is insecure":
#
#   snmp-cleartext          Our polling credential and every reading cross the
#                           customer VLAN unencrypted. Asserted about our own
#                           configuration, which we own and can prove.
#   snmp-default-community  The community string is still a factory default.
#
# What is deliberately NOT asserted: that the device accepts SNMP *writes*. SET
# is the attack that reconfigures a printer, we never attempt one, and modern
# firmware increasingly refuses it while still answering reads -- HP's "Secure
# by Default" (FutureSmart 4.5+) disables SNMPv1/v2 write access and leaves read
# enabled. Reporting a read-only v2c device as "insecure" would send a
# technician to argue with a printer that is already hardened, which is how a
# security report teaches its readers to ignore it.
FLAG_SNMP_CLEARTEXT = "snmp-cleartext"
FLAG_SNMP_DEFAULT_COMMUNITY = "snmp-default-community"

# Presentation metadata for the two above and for the transport grades. It
# lives here rather than in the template for two reasons: the wording of a
# security claim belongs next to the logic that decides it, and a flag code
# spelled in a Jinja literal is indistinguishable from a CSS class to the
# tree-shaking guard in tests/test_static_assets.py (which is why the previous
# flag name needed an entry in that test's exemption list). `tone` values are
# the badge() tones in _components.html.
POSTURE_FLAG_META = {
    FLAG_SNMP_CLEARTEXT: {
        "label": "cleartext SNMP",
        "tone": "warning",
        "detail": (
            "Polling credential and readings cross the customer VLAN "
            "unencrypted. Not a claim that the device accepts SNMP writes."
        ),
    },
    FLAG_SNMP_DEFAULT_COMMUNITY: {
        "label": "default community",
        "tone": "error",
        "detail": (
            "The community string on this subnet is still a vendor default, "
            "so any SNMP reader on the broadcast domain can query the device."
        ),
    },
}

SNMP_GRADE_META = {
    SNMP_GRADE_CLEARTEXT: {"label": "cleartext", "tone": "warning"},
    SNMP_GRADE_AUTHENTICATED: {"label": "authNoPriv", "tone": "info"},
    SNMP_GRADE_ENCRYPTED: {"label": "authPriv", "tone": "ok"},
}


def fleet_summary(db: Session, client_id: Optional[int] = None) -> dict:
    """Counts of printers by status, plus agent and alert tallies.

    ``client_id`` scopes ALL FIVE numbers. It used to scope only the printer
    counts: ``pending_discovery``, ``open_alerts`` and ``agents_offline`` stayed
    fleet-wide, so an operator asking ``/api/v1/reports/fleet?client_id=3`` was
    handed that client's 7 printers beside the whole MSP's 40 open alerts, with
    nothing in the response saying the numbers came from different populations.
    Not a tenant leak -- the route is staff-only and the filter is an operator
    convenience -- but a wrong answer, and the harder kind: every figure is
    individually correct, so nothing looks broken.

    Labelling the three instead was the alternative and is worse here. This is a
    JSON endpoint whose consumers we do not control, and a label only helps a
    reader who notices it; the summary is one object describing one thing, and
    the only self-consistent reading of "summarise client 3" is five numbers
    about client 3.

    HOW EACH IS SCOPED, since the three tables reach a client differently:

    * printers -- directly, by ``printers.client_id``.
    * alerts   -- through the printer, matching ``per_client_rollup`` and the
      client drill-down. ``Alert.printer_id`` is nullable, so an agent-scope
      alert (an offline collector) has none; those are counted by
      ``agents_offline`` and would otherwise be counted twice.
    * agents   -- through ``agents.site_id -> sites.client_id``. An agent has no
      client column: it belongs to a site, and a site belongs to a client.
    """
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

    pending_stmt = (
        select(func.count())
        .select_from(m.Printer)
        .where(m.Printer.discovery_state == m.DiscoveryState.pending)
    )
    alerts_stmt = (
        select(func.count()).select_from(m.Alert).where(m.Alert.state == m.AlertState.open)
    )
    agents_stmt = (
        select(func.count()).select_from(m.Agent).where(m.Agent.status == m.AgentStatus.offline)
    )
    if client_id is not None:
        pending_stmt = pending_stmt.where(m.Printer.client_id == client_id)
        alerts_stmt = alerts_stmt.join(
            m.Printer, m.Printer.id == m.Alert.printer_id
        ).where(m.Printer.client_id == client_id)
        agents_stmt = agents_stmt.join(m.Site, m.Site.id == m.Agent.site_id).where(
            m.Site.client_id == client_id
        )

    return {
        "total_printers": total,
        "by_status": by_status,
        "pending_discovery": db.scalar(pending_stmt) or 0,
        "open_alerts": db.scalar(alerts_stmt) or 0,
        "agents_offline": db.scalar(agents_stmt) or 0,
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


def receptacle_supply_clause():
    """SQL for "this supply's level means how FULL it is".

    The SQL twin of ``central.supplies.is_receptacle`` and deliberately the same
    rule, read the same way round: the device's own class wins, and a class that
    is absent or ``other`` falls back to the supply type.

    It is SQL rather than a Python filter because both callers count rows --
    ``low_supplies`` applies its cap IN SQL, and ``per_client_rollup`` never
    materialises a Supply at all -- so filtering after the fact would either
    spend the cap on receptacles or not run.

    ``coalesce`` is what makes the NULL case behave: in SQL ``NULL <> 'consumed'``
    is NULL, not true, so a bare inequality would quietly exclude every row
    written before the class was stored -- which is all of them.
    """
    from central.supplies import (
        SUPPLY_CLASS_CONSUMED,
        SUPPLY_CLASS_RECEPTACLE,
        receptacle_supply_types,
    )

    cls = func.lower(func.coalesce(m.Supply.supply_class, ""))
    return or_(
        cls == SUPPLY_CLASS_RECEPTACLE,
        and_(
            cls != SUPPLY_CLASS_RECEPTACLE,
            cls != SUPPLY_CLASS_CONSUMED,
            m.Supply.type.in_(receptacle_supply_types()),
        ),
    )


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

    RECEPTACLES ARE EXCLUDED, and that is a correctness rule rather than a
    filter. A waste container's level is how FULL it is (RFC 3805
    ``receptacleThatIsFilled``), so a freshly serviced box reporting 5 was the
    top hit on every "Low supplies" panel in the product -- an operator sent to
    replace a part that had just been replaced. "Low" is only meaningful for a
    supply that is consumed; a full receptacle is a different fact and is
    surfaced by ``central.reorder``. See ``central.supplies`` for why the type
    fallback makes this correct for rows written before the class was stored.
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
            not_(receptacle_supply_clause()),
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


# --------------------------------------------------------------------------- #
# Operations dashboard queues
# --------------------------------------------------------------------------- #
def _aware_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Treat SQLite's naive timestamps as UTC, matching worker/freshness logic."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def subnet_health(db: Session, now: Optional[datetime] = None) -> dict:
    """One honest tunnel-health row per configured subnet, worst first.

    A heartbeat only proves that the collector can reach central. The product
    decision for the dashboard is stricter: a subnet is ``verified`` only when
    the active agent is current *and* at least one approved printer inside that
    subnet has completed a poll inside the configured printer-offline grace.
    That last printer reply is the end-to-end evidence that the site tunnel,
    route, SNMP path, agent, and central ingest path all worked together.

    Printers do not carry a subnet foreign key. Matching therefore uses the
    collector's existing longest-prefix rule over the printer IP and the
    subnets at its site. This avoids creating a second, drifting definition of
    which overlapping subnet owns an address.

    No SNMP credentials are returned. This dictionary is rendered into HTML,
    and a health read must never become a path that exposes community strings
    or v3 secrets merely because the operator opened the dashboard.
    """
    from central.collector import subnet_for_ip
    from central.freshness import humanize_age
    from central.runtime import load_settings

    now = _aware_utc(now) or datetime.now(timezone.utc)
    runtime = load_settings(db)
    try:
        agent_grace = max(1, int(runtime.get("alerts.offline_grace_seconds", 300)))
    except (TypeError, ValueError):
        agent_grace = 300
    try:
        printer_grace = max(
            60, int(runtime.get("alerts.printer_offline_minutes", 30)) * 60
        )
    except (TypeError, ValueError):
        printer_grace = 1800

    clients = {row.id: row for row in db.scalars(select(m.Client))}
    sites = {row.id: row for row in db.scalars(select(m.Site))}
    agents = {row.id: row for row in db.scalars(select(m.Agent))}
    subnets = list(db.scalars(select(m.Subnet)))
    printers = list(
        db.scalars(
            select(m.Printer).where(
                m.Printer.discovery_state == m.DiscoveryState.approved
            )
        )
    )

    subnets_by_site: dict = {}
    for subnet in subnets:
        subnets_by_site.setdefault(subnet.site_id, []).append(subnet)
    printers_by_subnet: dict = {subnet.id: [] for subnet in subnets}
    for printer in printers:
        subnet = subnet_for_ip(subnets_by_site.get(printer.site_id, []), printer.ip)
        if subnet is not None:
            printers_by_subnet[subnet.id].append(printer)

    rows = []
    counts = {"verified": 0, "attention": 0, "down": 0}
    active_agent_ids = set()
    for subnet in subnets:
        site = sites.get(subnet.site_id)
        client = clients.get(site.client_id) if site is not None else None
        # A leased subnet is served by its current collector. Without a lease,
        # or before the first lease is acquired, its primary remains the only
        # honest agent to show.
        active_agent_id = subnet.collector_agent_id or subnet.agent_id
        if active_agent_id is not None:
            active_agent_ids.add(active_agent_id)
        agent = agents.get(active_agent_id) if active_agent_id else None
        heartbeat = _aware_utc(agent.last_heartbeat) if agent is not None else None
        heartbeat_age = (
            max(0.0, (now - heartbeat).total_seconds()) if heartbeat else None
        )

        subnet_printers = printers_by_subnet.get(subnet.id, [])
        last_printer = max(
            (p for p in subnet_printers if p.last_seen is not None),
            key=lambda p: _aware_utc(p.last_seen),
            default=None,
        )
        last_poll = _aware_utc(last_printer.last_seen) if last_printer else None
        poll_age = max(0.0, (now - last_poll).total_seconds()) if last_poll else None

        agent_current = bool(
            agent is not None
            and agent.status == m.AgentStatus.online
            and heartbeat_age is not None
            and heartbeat_age <= agent_grace
        )
        poll_current = poll_age is not None and poll_age <= printer_grace

        if agent is None:
            state, tone, label = "down", "error", "No agent"
            explanation = "No collector is assigned to this subnet."
        elif not agent_current:
            state, tone, label = "down", "error", "Agent offline"
            explanation = "The collector is not checking in; the tunnel is not verified."
        elif not subnet_printers:
            state, tone, label = "attention", "warning", "Not verified"
            explanation = "No approved printer is available to prove the subnet path."
        elif last_poll is None:
            state, tone, label = "attention", "warning", "Awaiting first poll"
            explanation = "No printer on this subnet has replied yet."
        elif not poll_current:
            state, tone, label = "down", "error", "Poll overdue"
            explanation = "No printer reply arrived inside the configured offline window."
        else:
            state, tone, label = "verified", "ok", "Verified"
            explanation = "A printer on this subnet replied successfully."

        counts[state] += 1
        rows.append(
            {
                "subnet_id": subnet.id,
                "subnet_label": subnet.label or subnet.cidr,
                "cidr": subnet.cidr,
                "client_name": client.name if client is not None else "Unknown client",
                "site_name": site.name if site is not None else "Unknown site",
                "agent_id": agent.id if agent is not None else None,
                "agent_name": agent.name if agent is not None else "Unassigned",
                "agent_status": agent.status.value if agent is not None else "unassigned",
                "heartbeat_at": heartbeat,
                "heartbeat_iso": heartbeat.isoformat() if heartbeat else "",
                "heartbeat_age": humanize_age(heartbeat_age),
                "last_poll_at": last_poll,
                "last_poll_iso": last_poll.isoformat() if last_poll else "",
                "last_poll_age": humanize_age(poll_age),
                "last_printer_id": last_printer.id if last_printer else None,
                "last_printer_name": (
                    last_printer.display_name
                    or last_printer.model
                    or last_printer.hostname
                    or last_printer.ip
                ) if last_printer else "",
                "printer_count": len(subnet_printers),
                "state": state,
                "tone": tone,
                "label": label,
                "explanation": explanation,
            }
        )

    state_rank = {"down": 0, "attention": 1, "verified": 2}
    rows.sort(
        key=lambda row: (
            state_rank[row["state"]],
            row["client_name"].casefold(),
            row["site_name"].casefold(),
            row["subnet_label"].casefold(),
        )
    )

    # Agents not currently serving any subnet are not allowed to disappear from
    # a page titled "System and agent status". Healthy spare/unassigned agents
    # are setup detail, not an incident; unhealthy ones are the exception worth
    # surfacing. An agent already represented by a subnet row is omitted here
    # because that row carries the same failure and the remediation link.
    agent_issues = []
    for agent in agents.values():
        if agent.id in active_agent_ids:
            continue
        heartbeat = _aware_utc(agent.last_heartbeat)
        heartbeat_age = (
            max(0.0, (now - heartbeat).total_seconds()) if heartbeat else None
        )
        if (
            agent.status == m.AgentStatus.online
            and heartbeat_age is not None
            and heartbeat_age <= agent_grace
        ):
            continue
        site = sites.get(agent.site_id)
        client = clients.get(site.client_id) if site is not None else None
        agent_issues.append(
            {
                "agent_id": agent.id,
                "agent_name": agent.name,
                "client_name": client.name if client is not None else "Unknown client",
                "site_name": site.name if site is not None else "Unknown site",
                "status": agent.status.value,
                "heartbeat_at": heartbeat,
                "heartbeat_iso": heartbeat.isoformat() if heartbeat else "",
                "heartbeat_age": humanize_age(heartbeat_age),
            }
        )
    agent_issues.sort(
        key=lambda row: (
            row["client_name"].casefold(),
            row["site_name"].casefold(),
            row["agent_name"].casefold(),
        )
    )
    return {
        "rows": rows,
        "total": len(rows),
        "agent_issues": agent_issues,
        "agent_attention": len(agent_issues),
        **counts,
    }


def printer_issue_queue(db: Session, limit: int = 12) -> dict:
    """Affected printers, grouped once and ordered by operational impact.

    A printer is one dashboard task even when three rules describe it. Grouping
    keeps the overview from becoming an alert-log duplicate while retaining the
    total through ``more_count``. Printers reporting ``offline`` or ``error``
    are included even before the worker opens an Alert, so the dashboard never
    waits a cycle to show a device that cannot print.
    """
    live_alerts = list(
        db.scalars(
            select(m.Alert).where(
                m.Alert.state != m.AlertState.resolved,
                m.Alert.printer_id.is_not(None),
            )
        )
    )
    status_printers = list(
        db.scalars(
            select(m.Printer).where(
                m.Printer.discovery_state == m.DiscoveryState.approved,
                m.Printer.status.in_([m.PrinterStatus.offline, m.PrinterStatus.error]),
            )
        )
    )
    printer_ids = {a.printer_id for a in live_alerts if a.printer_id is not None}
    printer_ids.update(p.id for p in status_printers)
    if not printer_ids:
        return {"rows": [], "total": 0}

    printers = {
        p.id: p
        for p in db.scalars(select(m.Printer).where(m.Printer.id.in_(printer_ids)))
    }
    site_ids = {p.site_id for p in printers.values()}
    client_ids = {p.client_id for p in printers.values()}
    sites = {
        row.id: row
        for row in db.scalars(select(m.Site).where(m.Site.id.in_(site_ids)))
    }
    clients = {
        row.id: row
        for row in db.scalars(select(m.Client).where(m.Client.id.in_(client_ids)))
    }
    alerts_by_printer: dict = {}
    for alert in live_alerts:
        alerts_by_printer.setdefault(alert.printer_id, []).append(alert)

    severity_rank = {
        m.EventSeverity.critical: 0,
        m.EventSeverity.warning: 1,
        m.EventSeverity.info: 2,
    }
    rows = []
    for printer_id in printer_ids:
        printer = printers.get(printer_id)
        if printer is None:
            continue
        alerts = alerts_by_printer.get(printer_id, [])
        blocking = printer.status in (m.PrinterStatus.offline, m.PrinterStatus.error)

        # A current device state is stronger than an older rule title. Put it at
        # the front so "Printer is offline" cannot be hidden behind "Toner low".
        status_title = None
        if printer.status == m.PrinterStatus.offline:
            status_title = "Printer is offline"
        elif printer.status == m.PrinterStatus.error:
            status_title = "Printer reports an error"

        ordered_alerts = sorted(
            alerts,
            key=lambda a: (
                0 if a.type == m.AlertConditionType.printer_offline else 1,
                severity_rank.get(a.severity, 9),
                _aware_utc(a.created_at) or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
        primary = ordered_alerts[0] if ordered_alerts else None
        if status_title is not None:
            title = status_title
            detail = primary.title if primary is not None else ""
            severity = m.EventSeverity.critical
            issue_count = len(alerts) + 1
            hidden_count = max(0, len(alerts) - 1)
        elif primary is not None:
            title = primary.title
            detail = primary.detail or ""
            severity = primary.severity
            issue_count = len(alerts)
            hidden_count = max(0, len(alerts) - 1)
            blocking = blocking or primary.type == m.AlertConditionType.printer_offline
            # A technician choosing "Printing stopped" creates a critical
            # manual issue before the next device poll can confirm a status
            # change. Treat that explicit observation as operational truth so
            # it is not demoted to "Needs attention" on the overview.
            blocking = blocking or (
                primary.type == m.AlertConditionType.manual_issue
                and primary.severity == m.EventSeverity.critical
            )
        else:
            continue

        site = sites.get(printer.site_id)
        client = clients.get(printer.client_id)
        rows.append(
            {
                "printer_id": printer.id,
                "printer_name": (
                    printer.display_name or printer.model or printer.hostname or printer.ip
                ),
                "model": printer.model or "Unknown model",
                "ip": printer.ip,
                "client_name": client.name if client is not None else "Unknown client",
                "site_name": site.name if site is not None else "Unknown site",
                "location": printer.location or "",
                "title": title,
                "detail": detail,
                "severity": severity.value,
                "tone": "error" if severity == m.EventSeverity.critical else "warning",
                "blocking": blocking,
                "impact_label": "Printing stopped" if blocking else "Needs attention",
                "issue_count": issue_count,
                "more_count": hidden_count,
            }
        )

    rows.sort(
        key=lambda row: (
            0 if row["blocking"] else 1,
            0 if row["severity"] == m.EventSeverity.critical.value else 1,
            row["client_name"].casefold(),
            row["site_name"].casefold(),
            row["printer_name"].casefold(),
        )
    )
    return {"rows": rows[: max(0, limit)], "total": len(rows)}


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

    **Five grouped queries, not five per client.** This was 5N+1 -- four counts
    in the loop plus a lazy ``client.sites`` load, which is the one that does not
    grep as a query at all -- so the Overview page cost 1,002 round trips at 200
    clients (measured: 1002 statements, 776ms against Postgres on a 200-client /
    4,000-printer fleet). Each count is now a single ``GROUP BY client_id``
    aggregate and the per-client numbers are read out of dicts, so the statement
    count is **constant** in the size of the fleet.

    Deliberately four aggregates rather than one join: joining ``printers`` to
    both ``alerts`` and ``supplies`` in one statement multiplies the rows and
    every count comes back wrong (a printer with 4 supplies and 2 alerts would
    be counted 8 times). Printer and offline counts DO share a statement --
    same table, same scope -- via conditional aggregation.

    A client with nothing at all is absent from every aggregate, so each lookup
    defaults to 0; the client list is what decides which rows exist, exactly as
    before.
    """
    low_pct = low_supply_threshold(db)
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    if not clients:
        return []

    approved = m.Printer.discovery_state == m.DiscoveryState.approved

    # Printers + offline/error printers: one scan of `printers`, two numbers.
    # `sum(case(...))` rather than `count(...) FILTER`, which SQLite lacks.
    printer_rows = db.execute(
        select(
            m.Printer.client_id,
            func.count(m.Printer.id),
            func.sum(
                case(
                    (
                        m.Printer.status.in_(
                            [m.PrinterStatus.offline, m.PrinterStatus.error]
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
        )
        .where(approved)
        .group_by(m.Printer.client_id)
    ).all()
    printer_counts = {cid: (total or 0) for cid, total, _off in printer_rows}
    offline_counts = {cid: int(off or 0) for cid, _total, off in printer_rows}

    # Open alerts join through Printer because Alert.printer_id may be null
    # for agent-scope alerts that aren't a per-client signal.
    alert_counts = dict(db.execute(
        select(m.Printer.client_id, func.count(m.Alert.id))
        .select_from(m.Alert)
        .join(m.Printer, m.Printer.id == m.Alert.printer_id)
        .where(m.Alert.state == m.AlertState.open)
        .group_by(m.Printer.client_id)
    ).all())

    supply_counts = dict(db.execute(
        select(m.Printer.client_id, func.count(m.Supply.id))
        .select_from(m.Supply)
        .join(m.Printer, m.Printer.id == m.Supply.printer_id)
        .where(
            approved,
            m.Supply.level_pct.is_not(None),
            m.Supply.level_pct <= low_pct,
            # Same rule as low_supplies: a waste box reports how FULL it is, so
            # 5% is nearly empty, not nearly exhausted. Ported here from the
            # per-client loop this aggregate replaced -- dropping it would have
            # made the N+1 fix silently reintroduce the false positive.
            not_(receptacle_supply_clause()),
        )
        .group_by(m.Printer.client_id)
    ).all())

    # `len(client.sites)` loaded every Site row of every client to count them.
    site_counts = dict(db.execute(
        select(m.Site.client_id, func.count(m.Site.id)).group_by(m.Site.client_id)
    ).all())

    return [
        {
            "client": client,
            "printer_count": printer_counts.get(client.id, 0),
            "offline_count": offline_counts.get(client.id, 0),
            "open_alerts": alert_counts.get(client.id, 0),
            "low_supplies": supply_counts.get(client.id, 0),
            "sites_count": site_counts.get(client.id, 0),
        }
        for client in clients
    ]


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


def snmp_grade(version: str, snmp_v3: Optional[dict]) -> str:
    """Grade the SNMP transport from the version plus the USM security level.

    v1/v2c are cleartext by definition. For v3 the version says nothing on its
    own -- the USM security level does -- and an absent level is not an unknown:
    the agent substitutes noAuthNoPriv for it, so the session really is
    unauthenticated and unencrypted. Treating it as cleartext is a statement
    about what this system actually does, not a guess about the device.
    """
    if version in INSECURE_SNMP_VERSIONS:
        return SNMP_GRADE_CLEARTEXT
    level = ((snmp_v3 or {}).get("security_level") or "").strip().lower()
    if level == "authpriv":
        return SNMP_GRADE_ENCRYPTED
    if level == "authnopriv":
        return SNMP_GRADE_AUTHENTICATED
    return SNMP_GRADE_CLEARTEXT


def _subnet_snmp_config_for(printer: m.Printer, subnets: list[m.Subnet]) -> dict:
    """Effective SNMP config for a printer, derived from its SUBNET row.

    The anchor signal for the posture report is "what SNMP version does this
    device actually talk over", which is owned by the subnet the printer sits
    in (each subnet row carries its own creds). We match the printer's IP
    against the CIDRs of the subnets in its own site; the matching subnet's
    config wins. Falls back to the printer's own columns when no subnet
    contains the IP (e.g. a manually-added device, or an IP outside any
    enrolled CIDR).

    Returns ``{"version", "source", "community", "snmp_v3"}`` where ``source``
    is the subnet label/cidr or "printer", so the UI can show where the
    determination came from. The community string itself is never rendered or
    exported -- only whether it is a factory default -- because it is the
    credential this whole finding is about.
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
                return {
                    "version": _normalize_snmp_version(sub.snmp_version),
                    "source": sub.label or sub.cidr,
                    "community": sub.snmp_community,
                    "snmp_v3": sub.snmp_v3,
                }
    return {
        "version": _normalize_snmp_version(printer.snmp_version),
        "source": "printer",
        "community": printer.snmp_community,
        "snmp_v3": printer.snmp_v3,
    }


def security_posture_rollup(db: Session, client_id: Optional[int] = None) -> dict:
    """Per-device security posture + a fleet summary -- "treat printers like
    endpoints".

    Grounded entirely in data we already hold, and careful about the difference
    between the two kinds of thing it holds:

      * SNMP transport (a FINDING) -- graded from the subnet's version + USM
        security level. This describes the monitoring path we configured, so we
        can assert it outright rather than infer it from the device.
      * Default community string (a FINDING) -- the credential in our own
        subnet row is still the factory default.
      * Firmware (VISIBILITY, not a finding) -- best-effort version string
        captured during polling; honestly ``None`` -> "unknown" when the device
        exposes nothing parseable, never fabricated.

    Firmware-unknown is deliberately NOT a ``flag``. It says we cannot see
    something, not that something is wrong, and folding a visibility gap into
    the same red count as a real exposure is how a security report becomes
    noise. It has its own column and its own summary counter instead.

    Posture is COMPUTED on read (not denormalized): the SNMP grade follows the
    live subnet config, so a row would otherwise go stale the moment an
    operator flips a subnet to v3 authPriv. Firmware is the only stored input
    and it's a fact the agent collected, not a derived verdict.

    Returns ``{"rows": [...], "summary": {...}}`` scoped to ``client_id`` when
    given. Each row: printer, client, site, snmp_version, snmp_grade,
    snmp_secure (bool), snmp_source, snmp_default_community (bool), firmware
    (str|None), firmware_known (bool), flags (list[str]).
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
    cleartext_count = 0
    authenticated_count = 0
    encrypted_count = 0
    default_community_count = 0
    unknown_fw_count = 0
    for printer in printers:
        cfg = _subnet_snmp_config_for(printer, subnets)
        version = cfg["version"]
        grade = snmp_grade(version, cfg["snmp_v3"])
        secure = grade != SNMP_GRADE_CLEARTEXT
        firmware = (printer.firmware or "").strip() or None
        firmware_known = firmware is not None
        # Only meaningful for v1/v2c: v3 has no community string at all, so
        # asking whether a v3 subnet's leftover community is "public" would
        # raise a finding about a credential nothing uses.
        default_community = (
            version in INSECURE_SNMP_VERSIONS
            and (cfg["community"] or "").strip().lower() in DEFAULT_SNMP_COMMUNITIES
        )

        flags: list[str] = []
        if grade == SNMP_GRADE_CLEARTEXT:
            flags.append(FLAG_SNMP_CLEARTEXT)
            cleartext_count += 1
        elif grade == SNMP_GRADE_AUTHENTICATED:
            authenticated_count += 1
        else:
            encrypted_count += 1
        if default_community:
            flags.append(FLAG_SNMP_DEFAULT_COMMUNITY)
            default_community_count += 1
        if not firmware_known:
            unknown_fw_count += 1

        rows.append({
            "printer": printer,
            "client": clients.get(printer.client_id),
            "site": sites.get(printer.site_id),
            "snmp_version": version,
            "snmp_grade": grade,
            "snmp_secure": secure,
            "snmp_source": cfg["source"],
            "snmp_default_community": default_community,
            "firmware": firmware,
            "firmware_known": firmware_known,
            "flags": flags,
        })

    summary = {
        "total": len(rows),
        "snmp_cleartext": cleartext_count,
        "snmp_authenticated": authenticated_count,
        "snmp_encrypted": encrypted_count,
        "snmp_default_community": default_community_count,
        "firmware_unknown": unknown_fw_count,
        "firmware_known": len(rows) - unknown_fw_count,
        # Devices carrying at least one SECURITY finding. Firmware visibility is
        # counted separately and on purpose -- see the docstring.
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


AUDIT_PAGE_SIZE = 100

# Which columns the audit filter searches. Deliberately not `detail`: it is
# free-form text holding whole rendered alert bodies, so including it makes an
# unanchored LIKE scan the widest column in the table for a filter whose stated
# purpose is "what did tech2 touch". Same three columns the shipped filter used.
_AUDIT_SEARCH_FIELDS = ("action", "target", "username")


def audit_page(
    db: Session, q: str = "", page: int = 1, per_page: int = AUDIT_PAGE_SIZE
) -> dict:
    """One page of the audit trail, newest first, optionally filtered by ``q``.

    The audit trail is the compliance surface -- every login, settings change,
    approval, backup download -- and it shipped as a bare ``LIMIT 200`` with no
    offset. At 100,000 rows that made 99,800 of them **permanently unreachable
    through any URL**: not slow to reach, unreachable. A truncated audit trail is
    worse than no audit trail, because it looks complete.

    The filter is applied **in SQL before the LIMIT**, which is the whole
    correctness point and not an optimisation. The inverse -- take a page and
    then narrow it -- is the bug this codebase has already shipped once in the
    customer portal (thirty newest alerts fleet-wide, then keep the tenant's, so
    a customer with a live fault was told "no open issues"). Here it would mean
    an operator filtering for ``login.failed`` seeing "no entries" whenever the
    200 newest rows happened to be something else.

    Two properties worth not undoing:

    * **The order is strictly total** -- ``ts DESC, id DESC``, not ``ts`` alone.
      ``ts`` is not unique (a settings save writes several rows inside one
      clock tick), and OFFSET paging over a non-deterministic order repeats one
      row on page 2 and skips another entirely -- silent gaps in the trail that
      is supposed to be the record of record.
    * **The pattern is escaped.** The shipped filter interpolated the operator's
      text straight into ``%...%``, so a ``%`` matched everything and a ``_``
      matched any character. That fails *open* on a security surface: a filter
      that quietly returns more than it was asked for reads as "nothing was
      narrowed", which is how somebody concludes an action was not logged.

    Returns the same envelope as ``search_printers``:
    ``{"rows", "total", "page", "pages", "per_page", "offset", "q"}``.
    """
    term = (q or "").strip()[:MAX_SEARCH_TERM]
    per_page = max(1, min(int(per_page or AUDIT_PAGE_SIZE), _MAX_PAGE_SIZE))

    filters = []
    if term:
        pattern = _like_contains(term)
        filters.append(
            or_(
                *[
                    getattr(m.AuditLog, field).ilike(pattern, escape=_LIKE_ESCAPE)
                    for field in _AUDIT_SEARCH_FIELDS
                ]
            )
        )

    total = db.scalar(
        select(func.count()).select_from(m.AuditLog).where(*filters)
    ) or 0
    pages = max(1, (total + per_page - 1) // per_page)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1
    # Clamped, not 404'd -- same rule as the fleet list. A bookmark into a trail
    # that has since been pruned should land on the last page, not an error.
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page

    rows = list(db.scalars(
        select(m.AuditLog)
        .where(*filters)
        .order_by(m.AuditLog.ts.desc(), m.AuditLog.id.desc())
        .limit(per_page)
        .offset(offset)
    ))

    return {
        "rows": rows,
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


def positive_delta(values: "Iterable[Optional[int]]") -> int:
    """Sum positive steps across one cumulative meter series (oldest→newest).

    We only add a delta when it's positive, so a counter reset (firmware
    reflash) or a printer swap onto the same DB row (the meter drops to a
    smaller absolute number) contributes 0 for that step instead of a large
    NEGATIVE number that would cancel out real prints. The trade-off — pages
    printed between the last reading before a reset and the reset itself are
    lost — is the safe direction for anything billing-adjacent: never invent
    prints, never go negative.

    ``None`` entries are skipped rather than treated as zero: a poll that
    reported no meter is silence, not a reading of nought.
    """
    total = 0
    prev: Optional[int] = None
    for value in values:
        if value is None:
            continue
        if prev is not None and value > prev:
            total += value - prev
        prev = value
    return total


def _printed_pages_for_printer(rows: "list[tuple[int]]") -> int:
    """``positive_delta`` over ``(page_count,)`` tuples, as the ESG rollup reads them."""
    return positive_delta(pc for (pc,) in rows)


#: One printer's metered work over a period. Every field is ``Optional``: a
#: meter the device never reported in the window is **unknown**, which is not
#: the same fact as zero and must never be billed as zero. See
#: ``central.billing`` for what each combination means commercially.
PeriodMeters = namedtuple("PeriodMeters", "pages mono color")

# The three cumulative meters, and the Reading column each one lives in. Kept as
# data so adding a fourth meter later is a line here rather than three more
# copies of the same delta loop.
_METER_COLUMNS = (
    ("pages", m.Reading.page_count),
    ("mono", m.Reading.mono_count),
    ("color", m.Reading.color_count),
)


def _meter_rows(
    db: Session, printer_id: int, start: datetime, end: datetime
) -> "list[tuple]":
    """The in-window meter series for one printer, oldest→newest.

    **The one place the billing period reads its source.** Today that is the raw
    ``readings`` table. When the daily rollup lands it preserves ``page_count`` /
    ``mono_count`` / ``color_count`` per printer per day, which is the same shape
    this returns — a time-ordered series of *cumulative* meter values — so the
    rollup can be unioned in here (raw rows inside the retention window, rolled
    up rows before it) without the delta arithmetic above changing at all. That
    is deliberately the only coupling: nothing else in the billing path knows
    where a row came from, and none of it references the rollup table, so this
    works whether or not that table exists yet.
    """
    stmt = (
        select(m.Reading.page_count, m.Reading.mono_count, m.Reading.color_count)
        .where(
            m.Reading.printer_id == printer_id,
            m.Reading.ts >= start,
            m.Reading.ts < end,
        )
        .order_by(m.Reading.ts.asc(), m.Reading.id.asc())
    )
    return list(db.execute(stmt))


def _meter_baseline(
    db: Session, printer_id: int, column, start: datetime
) -> Optional[int]:
    """The last value this meter held *before* the period opened, if any.

    Seeding the delta with it is what makes consecutive periods add up to the
    lifetime total. Without it, every period silently discards the pages printed
    between its first reading and the period boundary — one poll interval's
    worth at each month end, and rather more for a printer that was asleep.

    It deliberately does **not** make the meter "known": known-ness is measured
    strictly inside the window, so a device that stopped reporting its colour
    meter this month reads as unknown rather than as a suspiciously round zero.
    """
    stmt = (
        select(column)
        .where(
            m.Reading.printer_id == printer_id,
            m.Reading.ts < start,
            column.is_not(None),
        )
        .order_by(m.Reading.ts.desc(), m.Reading.id.desc())
        .limit(1)
    )
    return db.scalar(stmt)


def period_meters(
    db: Session, printer_id: int, start: datetime, end: datetime
) -> PeriodMeters:
    """Pages printed by one printer in ``[start, end)``, per meter, reset-safe.

    Returns ``None`` for any meter the device did not report inside the window,
    and an ``int`` (possibly 0) for one it did. That distinction is the whole
    point: blank means "we do not know", 0 means "we know it printed nothing".

    Cost: one windowed scan plus at most one ``LIMIT 1`` baseline lookup per
    meter that is actually present -- so a mono-only device costs three queries,
    not four. Per printer, like the ESG rollup next door. On a large fleet the
    right fix is the daily rollup (fewer rows per printer), not batching the
    baseline into a window function that only one backend has.
    """
    rows = _meter_rows(db, printer_id, start, end)
    out = {}
    for offset, (name, column) in enumerate(_METER_COLUMNS):
        values = [row[offset] for row in rows]
        if all(v is None for v in values):
            out[name] = None
            continue
        baseline = _meter_baseline(db, printer_id, column, start)
        series = values if baseline is None else [baseline] + values
        out[name] = positive_delta(series)
    return PeriodMeters(**out)


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
