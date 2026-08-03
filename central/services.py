"""Reusable domain logic shared by the API, the seed script, and tests.

Keeping the "apply a reading" path here (rather than inline in a router) means the
seed script and unit tests exercise exactly the same code the agent ingest uses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from central import audit
from central import models as m
from central import schemas as s


def _now() -> datetime:
    return datetime.now(timezone.utc)


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


def find_printer_in_sites(
    db: Session, site_ids: set[int], ip: str, serial: Optional[str] = None
) -> Optional[m.Printer]:
    """Resolve a device to its Printer row, preferring serial over IP.

    IP is a location, not an identity. Matching on it alone gets both halves of
    DHCP churn wrong:

      * A printer that changes lease looks brand new, so a second row is
        created and the first is stranded -- still ``approved``, still polled,
        its status and last_seen frozen at their last-good values. Page meters
        then double-count across the two rows and corrupt billing.
      * Worse in the other direction: a *different* printer that inherits a
        recycled IP silently adopts the old row, along with its id, reading
        history, page counts, asset tag and maintenance records.

    So: match on serial first (scoped to the sites this agent serves), and only
    fall back to IP when the device reports no serial or no serial matches.
    A row found by IP whose serial contradicts the device is deliberately NOT
    returned -- the caller creates a new row instead, which keeps the two
    devices' histories apart. Rows with no serial recorded yet still match by
    IP, so existing fleets keep working and get their serial backfilled.
    """
    if not site_ids:
        return None

    serial = (serial or "").strip() or None
    if serial:
        by_serial = db.scalar(
            select(m.Printer).where(
                m.Printer.site_id.in_(site_ids), m.Printer.serial == serial
            )
        )
        if by_serial is not None:
            return by_serial

    by_ip = find_printer_by_ip_in_sites(db, site_ids, ip)
    if by_ip is not None and serial and by_ip.serial and by_ip.serial != serial:
        return None  # recycled IP, different hardware -- don't inherit its history
    return by_ip


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
    agent path -- one agent serves several sites). The printer is resolved by
    serial where the device reports one, falling back to IP -- see
    ``find_printer_in_sites`` for why IP alone is unsafe. This matters more here
    than in discovery: readings carry the page meters, so binding one to the
    wrong row corrupts billing rather than just the device list.

    Returns the printer, or None if no matching approved printer exists at the
    site(s) (discovery happens via the separate /discovered endpoint, not here).
    """
    site_ids = set(site_id) if isinstance(site_id, (set, list, tuple, frozenset)) else {site_id}
    printer = find_printer_in_sites(db, site_ids, reading.ip, reading.serial)
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

    # Workstation driver tier. Only written when the agent actually probed:
    # probing is throttled and older agents never send it, so a reading without
    # these fields carries no information about the tier. Treating absent as
    # "unknown" would blank a printer that probed cleanly last week every time a
    # routine SNMP poll came in.
    #
    # driver_tier_override is deliberately untouched here. It is the operator's
    # decision, and the whole point of keeping it in its own column is that a
    # re-probe can refresh what we *observed* without discarding what a human
    # *decided* -- so the UI can still show both and explain the difference.
    if reading.driver_tier is not None:
        printer.driver_tier = reading.driver_tier
        printer.driver_tier_reason = reading.driver_tier_reason
        printer.driver_probed_at = ts
        # Endpoint and capabilities are only meaningful when something answered;
        # a device that has gone unreachable keeps its last known good endpoint
        # rather than having it blanked, which is what an operator needs to see
        # when diagnosing why it stopped answering.
        if reading.ipp_endpoint:
            printer.ipp_endpoint = reading.ipp_endpoint
        if reading.ipp_capabilities is not None:
            printer.ipp_capabilities = reading.ipp_capabilities

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

    Devices found on a subnet an operator marked ``trusted`` skip the approvals
    queue and land approved. Everything else queues -- including a device whose
    subnet_cidr resolves to no enrolled Subnet row, and a legacy agent that
    reports no CIDR at all, because unknown provenance must never mean approved.

    Auto-approval applies only to a row created *here*. Marking a subnet trusted
    does not retroactively sweep a backlog, because those rows may already carry
    a decision a human made; changing one silently is the failure mode this
    whole gate exists to avoid. Clearing a backlog is what bulk approve is for.
    """
    target_site_id = agent.site_id
    sub = None
    if device.subnet_cidr:
        sub = find_subnet_for_agent_cidr(db, agent.id, device.subnet_cidr)
        if sub is not None:
            target_site_id = sub.site_id

    existing = find_printer_in_sites(
        db, sites_served_by_agent(db, agent), device.ip, device.serial
    )
    if existing is not None:
        # Refresh identity but never downgrade an approved/ignored device to pending.
        for attr in ("mac", "hostname", "brand", "model", "serial", "firmware"):
            val = getattr(device, attr)
            if val and not getattr(existing, attr):
                setattr(existing, attr, val)
        # Matched by serial from a new address: the printer moved (DHCP lease,
        # re-cabling). Follow it rather than leaving a stale row polling a dead
        # address -- unless another row already holds that address in the same
        # site, which would break the (site_id, ip) uniqueness. That collision
        # means there are genuinely two rows for one device; leave both intact
        # for the operator to merge rather than guessing here.
        if existing.ip != device.ip:
            clash = find_printer_by_ip_in_sites(db, {existing.site_id}, device.ip)
            if clash is None:
                existing.ip = device.ip
        return existing, False

    site = db.get(m.Site, target_site_id)
    auto_approved = bool(sub is not None and sub.trusted)
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
        discovery_state=(
            m.DiscoveryState.approved if auto_approved else m.DiscoveryState.pending
        ),
        status=m.PrinterStatus.unknown,
    )
    db.add(printer)
    if auto_approved:
        # Auto-approval is the one path that puts a device into a tenant's fleet
        # with no human in the loop, so it is recorded rather than merely
        # happening. No Request and no User: the actor is the agent, and saying
        # so beats attributing it to whoever last logged in. Identity fields are
        # device-supplied, so only the IP (validated on the way in) goes in the
        # target and the rest stays out of the audit string entirely.
        audit.record(
            db, None, None, "printer.auto_approve",
            target=f"printer:{device.ip} site:{target_site_id}",
            detail=(
                f"discovered by agent {agent.id} on trusted subnet "
                f"{sub.cidr} (subnet:{sub.id})"
            ),
        )
    return printer, True


# --------------------------------------------------------------------------- #
# Agent claim codes: mint, redeem, expire
# --------------------------------------------------------------------------- #
# Default lifetime of a minted code. Short on purpose: the code is the thing
# that travels (chat message, ticket, runbook), so its window of usefulness to
# anyone who intercepts it should be the install itself, not the week around it.
CLAIM_CODE_TTL_MINUTES = 60
CLAIM_CODE_MAX_TTL_MINUTES = 60 * 24 * 7


def mint_claim_token(
    db: Session,
    *,
    site_id: int,
    agent_name: str,
    ttl_minutes: int = CLAIM_CODE_TTL_MINUTES,
    created_by_user_id: Optional[int] = None,
) -> tuple[m.AgentClaimToken, str]:
    """Create a claim code for ``site_id``. Returns (row, plaintext code).

    The plaintext is returned to the caller and never stored -- same contract as
    agent API keys, so the UI must show it once and mean it.
    """
    from central.security import generate_claim_code, hash_claim_code

    ttl = max(1, min(int(ttl_minutes), CLAIM_CODE_MAX_TTL_MINUTES))
    code = generate_claim_code()
    token = m.AgentClaimToken(
        token_hash=hash_claim_code(code),
        site_id=site_id,
        agent_name=(agent_name or "").strip() or None,
        created_by_user_id=created_by_user_id,
        expires_at=_now() + timedelta(minutes=ttl),
    )
    db.add(token)
    return token, code


def redeem_claim_token(
    db: Session, code: str, *, hostname: Optional[str] = None
) -> Optional[tuple[m.Agent, str]]:
    """Exchange a claim code for a real agent + API key. Returns None if the
    code is unknown, expired, or already redeemed -- the caller must not
    distinguish those to the client.

    Single use is enforced as a conditional UPDATE rather than read-then-write:
    two agents booting from the same baked image would otherwise both pass the
    check and both get an agent. The UPDATE's rowcount is the interlock -- only
    the transaction that actually flipped ``used_at`` from NULL proceeds.
    """
    from sqlalchemy import update

    from central.security import (
        generate_api_key,
        hash_api_key,
        hash_claim_code,
    )

    now = _now()
    result = db.execute(
        update(m.AgentClaimToken)
        .where(
            m.AgentClaimToken.token_hash == hash_claim_code(code or ""),
            m.AgentClaimToken.used_at.is_(None),
            m.AgentClaimToken.expires_at > now,
        )
        .values(used_at=now)
    )
    if result.rowcount != 1:
        return None

    token = db.scalar(
        select(m.AgentClaimToken).where(
            m.AgentClaimToken.token_hash == hash_claim_code(code or "")
        )
    )
    if token is None:  # pragma: no cover - the UPDATE just matched it
        return None

    site = db.get(m.Site, token.site_id)
    if site is None:
        # The site was deleted between mint and redeem. The code is already
        # burned by the UPDATE above, which is the right way round: fail closed
        # and make the operator mint a new one rather than guessing a home.
        return None

    # The hostname is machine-reported and therefore untrusted: it only ever
    # decorates the operator's chosen name, never replaces it, and is length
    # -capped so a hostile agent can't push a novel into the UI.
    base = token.agent_name or f"Agent for {site.name}"
    clean_host = (hostname or "").strip()[:60]
    name = f"{base} ({clean_host})" if clean_host else base

    api_key = generate_api_key()
    agent = m.Agent(
        site_id=token.site_id,
        name=name[:200],
        api_key_hash=hash_api_key(api_key),
    )
    db.add(agent)
    db.flush()
    token.used_by_agent_id = agent.id

    # Adopt this site's orphaned subnets. With claim-code enrollment the subnet
    # is declared before the agent exists (that is the point -- the operator
    # sets the site up, then the box shows up and enrolls itself), so without
    # this the agent would come online with nothing to scan and the operator
    # would have to go back and wire them up by hand, which is the manual step
    # this whole path exists to remove.
    #
    # Strictly ``agent_id IS NULL`` and strictly this site: an unassigned subnet
    # has no owner to take it from, and never touching an assigned one means a
    # second agent at the same site cannot silently steal the first one's work.
    orphans = list(db.scalars(
        select(m.Subnet).where(
            m.Subnet.site_id == token.site_id, m.Subnet.agent_id.is_(None)
        )
    ))
    for subnet in orphans:
        subnet.agent_id = agent.id
    if orphans:
        audit.record(
            db, None, None, "subnet.auto_assign",
            target=f"agent:{agent.id} site:{token.site_id}",
            detail="adopted on enrollment: " + ", ".join(s.cidr for s in orphans[:20]),
        )
    return agent, api_key


def purge_expired_claim_tokens(db: Session) -> int:
    """Delete claim tokens that are spent or long past their window.

    Redeemed rows are kept briefly so the Agents page can show "claimed at" and
    the audit trail has something to point at; beyond that they are noise, and a
    table of dead credentials is a table worth not having.
    """
    from sqlalchemy import delete, or_

    cutoff = _now() - timedelta(days=7)
    result = db.execute(
        delete(m.AgentClaimToken).where(
            or_(
                m.AgentClaimToken.expires_at < cutoff,
                m.AgentClaimToken.used_at < cutoff,
            )
        )
    )
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# Onboarding defaults
# --------------------------------------------------------------------------- #
def _parse_quiet_hours(raw: str) -> Optional[tuple]:
    """"HH:MM-HH:MM" -> (start_minute, end_minute), or None if unparseable.

    Returns None rather than raising: a malformed setting should cost the new
    client its quiet-hours window, not block onboarding entirely.
    """
    try:
        start_s, end_s = str(raw or "").strip().split("-", 1)
        sh, sm = (int(p) for p in start_s.strip().split(":", 1))
        eh, em = (int(p) for p in end_s.strip().split(":", 1))
    except (AttributeError, TypeError, ValueError):
        return None
    if not (0 <= sh < 24 and 0 <= eh < 24 and 0 <= sm < 60 and 0 <= em < 60):
        return None
    return sh * 60 + sm, eh * 60 + em


def _parse_weekdays(raw: str) -> list:
    days = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 6 and int(part) not in days:
            days.append(int(part))
    return days or [0, 1, 2, 3, 4, 5, 6]


def apply_onboarding_defaults(db: Session, client: m.Client, runtime: dict) -> list:
    """Give a freshly created client its starting rules. Returns what was made.

    Applied once, at creation, and never again: these are a starting point the
    client then owns. Re-applying on every settings change would quietly
    overwrite thresholds an operator tuned for that customer, which is the
    behaviour that makes people stop trusting defaults.

    The return value is what the caller audits -- "we created these three
    things on your behalf" is the difference between a helpful default and an
    unexplained row appearing in someone's alert rules.
    """
    created = []
    if not runtime.get("onboarding.apply_defaults"):
        return created

    threshold = int(runtime.get("onboarding.low_supply_pct") or 0)
    if threshold > 0:
        db.add(m.AlertRule(
            name=f"{client.name}: low supply",
            scope=m.AlertScope.client,
            scope_id=client.id,
            condition_type=m.AlertConditionType.supply_below,
            threshold=float(threshold),
            severity=m.EventSeverity.warning,
        ))
        created.append(f"low-supply rule at {threshold}%")

    window = _parse_quiet_hours(runtime.get("onboarding.quiet_hours") or "")
    if window:
        start_minute, end_minute = window
        db.add(m.SuppressionWindow(
            name=f"{client.name}: quiet hours",
            kind=m.SuppressionKind.quiet_hours,
            action=m.SuppressionAction.defer,
            scope=m.AlertScope.client,
            scope_id=client.id,
            start_minute=start_minute,
            end_minute=end_minute,
            weekdays=_parse_weekdays(runtime.get("onboarding.quiet_hours_weekdays") or ""),
        ))
        created.append(
            f"quiet hours {start_minute // 60:02d}:{start_minute % 60:02d}"
            f"-{end_minute // 60:02d}:{end_minute % 60:02d} (client-local)"
        )
    return created


# --------------------------------------------------------------------------- #
# End users + printer assignment
# --------------------------------------------------------------------------- #
class TenancyError(ValueError):
    """A cross-tenant reference was attempted.

    Its own type, not a bare ValueError, because callers must be able to tell
    "you sent me a bad field" from "you tried to reach into another customer's
    fleet". The first is a form error; the second is a security event worth
    auditing and worth never rendering as a routine validation message.
    """


def assign_printer(
    db: Session,
    *,
    printer: m.Printer,
    end_user: Optional[m.EndUser] = None,
    group: Optional[m.EndUserGroup] = None,
    machine: Optional[m.Machine] = None,
    is_default: bool = False,
    operator_id: Optional[int] = None,
) -> m.PrinterAssignment:
    """Assign a printer to one end user, one group, or one machine -- same tenant.

    This function exists to own the invariant the schema cannot state: the
    printer and its target must belong to the same client. A multi-tenant
    product where an operator can attach customer A's printer to customer B's
    staff is not merely wrong, it leaks the existence and identity of one
    customer's hardware into another's UI -- so the check is here, in the one
    place every caller has to pass through, rather than repeated per route.

    Machines go through this same function rather than a parallel one, precisely
    because of the paragraph above: a second implementation is a second place the
    tenancy rule can be got wrong, and the one that drifts will be the one nobody
    remembered existed.

    Re-assigning an existing pair is idempotent and updates ``is_default``,
    because the natural operator gesture (tick the box again) should not be a
    unique-constraint 500.
    """
    targets = [t for t in (end_user, group, machine) if t is not None]
    if len(targets) != 1:
        raise ValueError(
            "assign_printer needs exactly one of end_user, group or machine"
        )

    target_client_id = targets[0].client_id
    if target_client_id != printer.client_id:
        raise TenancyError(
            f"printer {printer.id} belongs to client {printer.client_id}, "
            f"target belongs to client {target_client_id}"
        )

    existing = db.scalar(
        select(m.PrinterAssignment).where(
            m.PrinterAssignment.printer_id == printer.id,
            m.PrinterAssignment.end_user_id == (end_user.id if end_user else None),
            m.PrinterAssignment.group_id == (group.id if group else None),
            m.PrinterAssignment.machine_id == (machine.id if machine else None),
        )
    )
    if existing is not None:
        existing.is_default = is_default
        return existing

    row = m.PrinterAssignment(
        printer_id=printer.id,
        end_user_id=end_user.id if end_user else None,
        group_id=group.id if group else None,
        machine_id=machine.id if machine else None,
        is_default=is_default,
        created_by_user_id=operator_id,
    )
    db.add(row)
    db.flush()
    return row


def effective_printers_for(
    db: Session, end_user: m.EndUser, machine: Optional[m.Machine] = None
) -> list:
    """Every printer this person should have, direct + inherited from groups.

    Returns ``[(printer, is_default, via_group_name_or_None), ...]`` ordered by
    printer id, with duplicates collapsed.

    Two resolution rules, both deliberate:

    * **A direct assignment outranks a machine one, which outranks a group one.**
      Somebody singled this person out; that is more specific than "the PC by
      the warehouse door", which is in turn more specific than "they are in
      Accounting". A machine flagged ``default_wins`` inverts the first pair --
      what a shared floor terminal or kiosk needs, where the person is standing
      at the machine and its printer is the right one whatever their group says.
      Per-machine rather than global, because one site routinely has both kinds.
    * **Exactly one default, chosen deterministically.** A person in three
      groups can easily be handed three "make this default" flags. Rather than
      let the last writer win (which makes the workstation's default printer
      depend on dictionary ordering), direct assignments win first, then the
      lowest printer id. Boring and stable beats clever and irreproducible --
      an operator asking "why is that their default?" gets an answer.

    Inactive users resolve to nothing. A deprovisioned person keeps their rows
    for audit, but must not keep their printers. The same goes for a retired
    machine.

    A machine belonging to a different client than the user contributes nothing.
    That is enforced here rather than left to the caller: tenancy spans three
    tables, so the resolver refuses instead of trusting that somebody upstream
    already checked.
    """
    if not end_user.active:
        return []

    if machine is not None and (
        not machine.active or machine.client_id != end_user.client_id
    ):
        machine = None

    direct = db.scalars(
        select(m.PrinterAssignment).where(
            m.PrinterAssignment.end_user_id == end_user.id
        )
    ).all()

    group_ids = list(
        db.scalars(
            select(m.EndUserGroupMember.group_id).where(
                m.EndUserGroupMember.end_user_id == end_user.id
            )
        ).all()
    )
    via_group = []
    if group_ids:
        via_group = db.scalars(
            select(m.PrinterAssignment).where(
                m.PrinterAssignment.group_id.in_(group_ids)
            )
        ).all()

    via_machine = []
    if machine is not None:
        via_machine = db.scalars(
            select(m.PrinterAssignment).where(
                m.PrinterAssignment.machine_id == machine.id
            )
        ).all()

    # Lower rank wins. A number rather than the old is_direct boolean because
    # there are three sources now, and a boolean cannot order three things --
    # which is precisely how a "last writer wins" bug gets in.
    if machine is not None and machine.default_wins:
        rank_direct, rank_machine = 1, 0
    else:
        rank_direct, rank_machine = 0, 1
    rank_group = 2

    # printer_id -> (is_default, group_name or None, rank)
    merged: dict = {}
    for a in via_group:
        name = a.group.name if a.group is not None else None
        prev = merged.get(a.printer_id)
        # Among groups alone, a default flag anywhere wins; the tie-break below
        # settles which printer actually ends up default.
        if prev is None:
            merged[a.printer_id] = (a.is_default, name, rank_group)
        elif not prev[0] and a.is_default:
            merged[a.printer_id] = (True, name, rank_group)
    for a in via_machine:
        prev = merged.get(a.printer_id)
        # Supersedes a group assignment for the same printer, never a direct one
        # -- which is why `direct` is applied after this, not before.
        if prev is None or prev[2] >= rank_machine:
            merged[a.printer_id] = (a.is_default, None, rank_machine)
    for a in direct:
        prev = merged.get(a.printer_id)
        if prev is None or prev[2] >= rank_direct:
            merged[a.printer_id] = (a.is_default, None, rank_direct)

    if not merged:
        return []

    printers = {
        p.id: p
        for p in db.scalars(
            select(m.Printer).where(m.Printer.id.in_(list(merged.keys())))
        ).all()
    }

    # Exactly one winner, chosen by (rank, printer id). Both terms are total
    # orders, so the outcome cannot depend on dict or query ordering -- which is
    # the whole point: a workstation's default must be reproducible, and "why is
    # that their default?" must have an answer.
    candidates = [pid for pid, (is_def, _, _) in merged.items() if is_def]
    winner = None
    if candidates:
        winner = min(candidates, key=lambda pid: (merged[pid][2], pid))

    out = []
    for pid in sorted(merged):
        printer = printers.get(pid)
        if printer is None:  # pragma: no cover - FK makes this unreachable
            continue
        _, group_name, _ = merged[pid]
        out.append((printer, pid == winner, group_name))
    return out


def sync_group_members(
    db: Session, *, group: m.EndUserGroup, end_users: list
) -> tuple:
    """Set a group's membership to exactly ``end_users``. Returns (added, removed).

    Same-tenant only, for the reason in ``assign_printer``. Written as a set
    difference rather than delete-all-then-insert so that a membership refresh
    is not a window in which everybody briefly has no printers -- with a sync
    running on a schedule, that window would be real and recurring.
    """
    for u in end_users:
        if u.client_id != group.client_id:
            raise TenancyError(
                f"group {group.id} belongs to client {group.client_id}, "
                f"end user {u.id} belongs to client {u.client_id}"
            )

    want = {u.id for u in end_users}
    have = set(
        db.scalars(
            select(m.EndUserGroupMember.end_user_id).where(
                m.EndUserGroupMember.group_id == group.id
            )
        ).all()
    )

    for uid in want - have:
        db.add(m.EndUserGroupMember(group_id=group.id, end_user_id=uid))
    if have - want:
        for row in db.scalars(
            select(m.EndUserGroupMember).where(
                m.EndUserGroupMember.group_id == group.id,
                m.EndUserGroupMember.end_user_id.in_(list(have - want)),
            )
        ).all():
            db.delete(row)
    db.flush()
    return len(want - have), len(have - want)


# --------------------- workstation enrollment ------------------------------- #


def redeem_enroll_key(
    db: Session,
    key: str,
    *,
    machine_uid: str,
    name: Optional[str] = None,
) -> Optional[tuple]:
    """Exchange a client enroll key for a machine + its own API key.

    Returns ``(machine, api_key, created, adopted)`` or ``None`` when the key is
    unknown or revoked. The caller must not distinguish those to the client: a bearer
    credential that reports *why* it failed is an oracle for guessing others.

    RE-ENROLLING A KNOWN ``machine_uid`` ROTATES ITS KEY rather than creating a
    second row. Both halves of that matter:

    * A second row would be the same PC twice -- one holding the operator's
      assignments and one holding none -- and the operator has no way to tell
      which is live. The uniqueness constraint makes it impossible anyway; this
      makes the good outcome happen instead of an integrity error.
    * Rotating means a workstation that lost its credential (a wiped ProgramData,
      a corrupt key file) recovers by itself. Without it, the machine is bricked
      until somebody visits it, which for a print client is the failure that
      makes people uninstall.

    The cost, stated rather than glossed: whoever holds the enroll key can
    rotate any machine's key **in that client** and inherit its assignments.
    That is bounded by already holding the key -- they could enroll regardless --
    and it is why revocation is one click and every redemption is audited.
    """
    from central.security import generate_api_key, hash_api_key, hash_enroll_key

    uid = (machine_uid or "").strip()
    if not uid or not (key or "").strip():
        return None

    row = db.scalar(
        select(m.WorkstationEnrollKey).where(
            m.WorkstationEnrollKey.key_hash == hash_enroll_key(key),
            m.WorkstationEnrollKey.revoked_at.is_(None),
        )
    )
    if row is None:
        return None

    now = _now()
    row.last_used_at = now

    # Machine-reported, therefore untrusted: length-capped and used only as a
    # display label. It never selects the tenant -- client_id comes from the key.
    clean_name = (name or "").strip()[:255]
    api_key = generate_api_key()

    machine = db.scalar(
        select(m.Machine).where(
            m.Machine.client_id == row.client_id,
            m.Machine.machine_uid == uid[:64],
        )
    )
    adopted = False
    if machine is None:
        # No record for this identity. Before creating one, see whether this is a
        # re-imaged PC coming back under a new GUID.
        machine = adopt_by_name(db, client_id=row.client_id, name=clean_name)
        adopted = machine is not None

    created = machine is None
    if machine is None:
        machine = m.Machine(
            client_id=row.client_id,
            machine_uid=uid[:64],
            name=clean_name,
        )
        db.add(machine)
    else:
        if adopted:
            # Take over the identity. Assignments, default_wins and history hang
            # off the row id, so preserving the row IS preserving the printers --
            # nothing has to be copied.
            machine.machine_uid = uid[:64]
        if clean_name:
            machine.name = clean_name

    machine.api_key_hash = hash_api_key(api_key)
    machine.last_seen_at = now
    # A re-enrolling machine that an operator retired comes back active: the PC
    # is demonstrably running the client again, and leaving it inactive would
    # silently provision nothing while reporting a successful enrollment.
    machine.active = True
    db.flush()
    return machine, api_key, created, adopted


def printers_for_machine(db: Session, machine: m.Machine) -> list:
    """The machine's own printers, with nobody signed in.

    Same return shape as ``effective_printers_for`` so the caller has one thing
    to iterate, and the same single-default guarantee, tie-broken by lowest
    printer id for the same reason: a workstation's default must not depend on
    query ordering.

    An inactive machine resolves to nothing, matching an inactive user.
    """
    if not machine.active:
        return []

    rows = db.scalars(
        select(m.PrinterAssignment).where(m.PrinterAssignment.machine_id == machine.id)
    ).all()
    if not rows:
        return []

    by_printer = {a.printer_id: a.is_default for a in rows}
    printers = {
        p.id: p
        for p in db.scalars(
            select(m.Printer).where(m.Printer.id.in_(list(by_printer.keys())))
        ).all()
    }
    defaults = sorted(pid for pid, is_def in by_printer.items() if is_def)
    winner = defaults[0] if defaults else None

    out = []
    for pid in sorted(by_printer):
        printer = printers.get(pid)
        if printer is None:  # pragma: no cover - FK makes this unreachable
            continue
        out.append((printer, pid == winner, None))
    return out


def adopt_by_name(
    db: Session, *, client_id: int, name: str
) -> "Optional[m.Machine]":
    """A re-imaged PC coming back: find the record it should take over.

    A re-image wipes ProgramData, so the machine mints a fresh GUID and arrives
    looking like a stranger. Without this it comes back with no printers and
    leaves a dead row behind, which for a fleet that re-images routinely means
    re-assigning every PC by hand. The computer name is the only durable thing
    left to match on -- which is exactly why ``machines.name`` was stored.

    Name matching is genuinely weaker than GUID matching, so every way it can be
    wrong is refused rather than resolved:

    * **Tenant-scoped, always.** The client comes from the enrollment key, never
      from the caller, so a name can only ever match inside its own customer.
    * **Exactly one candidate, or none.** Two records sharing a name means
      adopting either is a coin flip that could hand one person's printers to
      another. Ambiguity creates a new machine and leaves the operator a
      decision to make, which is recoverable; a wrong merge is not.
    * **The record must look GONE.** A machine that checked in recently is
      alive, so a name match is a *collision*, not a re-image. Adopting it would
      rotate a working PC's credential out from under it; that PC would 401,
      re-enroll, and the two would take turns stealing the row forever. The
      staleness window (``workstation.adopt_stale_after_min``) is what makes the
      difference between "replaced" and "duplicate name" decidable at all.
    * **Blank names never match.** Machine-reported and optional, and every
      unnamed record would otherwise be one collision.

    Returns the record to take over, or None to create a fresh one.

    Worth stating plainly, because it is a real change in what holding an
    enrollment key gets you: with adoption on, a holder who names their machine
    after a stale record inherits that record's printers, where before
    enrollment granted nothing at all. It is bounded to one tenant, needs the
    key, needs the target to be stale, and writes a ``machine.adopt`` audit row.
    Operators who do not want that trade turn ``workstation.adopt_by_name`` off.
    """
    from central.runtime import load_settings

    clean = (name or "").strip()
    if not clean:
        return None

    rt = load_settings(db)
    if not rt.get("workstation.adopt_by_name", True):
        return None
    try:
        stale_min = int(rt.get("workstation.adopt_stale_after_min", 60) or 0)
    except (TypeError, ValueError):
        stale_min = 60

    # Case-insensitive: Windows computer names are, and a PC that comes back as
    # DESK-01 having been desk-01 is the same desk.
    candidates = [
        row
        for row in db.scalars(
            select(m.Machine).where(m.Machine.client_id == client_id)
        ).all()
        if (row.name or "").strip().lower() == clean.lower()
    ]
    if len(candidates) != 1:
        return None

    candidate = candidates[0]
    cutoff = _now() - timedelta(minutes=max(stale_min, 0))
    last_seen = candidate.last_seen_at
    if last_seen is not None:
        # Rows written before timezone-awareness was universal can be naive;
        # treat those as UTC rather than raising mid-enrollment.
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if last_seen > cutoff:
            return None

    return candidate


# ----------------------- vendor driver packages ----------------------------- #

#: A shorter tag would match a whole fleet. "HL" appears in every Brother model
#: string; "HL-L2350DW" identifies one. Enforced on upload AND on match, so a
#: row that predates the rule cannot quietly bind everything.
MIN_DRIVER_MODEL_TAG = 3


def _driver_bytes_present(row: "m.DriverPackage") -> bool:
    """Whether this package's payload is where it should be.

    A ``system`` macOS package deliberately has none: the vendor ``.pkg`` was
    pushed by MDM and this row records only the absolute path of the PPD it
    installed. Running it through the missing-file check would disqualify every
    such package on every poll, so the exemption is stated here rather than
    special-cased at each call site.
    """
    from central import driver_store

    if row.platform == "macos" and row.macos_kind == "system":
        return True
    return not driver_store.missing(row.stored_at)


def driver_package_for(
    db: Session, printer: m.Printer, platform: str = "windows"
) -> "Optional[m.DriverPackage]":
    """The package that should drive this printer on ``platform``, or None.

    Matching is a case-insensitive **substring** of ``printers.model``, not
    equality: the model string comes from SNMP and varies by how the device
    reports itself ("HL-L2350DW" vs "Brother HL-L2350DW series"), so equality
    fails on exactly the value the printer actually returns.

    The rules, all of which refuse rather than guess:

    * **Tenant-scoped.** A package belongs to one client and can never drive
      another's hardware.
    * **A more specific match wins.** The longest matching tag is taken, so a
      model-specific package beats a family-wide one without the operator
      having to order them.
    * **A tie is refused.** Two equally-specific packages means binding either
      is a coin flip, and the wrong driver prints garbage -- worse than no queue
      at all, which is the whole reason driver_required printers were skipped
      rather than guessed in the first place.
    * **A package whose file is missing does not match.** After a database
      restore the rows survive and the bytes do not; returning one would send
      the workstation to a download that 404s. Reported as "re-upload" instead.
      A ``system`` macOS package is exempt: it has no bytes by design, because
      the driver was installed out of band by MDM and only its PPD path is
      recorded here.

    * **Platform scopes the candidates BEFORE specificity is compared**, and that
      ordering is a correctness requirement rather than a filter for tidiness. A
      client holding both a Windows and a macOS package tagged for one printer
      produces two equally-specific matches, which the ambiguity rule above
      correctly refuses -- so adding macOS packages to a client would have
      silently stopped the Windows staging that already worked. Scoping first
      means the two platforms cannot collide at all.
    """
    model = (printer.model or "").strip().lower()
    rows = db.scalars(
        select(m.DriverPackage).where(
            m.DriverPackage.client_id == printer.client_id,
            m.DriverPackage.platform == platform,
        )
    ).all()

    usable = [r for r in rows if _driver_bytes_present(r)]

    tagged = []
    for row in usable:
        tag = (row.model or "").strip().lower()
        if len(tag) >= MIN_DRIVER_MODEL_TAG and model and tag in model:
            tagged.append((len(tag), row))

    if tagged:
        best = max(t[0] for t in tagged)
        winners = [r for length, r in tagged if length == best]
        if len(winners) != 1:
            import logging

            logging.getLogger(__name__).warning(
                "printer %s matches %d driver packages equally; refusing to guess",
                printer.id, len(winners),
            )
            return None
        return winners[0]

    # Fall back to an untagged package -- the operator's "this client only has
    # the one printer family" case. Still refused when ambiguous.
    untagged = [r for r in usable if not (r.model or "").strip()]
    if len(untagged) == 1:
        return untagged[0]
    return None


# --------------------------------------------------------------------------- #
# Device/model definitions -- the server-pushed feed
# --------------------------------------------------------------------------- #
def clients_served_by_agent(db: Session, agent: m.Agent) -> set:
    """The set of client_ids this agent collects for.

    Derived from the sites it serves rather than stored, for the same reason
    printer assignment refuses to denormalise ``client_id``: a stored copy goes
    stale the moment a site moves between clients, and a stale tenancy value is
    worse than a join.
    """
    site_ids = sites_served_by_agent(db, agent)
    if not site_ids:
        return set()
    return set(
        db.scalars(select(m.Site.client_id).where(m.Site.id.in_(site_ids)).distinct())
    )


def device_definition_feed(db: Session, agent: m.Agent) -> list:
    """The definitions this agent should hold, in a stable order.

    Scoping: every enabled global definition (``client_id IS NULL`` -- hardware
    knowledge, not tenant data) plus those scoped to a client this agent
    actually collects for. An agent never receives another tenant's custom
    definition, which is why the scope is computed from the agent rather than
    the feed being served wholesale.

    Sorted by key so the feed digest and the signature are computed over a
    stable sequence; an unordered query would change the version on every call
    and make every agent re-download on every poll.
    """
    client_ids = clients_served_by_agent(db, agent)
    scope = m.DeviceDefinition.client_id.is_(None)
    if client_ids:
        scope = scope | m.DeviceDefinition.client_id.in_(client_ids)
    rows = db.scalars(
        select(m.DeviceDefinition)
        .where(m.DeviceDefinition.enabled.is_(True), scope)
        .order_by(m.DeviceDefinition.key)
    ).all()

    # A client-scoped definition overrides a global one with the same key --
    # that is what "scoped to this customer" has to mean, or the narrower row
    # would be silently ignored.
    #
    # Two *different* clients scoping the same key is the one case that has no
    # answer, and it is reachable: one agent at HQ collecting for several
    # customers. Serving either is a coin flip over what that fleet reads, so
    # the key is dropped and said out loud -- the same discipline as an
    # ambiguous driver-package match. Global definitions are unaffected,
    # because the partial unique index makes duplicate global keys impossible.
    import logging

    globals_by_key = {r.key: r for r in rows if r.client_id is None}
    scoped_by_key: dict = {}
    ambiguous = set()
    for row in rows:
        if row.client_id is None:
            continue
        if row.key in scoped_by_key:
            ambiguous.add(row.key)
        scoped_by_key[row.key] = row

    if ambiguous:
        logging.getLogger(__name__).warning(
            "agent %s is served by %d client(s) that scope the same definition "
            "key(s) %s; refusing to guess -- serving neither",
            agent.id, len(client_ids), ", ".join(sorted(ambiguous)),
        )

    merged = dict(globals_by_key)
    merged.update(scoped_by_key)
    return [merged[k] for k in sorted(merged) if k not in ambiguous]
