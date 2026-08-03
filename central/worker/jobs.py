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
from central.channels import (
    Notification,
    routable_channels,
    route_channels,
)
from central import suppression
from central.channels.delivery import (
    WITHHELD_CHANNEL_KEY,
    channel_badges,
    notification_payload,
    record_dispatch,
)
from central.channels.delivery import flush_deferred as _flush_deferred
from central.channels.delivery import retry_due as _retry_due
from central.channels.freescout import FreeScoutChannel
from central.runtime import load_settings

# Alert states that still represent an outstanding condition. An ACKNOWLEDGED
# alert is "seen but not fixed", so a cleared condition must resolve it too --
# otherwise an ack'd alert whose condition clears is stuck open forever.
_LIVE_STATES = (m.AlertState.open, m.AlertState.acknowledged)

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


def reassign_collectors(db: Session, now: Optional[datetime] = None) -> dict:
    """Hand a leased subnet to its standby when the collector really has gone.

    **This is the only path that moves a lease between agents.** A heartbeat can
    renew its own and pick up one nobody holds, but it can never take one -- so
    a takeover is always a central decision, made once, with the whole tenant in
    view, and audited. It runs under the worker's leader lock, so two worker
    containers cannot both perform it.

    Every check below is stale by the time the write executes, which is why the
    write is a compare-and-swap (``collector.grant_lease`` asserts the holder id
    AND the expiry it judged). A primary that was merely slow renews between our
    read and our write, the swap misses, and nothing moves -- that, not the
    checks, is what guarantees a working collector is never displaced. The
    checks exist to stop us *trying*, so the audit trail records handovers
    rather than attempts.

    Refusals, in the shape of ``services.adopt_by_name``:

    * **The lease must have lapsed.** A live lease is a collector that is still
      sweeping on its own clock; taking it would create the exact overlap the
      lease exists to prevent.
    * **The holder must look gone** -- ``offline``, and silent for longer than
      ``collector.takeover_after_seconds``, which is deliberately a separate,
      longer threshold than the offline grace. Missing one heartbeat is not
      having stopped working.
    * **The successor must be alive.** Moving a subnet to a second dead agent
      changes nothing except who is blamed for the silence.
    * **Exactly one candidate**, structurally: the other of primary/standby.
    * A holder that is **no longer either** (an operator reassigned the subnet
      out from under it) is not "gone", but it is not entitled either -- it
      simply stops being able to renew, and the subnet moves to the primary once
      its lease lapses. The lapse is what makes that safe, not the reassignment.

    A primary that comes back does NOT get its subnet back. The standby is
    collecting correctly; a second handover buys nothing and costs another
    transition, and a flapping collector -- which is how these actually fail --
    would oscillate, one handover per flap. Hand-back is an operator action.
    """
    from central import collector as _collector
    from central.audit import record

    now = now or _now()
    rt = load_settings(db)
    ttl, takeover_after, auto = _collector.lease_settings(rt)
    grace = timedelta(seconds=rt.get("alerts.offline_grace_seconds", 300))
    silent_for = timedelta(seconds=takeover_after)

    subnets = list(
        db.scalars(select(m.Subnet).where(m.Subnet.standby_agent_id.is_not(None)))
    )
    if not subnets:
        return {"collector_takeovers": 0}

    def _alive(agent: Optional[m.Agent]) -> bool:
        if agent is None or agent.status != m.AgentStatus.online:
            return False
        last = _aware(agent.last_heartbeat)
        return last is not None and (now - last) <= grace

    def _gone(agent: Optional[m.Agent]) -> bool:
        # A deleted agent row is gone by definition. Otherwise both conditions:
        # marked offline by mark_offline_agents (which ran earlier this cycle),
        # and silent for the longer takeover threshold.
        if agent is None:
            return True
        if agent.status != m.AgentStatus.offline:
            return False
        last = _aware(agent.last_heartbeat)
        return last is None or (now - last) > silent_for

    taken = 0
    for subnet in subnets:
        expires = _aware(subnet.collector_lease_expires_at)
        # A live lease is never touched, whatever anyone's status says.
        if expires is not None and expires > now:
            continue
        holder_id = subnet.collector_agent_id
        eligible = _collector.eligible_agent_ids(subnet)

        if holder_id is None:
            # Nobody holds it: a freshly-released lease past its barrier, or a
            # subnet whose primary has never heartbeated. The primary gets it
            # back -- this is a return to the configured owner, not a takeover,
            # so it needs no staleness judgement, only a live candidate.
            candidate_id = subnet.agent_id
        elif holder_id not in eligible:
            # The operator reassigned this subnet away from its holder. Not a
            # failure, so no staleness test; the lapse alone makes it safe.
            candidate_id = subnet.agent_id
        else:
            if not auto:
                continue
            candidate_id = next(iter(eligible - {holder_id}), None)
            if not _gone(db.get(m.Agent, holder_id)):
                continue

        if candidate_id is None or candidate_id == holder_id:
            continue
        if not _alive(db.get(m.Agent, candidate_id)):
            continue

        if _collector.grant_lease(
            db, subnet.id,
            to_agent_id=candidate_id, from_agent_id=holder_id,
            now=now, ttl_seconds=ttl,
        ):
            taken += 1
            action = (
                "subnet.collector_takeover" if holder_id is not None
                else "subnet.collector_assign"
            )
            record(
                db, None, None, action,
                target=f"subnet:{subnet.id} {subnet.cidr}",
                detail=(
                    f"collector agent:{holder_id if holder_id is not None else 'none'}"
                    f" -> agent:{candidate_id}; lease lapsed"
                    + (
                        f", holder silent > {int(silent_for.total_seconds())}s"
                        if holder_id is not None and holder_id in eligible else ""
                    )
                ),
            )
    db.commit()
    return {"collector_takeovers": taken}


def _sites_with_a_live_agent(db: Session) -> set:
    """Site ids currently covered by at least one non-offline agent.

    Mirrors ``services.sites_served_by_agent`` (home site + every subnet
    assigned to the agent, plus any whose lease it currently holds) but
    aggregated across all agents, so a site served by two agents stays covered
    while only one of them is down. The lease clause is what stops a site whose
    primary died and whose standby took over from being reported as an outage:
    it is being collected, by the other agent.
    """
    from sqlalchemy import or_

    covered: set = set()
    for agent in db.scalars(select(m.Agent).where(m.Agent.status == m.AgentStatus.online)):
        covered.add(agent.site_id)
        covered.update(
            db.scalars(
                select(m.Subnet.site_id)
                .where(
                    or_(
                        m.Subnet.agent_id == agent.id,
                        m.Subnet.collector_agent_id == agent.id,
                    )
                )
                .distinct()
            )
        )
    return covered


def mark_offline_printers(db: Session, now: Optional[datetime] = None) -> dict:
    """Mark approved printers offline once their readings dry up.

    An unreachable printer doesn't report anything -- ``poll_one`` raises,
    the agent drops it from the batch, and central is never told. Since
    ``Printer.status`` is only ever written from an arriving reading, nothing
    moved it off its last-good value: an unplugged printer read "ok" forever
    and raised nothing, which is the single failure an MSP most needs to catch.

    Printers whose site has no live agent are skipped. That outage is already
    covered by the agent-offline alert, and marking the agent's whole fleet
    offline would turn one site event into one alert per printer.

    Recovery needs no work here: the next reading to arrive sets the status
    from the device itself (``services.apply_reading``).
    """
    now = now or _now()
    minutes = load_settings(db).get("alerts.printer_offline_minutes", 30)
    grace = timedelta(minutes=minutes)
    covered = _sites_with_a_live_agent(db)
    changed = 0
    for printer in db.scalars(
        select(m.Printer).where(m.Printer.discovery_state == m.DiscoveryState.approved)
    ):
        if printer.site_id not in covered:
            continue
        last = _aware(printer.last_seen)
        # A printer approved but never yet polled has nothing to go stale.
        if last is None or (now - last) <= grace:
            continue
        if printer.status != m.PrinterStatus.offline:
            printer.status = m.PrinterStatus.offline
            changed += 1
    db.commit()
    return {"printers_marked_offline": changed}


# --------------------------------------------------------------------------- #
# Alert evaluation
# --------------------------------------------------------------------------- #
def _printer_label(printer: m.Printer) -> str:
    name = printer.display_name or printer.model or printer.hostname or "printer"
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


def _external_ref_from(results, channels) -> Optional[str]:
    """Pull the FreeScout ticket id out of a dispatch result set.

    ``dispatch`` returns ``(channel_name, ChannelResult)`` tuples; the FreeScout
    channel sets ``ChannelResult.external_ref`` to the new conversation id. Match
    the result back to its channel by name so only a real FreeScout ticket id is
    persisted (a dry-run create returns no external_ref, so nothing is stored).
    """
    fs_names = {c.name for c in channels if isinstance(c, FreeScoutChannel)}
    for name, res in results:
        if name in fs_names and res.ok and res.external_ref:
            return res.external_ref
    return None


def _find_open_alert(db: Session, dedupe_key: str) -> Optional[m.Alert]:
    """Find a still-live (open OR acknowledged) alert for this dedupe key.

    Acknowledged is included so acking an alert doesn't make the worker re-open
    a fresh duplicate on the next cycle while the condition still holds.
    """
    return db.scalar(
        select(m.Alert).where(
            m.Alert.dedupe_key == dedupe_key,
            m.Alert.state.in_(_LIVE_STATES),
        )
    )


def _revive_flapping_alert(
    db: Session,
    dedupe_key: str,
    detail: str,
    now: datetime,
    runtime: Optional[dict],
) -> Optional[m.Alert]:
    """Re-open a same-condition alert that resolved inside the flap window.

    A condition sitting on its threshold clears and re-fires repeatedly. Each
    re-fire used to create a brand-new alert and dispatch a brand-new
    notification: a cartridge oscillating 9<->11% around a threshold of 10
    produced three fresh alerts in six cycles -- 720 notifications/day at a 60s
    poll interval, for one cartridge.

    Inside ``alerts.renotify_cooldown_min`` this is treated as the SAME incident:
    the original alert is re-opened with its flap count bumped and **no
    notification is sent**. Deliberately quiet -- the operator was already told
    about this condition minutes ago, and telling them again per oscillation is
    the bug. If it is genuinely still broken, ``escalate_alerts`` re-notifies on
    its own schedule; ``last_notified_at`` is left untouched so that clock keeps
    running from the real notification rather than being reset by every flap.

    Returns the revived alert, or None when nothing is eligible (no recent
    resolve, or the cooldown is disabled).
    """
    minutes = int((runtime or {}).get("alerts.renotify_cooldown_min", 0) or 0)
    if minutes <= 0:
        return None
    cutoff = now - timedelta(minutes=minutes)
    alert = db.scalar(
        select(m.Alert)
        .where(
            m.Alert.dedupe_key == dedupe_key,
            m.Alert.state == m.AlertState.resolved,
            m.Alert.resolved_at.is_not(None),
        )
        .order_by(m.Alert.resolved_at.desc())
        .limit(1)
    )
    if alert is None:
        return None
    resolved_at = _aware(alert.resolved_at)
    if resolved_at is None or resolved_at < cutoff:
        return None
    alert.state = m.AlertState.open
    alert.resolved_at = None
    alert.flap_count = (alert.flap_count or 0) + 1
    alert.detail = "%s (re-opened; flapped %d× within %dm)" % (
        detail,
        alert.flap_count,
        minutes,
    )
    return alert


def _notify_alert(
    db: Session,
    alert: m.Alert,
    *,
    rule: Optional[m.AlertRule],
    printer: Optional[m.Printer],
    candidates: list,
    now: datetime,
    runtime: Optional[dict] = None,
    windows: Optional[list] = None,
) -> bool:
    """Build the notification, route it, deliver durably, and stamp bookkeeping.

    Routing honors ``rule.channel_ids`` and per-channel scope/severity (see
    central.channels.route_channels); the routed channels are then delivered
    through ``record_dispatch`` so a transient channel outage is retried by
    retry_deliveries instead of being silently dropped. Captures the FreeScout
    ticket id on ``external_ref`` and sets ``last_notified_at`` so the escalation
    pass can measure how long an alert has gone un-escalated.

    Suppression windows are consulted FIRST, and because both the initial open
    and every escalation come through here, one check covers both. Returns True
    when a notification was actually dispatched and False when a window held or
    dropped it, so callers can avoid counting a send that didn't happen.

    On a deferral ``last_notified_at`` is deliberately NOT stamped -- nothing was
    sent, and stamping it would make the escalation clock measure from a
    non-event. The held row itself is idempotent, so an escalation that arrives
    during the window doesn't stack duplicates.
    """
    runtime = runtime or load_settings(db)
    decision = suppression.evaluate(
        db, printer, alert.severity.value, now, runtime=runtime, windows=windows
    )
    if not decision.dispatch:
        _record_withheld(db, alert, decision, now)
        return False

    client_name = site_name = None
    if printer is not None:
        client = db.get(m.Client, printer.client_id)
        site = db.get(m.Site, printer.site_id)
        client_name = client.name if client else None
        site_name = site.name if site else None

    note = Notification(
        title=alert.title,
        body=alert.detail or "",
        severity=alert.severity.value,
        client_name=client_name,
        site_name=site_name,
        printer_label=_printer_label(printer) if printer else None,
        alert_id=alert.id,
    )
    channels = route_channels(
        candidates, rule=rule, printer=printer, severity=alert.severity.value
    )
    results = record_dispatch(db, alert.id, note, channels, runtime=runtime)
    alert.notified_channels = channel_badges(results)
    # Never clear a ref we already hold: escalation re-dispatches through this
    # same path, and FreeScout only returns a conversation id when it CREATES
    # one. An unconditional assign nulled the ref on every escalation, so the
    # closed-loop resolver lost the ticket and the next round opened a duplicate.
    alert.external_ref = _external_ref_from(results, channels) or alert.external_ref
    alert.last_notified_at = now
    return True


def _record_withheld(
    db: Session, alert: m.Alert, decision, now: datetime,
) -> None:
    """Persist the fact that a window held or dropped this alert's notification.

    A deferral writes ONE ``deferred`` row carrying ``next_attempt_at`` = the
    window's end, which makes the existing retry sweeper the wake mechanism --
    no second scheduler, no polling. It is idempotent per alert: an escalation
    arriving mid-window must not stack a second held row, and re-evaluating on
    every cycle must not either. The stored end is refreshed on each pass so an
    operator extending the window is honoured.

    A suppression writes a terminal ``suppressed`` row rather than nothing at
    all. Writing nothing would be indistinguishable from "no alert fired", and
    an operator asking for silence still deserves an answer to "what did I miss
    on Saturday?".

    The badge on the alert says which happened, so the Alerts page shows "held"
    or "suppressed" instead of leaving the alert looking simply un-notified.
    """
    existing = db.scalar(
        select(m.NotificationDelivery).where(
            m.NotificationDelivery.alert_id == alert.id,
            m.NotificationDelivery.status.in_(
                (m.DeliveryStatus.deferred, m.DeliveryStatus.suppressed)
            ),
        )
    )
    payload = notification_payload(Notification(
        title=alert.title,
        body=alert.detail or "",
        severity=alert.severity.value,
        printer_label=None,
        alert_id=alert.id,
    ))
    if decision.suppress:
        status = m.DeliveryStatus.suppressed
        next_at = None
    else:
        status = m.DeliveryStatus.deferred
        next_at = decision.deferred_until

    if existing is not None:
        # Refresh in place rather than adding a row.
        existing.status = status
        existing.next_attempt_at = next_at
        existing.last_error = decision.reason[:2000]
    else:
        db.add(m.NotificationDelivery(
            alert_id=alert.id,
            channel_key=WITHHELD_CHANNEL_KEY,
            status=status,
            attempts=0,
            last_error=decision.reason[:2000],
            next_attempt_at=next_at,
            payload=payload,
        ))
    alert.notified_channels = [{
        "channel": "quiet hours" if decision.defer else "suppressed",
        "ok": True,
        "sent": False,
        "detail": decision.reason,
    }]


def _open_alert(
    db: Session,
    rule: m.AlertRule,
    dedupe_key: str,
    title: str,
    detail: str,
    *,
    printer: Optional[m.Printer] = None,
    agent: Optional[m.Agent] = None,
    candidates: Optional[list] = None,
    now: Optional[datetime] = None,
    runtime: Optional[dict] = None,
    stats: Optional[dict] = None,
) -> Optional[m.Alert]:
    """Open an alert if one isn't already live for this dedupe_key. Returns it (or None).

    Returns None both when an alert is already live (dedupe) and when a recent
    resolve was revived as a flap (see ``_revive_flapping_alert``) -- in neither
    case did a NEW alert open, so callers must not count it as one. ``stats``, if
    given, has its ``flapped`` key incremented so the cycle summary can report
    damping that happened instead of a notification.
    """
    if _find_open_alert(db, dedupe_key) is not None:
        return None
    now = now or _now()
    revived = _revive_flapping_alert(db, dedupe_key, detail, now, runtime)
    if revived is not None:
        if stats is not None:
            stats["flapped"] = stats.get("flapped", 0) + 1
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
        escalation_level=0,
    )
    db.add(alert)
    db.flush()  # assign alert.id
    _notify_alert(
        db,
        alert,
        rule=rule,
        printer=printer,
        candidates=candidates or [],
        now=now,
        runtime=runtime,
    )
    return alert


def _freescout_channel(channels) -> Optional[FreeScoutChannel]:
    for c in channels or []:
        if isinstance(c, FreeScoutChannel):
            return c
    return None


def _close_ticket_for(alert: m.Alert, channels) -> Optional[dict]:
    """Close the FreeScout ticket tied to ``alert`` when it auto-resolves.

    No-op (returns None) when the alert never opened a ticket. Returns a small
    record describing the close call so callers can stash it and tests can
    assert exactly-once. Errors are swallowed into the record -- a flaky
    FreeScout must never block the worker from marking the alert resolved.
    """
    ref = alert.external_ref
    if not ref:
        return None
    ch = _freescout_channel(channels)
    if ch is None:
        return None
    note = f"Auto-resolved by Printer Nanny: {alert.title}"
    try:
        res = ch.close_ticket(ref, note)
        return {"external_ref": ref, "ok": res.ok, "detail": res.detail}
    except Exception as exc:  # noqa: BLE001 - closing a ticket must never break resolve
        return {"external_ref": ref, "ok": False, "detail": f"unhandled: {exc}"}


def _resolve_stale(db: Session, active_keys: set[str], channels=None) -> int:
    """Resolve live rule-driven alerts whose condition no longer holds this run.

    Covers both cleared conditions (key not re-added) and alerts orphaned by a
    rule that was disabled/deleted (its key is never re-added). Resolves alerts
    in BOTH open and acknowledged states -- an ack'd alert whose condition clears
    must still auto-resolve, otherwise it's stuck open forever. Maintenance and
    predicted-depletion alerts own their own lifecycle (check_maintenance_due /
    forecast_supplies) and are skipped; a resolved alert that opened a FreeScout
    ticket gets it auto-closed.
    """
    resolved = 0
    stmt = select(m.Alert).where(m.Alert.state.in_(_LIVE_STATES))
    for alert in db.scalars(stmt):
        if alert.type in (
            m.AlertConditionType.maintenance_due,
            m.AlertConditionType.predicted_depletion,
        ):
            continue
        if alert.dedupe_key not in active_keys:
            alert.state = m.AlertState.resolved
            alert.resolved_at = _now()
            _close_ticket_for(alert, channels)
            resolved += 1
    return resolved


def evaluate_alerts(db: Session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    rules = list(db.scalars(select(m.AlertRule).where(m.AlertRule.enabled.is_(True))))
    runtime = load_settings(db)
    candidates = routable_channels(db, runtime)
    opened = 0
    stats: dict = {"flapped": 0}
    active_keys: set[str] = set()

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
                    if _open_alert(db, rule, key, title, detail, agent=agent,
                                   candidates=candidates, now=now, runtime=runtime,
                                   stats=stats):
                        opened += 1
            continue

        # Resolved once per rule rather than per printer: the covered-site set
        # is the same for every printer in scope.
        covered = (
            _sites_with_a_live_agent(db)
            if rule.condition_type == m.AlertConditionType.printer_offline
            else None
        )

        for printer in _printers_in_scope(db, rule):
            if rule.condition_type == m.AlertConditionType.printer_offline:
                # Suppress while the whole site is dark -- the agent-offline
                # alert already covers it. See mark_offline_printers.
                if printer.site_id not in covered:
                    continue
                limit = timedelta(minutes=rule.threshold or 0)
                last = _aware(printer.last_seen)
                if last is not None and (now - last) >= limit:
                    key = f"rule:{rule.id}:printer:{printer.id}:offline"
                    active_keys.add(key)
                    title = f"Printer offline: {_printer_label(printer)}"
                    detail = (
                        f"No reading for over {rule.threshold:.0f} min "
                        f"(last seen: {last.isoformat()})."
                    )
                    if _open_alert(db, rule, key, title, detail, printer=printer,
                                   candidates=candidates, now=now, runtime=runtime,
                                   stats=stats):
                        opened += 1

            elif rule.condition_type == m.AlertConditionType.supply_below:
                threshold = rule.threshold or 0
                # Asymmetric thresholds (a deadband): open at <= threshold but
                # hold a live alert until the level climbs the margin ABOVE it.
                # Without this, a cartridge reading 9/11/9/11 around a threshold
                # of 10 resolved and re-opened every cycle, each re-open being a
                # fresh alert and a fresh notification.
                deadband = float(runtime.get("alerts.supply_deadband_pct", 0) or 0)
                for supply in printer.supplies:
                    if supply.level_pct is None:
                        continue
                    key = f"rule:{rule.id}:printer:{printer.id}:supply:{supply.id}"
                    label = supply.color or supply.type.value
                    if supply.level_pct <= threshold:
                        active_keys.add(key)
                        title = f"Low {label} on {_printer_label(printer)}"
                        detail = f"{label} at {supply.level_pct:.0f}% (threshold {threshold:.0f}%)."
                        if _open_alert(db, rule, key, title, detail, printer=printer,
                                       candidates=candidates, now=now, runtime=runtime,
                                       stats=stats):
                            opened += 1
                    elif (
                        deadband > 0
                        and supply.level_pct < threshold + deadband
                        and _find_open_alert(db, key) is not None
                    ):
                        # In the deadband with an alert still live: keep the key
                        # active so the resolve pass leaves it alone. Only a
                        # genuine recovery past threshold+margin resolves it.
                        active_keys.add(key)

            elif rule.condition_type == m.AlertConditionType.error_severity:
                min_rank = _SEVERITY_RANK.get(rule.severity, 1)
                # Queried, not read off ``printer.events``: sessions are
                # configured expire_on_commit=False, so that relationship caches
                # whatever it first loaded. The worker happens to build a fresh
                # session per cycle, which is the only reason the old code saw
                # new events at all -- correctness must not rest on that. The
                # query also filters unresolved + severity in SQL instead of
                # loading every event ever recorded for the printer.
                qualifying = [
                    sev for sev, rank in _SEVERITY_RANK.items() if rank >= min_rank
                ]
                unresolved = list(db.scalars(
                    select(m.PrinterEvent).where(
                        m.PrinterEvent.printer_id == printer.id,
                        m.PrinterEvent.resolved_at.is_(None),
                        m.PrinterEvent.severity.in_(qualifying),
                    )
                ))
                # One alert per distinct error CODE, not just the newest event.
                # Keying only the newest meant a fresh DOOR_OPEN stopped
                # refreshing the PAPER_JAM key, so the resolve pass closed the
                # jam alert while the printer was still jammed -- the inbox said
                # fixed, the device disagreed.
                by_code: dict = {}
                for event in unresolved:
                    code = event.code or "event"
                    prev = by_code.get(code)
                    if prev is None or event.ts > prev.ts:
                        by_code[code] = event
                # Newest first, then capped: a device spewing dozens of distinct
                # codes must not open dozens of alerts. The cap is disclosed in
                # the detail of the ones that DO open rather than dropped
                # silently, so nobody reads a capped list as the whole story.
                ordered = sorted(by_code.values(), key=lambda e: e.ts, reverse=True)
                cap = max(1, int(runtime.get("alerts.max_error_alerts_per_printer", 5) or 5))
                hidden = max(0, len(ordered) - cap)
                for event in ordered[:cap]:
                    code = event.code or "event"
                    key = f"rule:{rule.id}:printer:{printer.id}:error:{code}"
                    active_keys.add(key)
                    title = f"Error on {_printer_label(printer)}"
                    detail = f"{event.severity.value}: {event.message}"
                    if hidden:
                        detail += (
                            f" (+{hidden} more distinct error code(s) on this "
                            f"printer above the {cap}-alert cap)"
                        )
                    if _open_alert(db, rule, key, title, detail, printer=printer,
                                   candidates=candidates, now=now, runtime=runtime,
                                   stats=stats):
                        opened += 1

    # Pass the unwrapped candidate channels so a resolved alert's FreeScout
    # ticket can be auto-closed (route metadata isn't needed to close a ticket).
    close_channels = [rc.channel for rc in candidates]
    resolved = _resolve_stale(db, active_keys, close_channels)
    # Flush the resolutions so the escalation query (run on a session with
    # autoflush off) doesn't re-notify alerts we just resolved this same pass.
    db.flush()
    escalated = _escalate_alerts(db, runtime, candidates, now)
    db.commit()
    return {
        "alerts_opened": opened,
        "alerts_resolved": resolved,
        "alerts_escalated": escalated,
        # Conditions that re-fired inside the flap cooldown and were folded back
        # into their original alert instead of raising (and notifying) a new one.
        "alerts_flapped": stats["flapped"],
    }


def _escalate_alerts(
    db: Session, runtime: dict, candidates: list, now: datetime
) -> int:
    """Re-notify still-live alerts that have gone unresolved past the window.

    Controlled by ``alerts.escalate_after_minutes`` (0 = off). For each live
    (open or acknowledged) alert whose last notification is older than the
    window, re-dispatch through the same routing, bump ``escalation_level``,
    and stamp ``last_notified_at`` so the next escalation is measured from now.

    This is a deliberate re-send, distinct from dedupe suppression: dedupe stops
    a *duplicate OPEN* of the same condition, while escalation re-notifies an
    *already-open* alert that nobody has resolved.
    """
    minutes = int(runtime.get("alerts.escalate_after_minutes", 0) or 0)
    if minutes <= 0:
        return 0
    window = timedelta(minutes=minutes)
    escalated = 0
    for alert in db.scalars(select(m.Alert).where(m.Alert.state.in_(_LIVE_STATES))):
        # Baseline: an alert never (re)notified uses its creation time.
        baseline = _aware(alert.last_notified_at) or _aware(alert.created_at)
        if baseline is not None and (now - baseline) < window:
            continue
        rule = db.get(m.AlertRule, alert.rule_id) if alert.rule_id else None
        printer = db.get(m.Printer, alert.printer_id) if alert.printer_id else None
        # Bump only if the notification actually went out. A quiet-hours window
        # returns False here, and counting that as an escalation would inflate the
        # level once per cycle all night -- so an alert would emerge at 07:00
        # reading "escalation level 480" having never escalated to anyone.
        if _notify_alert(
            db, alert, rule=rule, printer=printer,
            candidates=candidates, now=now, runtime=runtime,
        ):
            alert.escalation_level = (alert.escalation_level or 0) + 1
            escalated += 1
    return escalated


# --------------------------------------------------------------------------- #
# Component-life maintenance — match a schedule's component_type onto the
# component-life Supply rows the Brother provider writes (belt/fuser/laser/
# drum/PF-kit). See central.models.MaintenanceSchedule.COMPONENT_TYPES and the
# agent's brother_maintenance._EXTRA_PART_ROWS for the type/color labelling.
# --------------------------------------------------------------------------- #
def _component_supply_matches(supply: m.Supply, component_type: str) -> bool:
    """True when ``supply`` is the component-life row for ``component_type``."""
    if component_type == "fuser":
        return supply.type == m.SupplyType.fuser
    if component_type == "drum":
        return supply.type == m.SupplyType.drum
    if component_type == "belt":
        return supply.type == m.SupplyType.other and supply.color == "belt"
    if component_type == "laser":
        return supply.type == m.SupplyType.other and supply.color == "laser"
    if component_type == "pf_kit":
        return supply.type == m.SupplyType.other and supply.color in ("pf-kit-mp", "pf-kit-1")
    return False


def _component_schedule_printers(db: Session, sched: m.MaintenanceSchedule):
    """Approved printers a component schedule applies to (specific / model / fleet)."""
    stmt = select(m.Printer).where(m.Printer.discovery_state == m.DiscoveryState.approved)
    if sched.printer_id:
        stmt = stmt.where(m.Printer.id == sched.printer_id)
    elif sched.model:
        stmt = stmt.where(m.Printer.model.ilike(f"%{sched.model}%"))
    return db.scalars(stmt)


# --------------------------------------------------------------------------- #
# Maintenance due — schedule-driven (no alert_rule needed), with its own
# open/dispatch/resolve lifecycle so a due schedule actually notifies, and the
# alert clears once the schedule's next_due is rolled forward (e.g. service logged).
# --------------------------------------------------------------------------- #
def check_maintenance_due(db: Session, now: Optional[datetime] = None) -> dict:
    now = now or _now()
    runtime = load_settings(db)
    candidates = routable_channels(db, runtime)
    opened = 0
    active_keys: set[str] = set()

    def _open_maintenance_alert(key, title, detail, printer):
        """Open a maintenance-due alert for ``key`` unless one's already live.

        Routes by the printer's scope (no AlertRule), delivers durably, captures
        the FreeScout ticket id, and stamps escalation bookkeeping via
        ``_notify_alert`` -- same path as rule-driven alerts.
        """
        if _find_open_alert(db, key) is not None:
            return False
        alert = m.Alert(
            rule_id=None,
            printer_id=printer.id if printer else None,
            type=m.AlertConditionType.maintenance_due,
            severity=m.EventSeverity.warning,
            state=m.AlertState.open,
            title=title,
            detail=detail,
            dedupe_key=key,
            escalation_level=0,
        )
        db.add(alert)
        db.flush()
        _notify_alert(
            db, alert, rule=None, printer=printer,
            candidates=candidates, now=now, runtime=runtime,
        )
        return True

    for sched in queries.maintenance_due(db, now):
        printer = db.get(m.Printer, sched.printer_id) if sched.printer_id else None
        # Page-threshold schedules also require the page count to be reached.
        if sched.page_threshold and printer and printer.page_count is not None:
            if printer.page_count < sched.page_threshold:
                continue
        due_str = sched.next_due.date().isoformat() if sched.next_due else "due"
        key = f"maintenance:{sched.id}:{due_str}"
        active_keys.add(key)
        label = _printer_label(printer) if printer else (sched.model or "fleet")
        if _open_maintenance_alert(
            key,
            f"Maintenance due: {sched.name} ({label})",
            f"'{sched.name}' is due as of {due_str}.",
            printer,
        ):
            opened += 1

    # Component-life schedules: open when a matching component-life Supply row
    # has dropped to (or below) the schedule's life_threshold percent. Dedupe
    # per (schedule, printer); the key drops out — and the alert auto-resolves
    # below — once the part is serviced and its % climbs back above threshold.
    for sched in queries.component_maintenance_schedules(db):
        ctype = sched.component_type
        threshold = sched.life_threshold
        if ctype is None or threshold is None:
            continue
        for printer in _component_schedule_printers(db, sched):
            low = [
                s
                for s in printer.supplies
                if _component_supply_matches(s, ctype)
                and s.level_pct is not None
                and s.level_pct <= threshold
            ]
            if not low:
                continue
            worst = min(low, key=lambda s: s.level_pct)
            key = f"maintenance:component:{sched.id}:printer:{printer.id}:{ctype}"
            active_keys.add(key)
            part = (worst.description or ctype).strip()
            if _open_maintenance_alert(
                key,
                f"Maintenance due: {sched.name} ({_printer_label(printer)})",
                f"{part} life at {worst.level_pct:.0f}% "
                f"(threshold {threshold:.0f}%) for '{sched.name}'.",
                printer,
            ):
                opened += 1

    # Resolve maintenance alerts whose schedule is no longer due (next_due rolled
    # forward, or a component's life climbed back above threshold / was serviced).
    # Resolve in both open AND acknowledged states so an ack'd maintenance alert
    # clears once serviced.
    close_channels = [rc.channel for rc in candidates]
    resolved = 0
    for alert in db.scalars(
        select(m.Alert).where(
            m.Alert.state.in_(_LIVE_STATES),
            m.Alert.type == m.AlertConditionType.maintenance_due,
        )
    ):
        if alert.dedupe_key not in active_keys:
            alert.state = m.AlertState.resolved
            alert.resolved_at = now
            _close_ticket_for(alert, close_channels)
            resolved += 1

    db.commit()
    return {"maintenance_opened": opened, "maintenance_resolved": resolved}


# --------------------------------------------------------------------------- #
# Supply-depletion forecast (days-to-empty from a regression over recent levels)
# --------------------------------------------------------------------------- #
# Confidence gate: below these the consumption slope is too noisy to trust, so
# forecast_days_to_empty returns None rather than a number nobody should reorder
# against. Two points over a few days is the floor the older two-point estimate
# implicitly assumed; the regression keeps that floor while smoothing the rest.
FORECAST_MIN_POINTS = 2          # need at least a baseline + a follow-up reading
FORECAST_MIN_HISTORY_DAYS = 3.0  # ...spanning at least this long (matches RUNWAY_MIN_HISTORY_DAYS)
# How far back the forecast pass reads. `readings` is append-only and has no
# retention, so an unwindowed scan loads every row ever recorded for every
# printer -- on every 60s cycle, under the leader lock. That grows without
# bound and drags alert latency down with it as a deployment ages. A month is
# far more than the fit needs (it segments to the current cartridge anyway).
FORECAST_HISTORY_WINDOW_DAYS = 30


def forecast_days_to_empty(
    readings: list[tuple[datetime, float]], refill_tolerance: float = 5.0
) -> Optional[float]:
    """Days until level→0, from a least-squares fit over the recent depleting segment.

    Replaces the old first-point/last-point slope (which was maximally
    noise-sensitive) with an ordinary least-squares regression of level_pct on
    time across every point in the segment, so a single jittery reading no
    longer swings the estimate. The refill/cartridge-swap handling is preserved:
    a jump up of more than ``refill_tolerance`` points is treated as a fresh
    cartridge and resets the baseline, so a spent cartridge's slope isn't
    averaged against the new one.

    Returns ``None`` (the existing "no estimate" contract shared with
    ``central.queries.supply_runway``) when the series is rising/flat, or when
    the surviving segment doesn't clear the confidence gate
    (``FORECAST_MIN_POINTS`` points spanning ``FORECAST_MIN_HISTORY_DAYS`` days).
    The number returned is days-to-empty measured from the most recent reading,
    using the regression-predicted level there (== the observed level for a
    clean linear series, so legacy expectations are unchanged).
    """
    points = sorted([(t, lvl) for t, lvl in readings if lvl is not None], key=lambda p: p[0])
    if len(points) < FORECAST_MIN_POINTS:
        return None
    start = 0
    for i in range(1, len(points)):
        if points[i][1] > points[i - 1][1] + refill_tolerance:
            start = i  # refill detected — baseline resets here
    seg = points[start:]
    if len(seg) < FORECAST_MIN_POINTS:
        return None

    t0 = seg[0][0]
    # x in days since the segment's first reading; y in percent remaining.
    xs = [(t - t0).total_seconds() / 86400.0 for t, _ in seg]
    ys = [lvl for _, lvl in seg]
    span = xs[-1] - xs[0]
    if span < FORECAST_MIN_HISTORY_DAYS:
        return None  # not enough elapsed history to trust the slope yet

    n = len(seg)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0:
        return None  # all readings at the same instant — no slope
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov_xy / var_x  # percent change per day (negative when depleting)
    rate = -slope  # percent consumed per day
    if rate <= 0:
        return None  # not depleting (rising or flat fit)

    intercept = mean_y - slope * mean_x
    level_now = intercept + slope * xs[-1]  # fitted level at the latest reading
    if level_now <= 0:
        return 0.0  # already projected empty
    return round(level_now / rate, 1)


def forecast_supplies(db: Session, now: Optional[datetime] = None) -> dict:
    """Forecast each supply's days-to-empty, persist it, and raise reorder alerts.

    Three things happen per approved printer, per ``(type, color)`` supply:

      1. Fit ``forecast_days_to_empty`` over the supply's reading history.
      2. Persist the result onto the matching ``Supply`` row
         (``days_to_empty`` + ``forecast_at``) so dashboards/portal/reports read
         it instead of re-fitting on every render. A supply with no trustworthy
         estimate is cleared back to ``None``.
      3. If the estimate is at/under the operator's reorder lead-time
         (``alerts.reorder_lead_days``), open a ``predicted_depletion`` alert
         deduped PER (printer, supply) — not per printer, so a color device
         with three depleting toners raises three actionable alerts instead of
         one storm-prone aggregate. The dedupe / auto-resolve machinery is the
         same scaffolding the rule engine uses: keys re-added this pass stay
         open, keys that drop out (estimate recovered, or the cartridge was
         swapped/refilled so the recent segment no longer projects empty) are
         resolved.
    """
    now = now or _now()
    runtime = load_settings(db)
    candidates = routable_channels(db, runtime)
    lead_days = runtime.get("alerts.reorder_lead_days", 14)
    history_since = now - timedelta(days=FORECAST_HISTORY_WINDOW_DAYS)

    flagged = 0
    forecasted = 0
    opened = 0
    active_keys: set[str] = set()

    for printer in db.scalars(
        select(m.Printer).where(m.Printer.discovery_state == m.DiscoveryState.approved)
    ):
        # Index this printer's Supply rows by (type, color) so a forecast keyed
        # off the snapshot history lands on the right cartridge.
        supplies_by_key: dict[str, m.Supply] = {}
        for supply in printer.supplies:
            supplies_by_key[f"{supply.type.value}:{supply.color}"] = supply

        # Build per-(type,color) level series from supply_snapshot history.
        series: dict[str, list[tuple[datetime, float]]] = {}
        for r in db.scalars(
            select(m.Reading)
            .where(
                m.Reading.printer_id == printer.id,
                m.Reading.supply_snapshot.is_not(None),
                m.Reading.ts >= history_since,
            )
            .order_by(m.Reading.ts.asc())
        ):
            for snap in r.supply_snapshot or []:
                lvl = snap.get("level_pct")
                if lvl is None:
                    continue
                key = f"{snap.get('type')}:{snap.get('color')}"
                series.setdefault(key, []).append((_aware(r.ts), float(lvl)))

        for key, pts in series.items():
            supply = supplies_by_key.get(key)
            dte = forecast_days_to_empty(pts)
            # Persist onto the supply row (None clears a stale estimate).
            if supply is not None:
                supply.days_to_empty = dte
                supply.forecast_at = now if dte is not None else None
                if dte is not None:
                    forecasted += 1
            if dte is None or dte > lead_days:
                continue
            flagged += 1
            if supply is None:
                continue  # snapshot for a cartridge we no longer track — nothing to alert on
            dedupe_key = f"forecast:printer:{printer.id}:supply:{supply.id}"
            active_keys.add(dedupe_key)
            label = supply.color or supply.type.value
            title = f"Reorder {label} for {_printer_label(printer)}"
            detail = (
                f"{label} is forecast to run out in ~{dte:.0f} day(s) "
                f"(reorder lead time {lead_days} day(s))."
            )
            if _open_forecast_alert(
                db, dedupe_key, title, detail, printer=printer,
                candidates=candidates, now=now, runtime=runtime,
            ):
                opened += 1

    close_channels = [rc.channel for rc in candidates]
    resolved = _resolve_stale_forecasts(db, active_keys, now, close_channels)
    db.commit()
    # ``supplies_forecast_low`` keeps its historical meaning (count at/under the
    # lead-time threshold) so existing callers/tests reading that key still work.
    return {
        "supplies_forecast_low": flagged,
        "supplies_forecasted": forecasted,
        "forecast_alerts_opened": opened,
        "forecast_alerts_resolved": resolved,
    }


# --------------------------------------------------------------------------- #
# Notification delivery retry / dead-letter
# --------------------------------------------------------------------------- #
def retry_deliveries(db: Session, now: Optional[datetime] = None) -> dict:
    """Re-send due failed/pending notification deliveries with exponential backoff.

    A channel send that failed when its alert opened was persisted as a
    NotificationDelivery row; this job re-sends it once its backoff window has
    elapsed, marks it delivered on success, and dead-letters it after the
    configured max-attempts cap. Idempotent and safe to run every cycle --
    delivered/dead rows are terminal and never re-sent (see channels.delivery).
    """
    return _retry_due(db, load_settings(db), now or _now())


def flush_quiet_hours(db: Session, now: Optional[datetime] = None) -> dict:
    """Deliver notifications held by a quiet-hours window, batched into a digest.

    A held notification is a ``deferred`` delivery row whose ``next_attempt_at``
    is the instant its window closes, so this job needs no schedule of its own --
    it simply finds the rows that have come due. One digest per client rather than
    one message per alert, which is the whole point of deferring instead of
    suppressing: the operator hears about the night once, in the morning.
    """
    return _flush_deferred(db, load_settings(db), now or _now())


def _open_forecast_alert(
    db: Session,
    dedupe_key: str,
    title: str,
    detail: str,
    *,
    printer: m.Printer,
    candidates: Optional[list] = None,
    now: Optional[datetime] = None,
    runtime: Optional[dict] = None,
) -> Optional[m.Alert]:
    """Open a predicted-depletion alert if one isn't already live for the key.

    Rule-less (no AlertRule), so it routes by the printer's scope and delivers
    durably via ``_notify_alert`` -- the same path as rule-driven and maintenance
    alerts (per-tenant routing, retry/dead-letter, FreeScout ticket capture, and
    escalation bookkeeping). Returns the new alert, or ``None`` if one was open.
    """
    if _find_open_alert(db, dedupe_key) is not None:
        return None
    now = now or _now()
    alert = m.Alert(
        rule_id=None,
        printer_id=printer.id,
        type=m.AlertConditionType.predicted_depletion,
        severity=m.EventSeverity.warning,
        state=m.AlertState.open,
        title=title,
        detail=detail,
        dedupe_key=dedupe_key,
        escalation_level=0,
    )
    db.add(alert)
    db.flush()  # assign alert.id
    _notify_alert(
        db, alert, rule=None, printer=printer,
        candidates=candidates or [], now=now, runtime=runtime,
    )
    return alert


def _resolve_stale_forecasts(
    db: Session, active_keys: set[str], now: datetime, channels=None
) -> int:
    """Resolve open predicted-depletion alerts whose forecast no longer holds.

    A key drops out of ``active_keys`` when the supply recovered above the
    lead-time (or the cartridge was swapped/refilled, so the refill-aware fit no
    longer projects it empty within the window). Scoped to forecast alerts so it
    can't touch rule-driven or maintenance alerts, which own their own lifecycle.
    A resolved alert that opened a FreeScout ticket gets it auto-closed too.
    """
    resolved = 0
    for alert in db.scalars(
        select(m.Alert).where(
            # _LIVE_STATES, not open: an ACKNOWLEDGED reorder alert must resolve
            # when its supply recovers too. Filtering on open alone left it
            # acknowledged forever, and because dedupe suppresses while an alert
            # is live, that one stuck row silently blocked every future reorder
            # alert for the same cartridge.
            m.Alert.state.in_(_LIVE_STATES),
            m.Alert.type == m.AlertConditionType.predicted_depletion,
        )
    ):
        if alert.dedupe_key not in active_keys:
            _close_ticket_for(alert, channels)
            alert.state = m.AlertState.resolved
            alert.resolved_at = now
            resolved += 1
    return resolved


def sync_directories(db: Session, now: Optional[datetime] = None) -> dict:
    """Refresh each enabled directory connection that is due.

    Interval-gated off each connection's own ``last_sync_at`` rather than a
    global marker, so adding a customer does not reset everybody else's clock
    and one slow tenant does not starve the rest.

    A failing connection is recorded on its own row and skipped; it never
    aborts the loop. A directory being unreachable is a normal Tuesday, and it
    must not stop the other customers syncing -- still less take down a worker
    cycle that also has alerts to deliver.
    """
    from central.directory.sync import run_connection

    settings = load_settings(db)
    if not settings.get("directory.sync_enabled", True):
        return {"directory_sync": "disabled"}

    now = _aware(now) or _now()
    try:
        interval = max(1, int(settings.get("directory.sync_interval_min", 60)))
    except (TypeError, ValueError):
        interval = 60
    cutoff = now - timedelta(minutes=interval)

    conns = list(db.scalars(
        select(m.DirectoryConnection).where(m.DirectoryConnection.enabled.is_(True))
    ))
    ran = ok = failed = 0
    for conn in conns:
        last = _aware(conn.last_sync_at)
        if last is not None and last > cutoff:
            continue
        ran += 1
        result = run_connection(db, conn)
        if result.get("error"):
            failed += 1
        else:
            ok += 1
        # Commit per connection: a later failure must not roll back a customer
        # whose sync already succeeded.
        db.commit()

    if not ran:
        return {}
    return {"directory_sync": {"ran": ran, "ok": ok, "failed": failed}}
