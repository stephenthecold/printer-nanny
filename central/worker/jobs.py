"""Background jobs: heartbeat/offline detection, alert evaluation, maintenance,
and supply-depletion forecasting. Each function is independently runnable and
returns a small summary dict so the worker loop (and tests) can assert on it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from central import models as m
from central import queries
from central import supplies as supplies_lib
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
from central.events.delivery import deliver_due as _deliver_events
from central.events.emit import (
    emit_alert_opened,
    emit_alert_resolved,
    emit_printer_offline,
)
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
            # Emitted on the transition, inside the same transaction as the
            # status write -- so a rolled-back cycle tells nobody a printer went
            # down, and a printer that flaps offline/online/offline produces two
            # events rather than one (the key carries the last_seen it went
            # stale from; see emit_printer_offline).
            emit_printer_offline(db, printer, at=now)
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


# An occurrence-rate rule's window is operator-typed, and an unbounded one is a
# scan of every event ever recorded -- the trap FORECAST_HISTORY_WINDOW_DAYS
# already had to be walked back from, on a table that is likewise append-only.
# 30 days is well past any plausible "N per day / per shift / per hour" rule and
# is enforced in BOTH places a window can arrive: the form, so an operator is
# told, and the evaluator, so a hand-edited row cannot bypass it.
OCCURRENCE_MAX_WINDOW_MINUTES = 60 * 24 * 30

# LIKE metacharacters in an operator-typed match string. Left unescaped, a code
# of "100%" silently becomes "match everything", which is a rule that reports a
# flood the operator never asked about rather than failing visibly.
_LIKE_ESCAPE = "\\"


def _like_contains(needle: str) -> str:
    """Build a case-insensitive CONTAINS pattern with metacharacters escaped."""
    escaped = (
        needle.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def _severities_at_or_above(floor: m.EventSeverity) -> list:
    return [sev for sev, rank in _SEVERITY_RANK.items() if rank >= _SEVERITY_RANK.get(floor, 0)]


def _occurrence_window_minutes(rule: m.AlertRule) -> Optional[int]:
    """The rule's rolling window, clamped. None when the rule cannot be evaluated.

    A rule with no window (or a nonsensical one) is skipped rather than given a
    default: the window is half the operator's statement of intent, and inventing
    it would make a rule fire on a period nobody chose.
    """
    raw = rule.window_minutes
    if raw is None:
        return None
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return None
    return min(minutes, OCCURRENCE_MAX_WINDOW_MINUTES)


def _occurrence_counts(
    db: Session, rule: m.AlertRule, since: datetime
) -> dict:
    """Matching event counts per printer in ``rule``'s scope, since ``since``.

    ONE grouped query per rule, not one COUNT per printer: the printer loop
    below already runs per rule, and a per-printer count would put a query per
    printer per rule on every worker cycle (~1,000 on a 500-printer fleet with
    two rate rules), which is the N+1 shape this codebase keeps having to undo.
    The scope filter is a join onto printers rather than an ``IN (:ids)`` list
    because the id list would overrun SQLite's 999-variable limit on a fleet
    that size -- a failure that only appears in production.

    Bounded by construction: ``since`` is always set, and both filters are
    parameterised.
    """
    stmt = (
        select(m.PrinterEvent.printer_id, func.count(m.PrinterEvent.id))
        .join(m.Printer, m.Printer.id == m.PrinterEvent.printer_id)
        .where(
            m.PrinterEvent.ts >= since,
            m.Printer.discovery_state == m.DiscoveryState.approved,
        )
        .group_by(m.PrinterEvent.printer_id)
    )
    if rule.scope == m.AlertScope.client and rule.scope_id:
        stmt = stmt.where(m.Printer.client_id == rule.scope_id)
    elif rule.scope == m.AlertScope.site and rule.scope_id:
        stmt = stmt.where(m.Printer.site_id == rule.scope_id)
    elif rule.scope == m.AlertScope.printer and rule.scope_id:
        stmt = stmt.where(m.Printer.id == rule.scope_id)

    code = (rule.match_code or "").strip()
    if code:
        stmt = stmt.where(
            m.PrinterEvent.code.is_not(None),
            m.PrinterEvent.code.ilike(_like_contains(code), escape=_LIKE_ESCAPE),
        )
    if rule.match_min_severity is not None:
        stmt = stmt.where(
            m.PrinterEvent.severity.in_(_severities_at_or_above(rule.match_min_severity))
        )
    return {pid: count for pid, count in db.execute(stmt)}


def _fmt_window(minutes: Optional[int]) -> str:
    """A window as an operator would say it: 45m, 6h, 24h, 7d."""
    if not minutes:
        return "0m"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _occurrence_subject(rule: m.AlertRule) -> str:
    """What the rule is counting, for an alert title."""
    code = (rule.match_code or "").strip()
    return f"'{code}' events" if code else "events"


def _occurrence_criteria(rule: m.AlertRule) -> str:
    """A one-line restatement of what was counted.

    The alert has to be self-explanatory in an email that carries no link back
    to the rule: "12 events" is unactionable without "...whose code contains
    'jam', at warning or above".
    """
    code = (rule.match_code or "").strip()
    parts = [
        f"Counting events whose code contains '{code}'" if code
        else "Counting every event"
    ]
    if rule.match_min_severity is not None:
        parts.append(f"at {rule.match_min_severity.value} or above")
    return ", ".join(parts) + "."


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


def _flap_cooldown_minutes(runtime: Optional[dict], cap_minutes: Optional[int]) -> int:
    """The effective flap-cooldown window, in minutes. 0 disables folding.

    ``cap_minutes`` exists for occurrence-rate rules and is the one place the
    cooldown is allowed to be shortened. See ``_revive_flapping_alert`` for why.
    """
    minutes = int((runtime or {}).get("alerts.renotify_cooldown_min", 0) or 0)
    if cap_minutes is not None:
        minutes = min(minutes, max(0, int(cap_minutes)))
    return minutes


def _flap_suffix(detail: str, flap_count: int, minutes: int) -> str:
    """Annotate a detail string with the flap bookkeeping, if it has flapped."""
    if not flap_count:
        return detail
    return "%s (re-opened; flapped %d× within %dm)" % (detail, flap_count, minutes)


def _revive_flapping_alert(
    db: Session,
    dedupe_key: str,
    detail: str,
    now: datetime,
    runtime: Optional[dict],
    cap_minutes: Optional[int] = None,
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

    ``cap_minutes`` shortens the cooldown for a single call, and exists for
    occurrence-rate rules. Folding asserts "this is the same incident", and for
    a rate condition that claim is only true while the two firings are counting
    overlapping events -- i.e. within the rule's own window W. A 10-minute
    "5 jams" rule under the default 30-minute cooldown would otherwise have a
    genuinely NEW burst -- five fresh jams sharing not one event with the
    previous alert -- folded in silently, with no notification: the damping
    mechanism defeating the very feature that exists to measure repetition.
    Capping at W makes the two cases distinguishable by the only evidence
    available, which is whether any counted event is common to both.

    Returns the revived alert, or None when nothing is eligible (no recent
    resolve, or the cooldown is disabled).
    """
    minutes = _flap_cooldown_minutes(runtime, cap_minutes)
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
    alert.detail = _flap_suffix(detail, alert.flap_count, minutes)
    # Damping suppresses the *notification*, not the fact. A subscriber told the
    # condition resolved and never told it came back holds a state that is
    # false, so the event goes out even though nobody is paged -- see
    # events.emit.alert_key for why the key carries the flap generation.
    emit_alert_opened(db, alert)
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
    cooldown_cap_min: Optional[int] = None,
    refresh_detail: bool = False,
) -> Optional[m.Alert]:
    """Open an alert if one isn't already live for this dedupe_key. Returns it (or None).

    Returns None both when an alert is already live (dedupe) and when a recent
    resolve was revived as a flap (see ``_revive_flapping_alert``) -- in neither
    case did a NEW alert open, so callers must not count it as one. ``stats``, if
    given, has its ``flapped`` key incremented so the cycle summary can report
    damping that happened instead of a notification.

    ``refresh_detail`` rewrites a *live* alert's detail from this pass's text
    without notifying. It is off by default because for a state condition the
    detail barely moves ("9%" vs "8%"), and on for occurrence-rate rules because
    there the number IS the alert: dedupe would otherwise leave "10 jams in 24h"
    on screen while the printer sits at sixty, which is the alert asserting
    something untrue rather than merely stale. Silent by design -- re-notifying
    per cycle is the noise every other mechanism here exists to stop, and
    escalation already covers "still not fixed".
    """
    live = _find_open_alert(db, dedupe_key)
    if live is not None:
        if refresh_detail:
            live.detail = _flap_suffix(
                detail,
                live.flap_count or 0,
                _flap_cooldown_minutes(runtime, cooldown_cap_min),
            )
        return None
    now = now or _now()
    revived = _revive_flapping_alert(
        db, dedupe_key, detail, now, runtime, cap_minutes=cooldown_cap_min
    )
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
    # Emitted before the notification dispatch, and independently of it: a
    # suppression window silences a *person*, not an integration, and the event
    # surface is how an MSP's own systems learn the fleet changed.
    emit_alert_opened(db, alert, printer)
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


def _resolve_stale(
    db: Session,
    active_keys: set[str],
    channels=None,
    now: Optional[datetime] = None,
) -> int:
    """Resolve live rule-driven alerts whose condition no longer holds this run.

    Covers both cleared conditions (key not re-added) and alerts orphaned by a
    rule that was disabled/deleted (its key is never re-added). Resolves alerts
    in BOTH open and acknowledged states -- an ack'd alert whose condition clears
    must still auto-resolve, otherwise it's stuck open forever. Maintenance and
    predicted-depletion alerts own their own lifecycle (check_maintenance_due /
    forecast_supplies) and are skipped; a resolved alert that opened a FreeScout
    ticket gets it auto-closed.

    ``resolved_at`` is stamped from the pass's ``now``, not from a fresh
    wall-clock read. The flap window is measured backwards from ``now`` against
    exactly this column, so two different clocks inside one cycle make "did this
    re-fire inside the cooldown?" depend on how long the cycle took -- and make
    it unanswerable at all under an injected clock.
    """
    now = now or _now()
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
            alert.resolved_at = now
            _close_ticket_for(alert, channels)
            emit_alert_resolved(db, alert)
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

        # Likewise once per rule: one grouped, windowed count for every printer
        # in scope, read out of a dict inside the loop below.
        occurrence_window = occurrence_counts = None
        occurrence_n = occurrence_floor = 0
        if rule.condition_type == m.AlertConditionType.occurrence_rate:
            occurrence_window = _occurrence_window_minutes(rule)
            occurrence_n = int(rule.threshold or 0)
            if occurrence_window is None or occurrence_n < 1:
                # Unusable rule: without a window there is nothing to count
                # over, and a count of zero is satisfied by every printer that
                # has ever emitted nothing, forever. Skipped rather than
                # defaulted -- both halves are the operator's statement of what
                # "too often" means, and neither can be guessed for them.
                continue
            occurrence_counts = _occurrence_counts(
                db, rule, now - timedelta(minutes=occurrence_window)
            )
            # Hysteresis, same philosophy as alerts.supply_deadband_pct: open at
            # the count, but hold open until it falls a margin BELOW it. A
            # rolling window sheds occurrences on its own as they age out, so
            # without a margin every rate alert resolves the moment the oldest
            # event leaves the window and re-opens on the very next one.
            occurrence_floor = occurrence_n - (
                occurrence_n
                * float(runtime.get("alerts.occurrence_clear_margin_pct", 0) or 0)
                / 100.0
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
                    # A receptacle's level is how FULL it is, so "below N%" is
                    # the healthy end for it: an emptied waste box reading 2
                    # opened "Low waste at 2%" against a part that had just been
                    # serviced, and would only resolve once the box filled up
                    # again. Skipped rather than inverted -- this rule is
                    # "supply_below" and a nearly-full container is a different
                    # condition, surfaced by central.reorder. See
                    # central.supplies.
                    if supplies_lib.is_receptacle(supply):
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

            elif rule.condition_type == m.AlertConditionType.occurrence_rate:
                # Rate, not state: how many matching events landed inside the
                # rolling window, versus how many the operator called too many.
                count = (occurrence_counts or {}).get(printer.id, 0)
                key = f"rule:{rule.id}:printer:{printer.id}:rate"
                window_label = _fmt_window(occurrence_window)
                subject = _occurrence_subject(rule)
                if count >= occurrence_n:
                    active_keys.add(key)
                    title = f"Frequent {subject} on {_printer_label(printer)}"
                    detail = (
                        f"{count} matching event(s) in the last {window_label} "
                        f"(threshold {occurrence_n}). {_occurrence_criteria(rule)}"
                    )
                    if _open_alert(
                        db, rule, key, title, detail, printer=printer,
                        candidates=candidates, now=now, runtime=runtime,
                        stats=stats,
                        # Two departures from the other condition types, both
                        # because a rate alert's content IS its number. See
                        # _open_alert and _revive_flapping_alert for the full
                        # reasoning.
                        cooldown_cap_min=occurrence_window,
                        refresh_detail=True,
                    ):
                        opened += 1
                elif count > occurrence_floor:
                    live = _find_open_alert(db, key)
                    if live is not None:
                        # Inside the recovery margin with the alert still live:
                        # keep the key active so the resolve pass leaves it
                        # alone, and say on the alert that it is receding rather
                        # than leaving the opening count on screen.
                        active_keys.add(key)
                        live.detail = _flap_suffix(
                            f"{count} matching event(s) in the last {window_label} — "
                            f"holding open until it drops below {occurrence_floor:.0f} "
                            f"(threshold {occurrence_n}). {_occurrence_criteria(rule)}",
                            live.flap_count or 0,
                            _flap_cooldown_minutes(runtime, occurrence_window),
                        )

    # Pass the unwrapped candidate channels so a resolved alert's FreeScout
    # ticket can be auto-closed (route metadata isn't needed to close a ticket).
    close_channels = [rc.channel for rc in candidates]
    resolved = _resolve_stale(db, active_keys, close_channels, now=now)
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
        emit_alert_opened(db, alert, printer)
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
            emit_alert_resolved(db, alert, resolved_at=now)
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
# The same confidence gate expressed on the PAGES axis. A segment spanning three
# printed pages has a slope, and it is meaningless -- toner level readings are
# coarse (many devices report in 1% or 10% steps), so the fit needs enough pages
# for a real percentage change to have occurred. 50 pages is the floor at which
# a 1% step is a plausible measurement rather than quantisation noise.
FORECAST_MIN_PAGES_SPAN = 50.0


def _fit_depleting_segment(
    points: list, min_span: float, refill_tolerance: float = 5.0
) -> Optional[tuple]:
    """Least-squares fit of supply level against an increasing axis.

    ``points`` is [(x, level_pct)] where x is any monotonically increasing
    measure of consumption -- elapsed days for the days-to-empty forecast,
    cumulative page count for the pages-to-empty one. The maths is identical on
    both axes, which is exactly why it lives here once: the two forecasts must
    never be able to disagree about what counts as a refill, a confidence floor,
    or a depleting series.

    Returns ``(rate_per_x, level_now)`` -- percent consumed per unit of x, and
    the FITTED level at the last point -- or ``None`` when there is no
    trustworthy estimate. ``None`` covers: too few points, a segment spanning
    less than ``min_span``, no variation in x, and a series that is rising or
    flat rather than depleting.

    A jump UP of more than ``refill_tolerance`` points is a fresh cartridge, and
    resets the baseline to that index, so a spent cartridge's slope is never
    averaged against its replacement's.
    """
    if len(points) < FORECAST_MIN_POINTS:
        return None
    start = 0
    for i in range(1, len(points)):
        if points[i][1] > points[i - 1][1] + refill_tolerance:
            start = i  # refill detected — baseline resets here
    seg = points[start:]
    if len(seg) < FORECAST_MIN_POINTS:
        return None

    # x is re-based on the segment's first point. The fit is invariant to this
    # shift, but the page-count axis carries absolute meter values (hundreds of
    # thousands), and squaring those in var_x throws away significant digits.
    x0 = seg[0][0]
    xs = [x - x0 for x, _ in seg]
    ys = [lvl for _, lvl in seg]
    if xs[-1] - xs[0] < min_span:
        return None  # not enough history to trust the slope yet

    n = len(seg)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0:
        return None  # every reading at the same x — no slope
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov_xy / var_x  # percent change per unit x (negative when depleting)
    rate = -slope  # percent consumed per unit x
    if rate <= 0:
        return None  # not depleting (rising or flat fit)

    intercept = mean_y - slope * mean_x
    level_now = intercept + slope * xs[-1]  # fitted level at the latest reading
    return rate, level_now


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
    if not points:
        return None
    t0 = points[0][0]
    # x in days since the first reading; y in percent remaining.
    fit = _fit_depleting_segment(
        [((t - t0).total_seconds() / 86400.0, lvl) for t, lvl in points],
        min_span=FORECAST_MIN_HISTORY_DAYS,
        refill_tolerance=refill_tolerance,
    )
    if fit is None:
        return None
    rate, level_now = fit
    if level_now <= 0:
        return 0.0  # already projected empty
    return round(level_now / rate, 1)


def forecast_pages_to_empty(
    readings: list, refill_tolerance: float = 5.0
) -> Optional[float]:
    """Pages until level→0, fitting supply level against the printer's page meter.

    ``readings`` is [(page_count, level_pct)]. This is deliberately a SEPARATE
    measure rather than ``days_to_empty * pages_per_day``, because the two answer
    different questions and the derived version inherits the weakness of the one
    it is derived from. Days-remaining is volatile -- a quiet week inflates it,
    a month-end run collapses it. Pages-remaining is a property of the cartridge:
    "about 400 pages left" means the same thing on a busy device and an idle one,
    it is directly comparable against the yield a cartridge is sold by, and it
    does not move when the customer simply stops printing for a fortnight.

    A page meter that goes BACKWARDS is a meter reset or a replaced formatter
    board, not negative printing; everything before the drop is discarded rather
    than fitted through, which would otherwise produce a confidently wrong
    negative consumption rate.

    Returns ``None`` on the same terms as ``forecast_days_to_empty``: an
    untrustworthy estimate is no estimate. In particular a printer that has not
    printed since the cartridge went in has no page-based rate at all (no
    variation in x), which is correct -- not "it will last forever".
    """
    # Input arrives in TIME order (that is how the caller reads it), which is what
    # makes a meter reset detectable at all: sorting by page count first would
    # silently interleave the post-reset readings among the pre-reset ones and
    # hide the discontinuity completely.
    raw = [
        (float(pages), float(lvl))
        for pages, lvl in readings
        if pages is not None and lvl is not None
    ]
    start = 0
    for i in range(1, len(raw)):
        if raw[i][0] < raw[i - 1][0]:
            start = i  # meter reset — everything before this is a different series
    points = sorted(raw[start:], key=lambda p: p[0])
    fit = _fit_depleting_segment(
        points,
        min_span=FORECAST_MIN_PAGES_SPAN,
        refill_tolerance=refill_tolerance,
    )
    if fit is None:
        return None
    rate, level_now = fit
    if level_now <= 0:
        return 0.0  # already projected empty
    # Whole pages: a tenth of a page is not a thing, and the false precision
    # would read as a measurement rather than an estimate.
    return float(round(level_now / rate))


def forecast_supplies(db: Session, now: Optional[datetime] = None) -> dict:
    """Forecast each supply's days-to-empty, persist it, and raise reorder alerts.

    Three things happen per approved printer, per ``(type, color)`` supply:

      1. Fit ``forecast_days_to_empty`` over the supply's reading history, and
         ``forecast_pages_to_empty`` over the same readings against the page
         meter. The second costs no extra query: ``page_count`` is a column on
         the Reading rows this pass is already loading.
      2. Persist both onto the matching ``Supply`` row (``days_to_empty``,
         ``pages_to_empty`` + ``forecast_at``) so dashboards/portal/reports and
         the reorder recommendations read them instead of re-fitting 30 days of
         readings on every render. A supply with no trustworthy estimate is
         cleared back to ``None``.
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

        # Build per-(type,color) level series from supply_snapshot history. Two
        # series per cartridge off the SAME rows: level against time, and level
        # against the page meter. Both stay in reading (time) order -- the pages
        # fit needs that to spot a meter reset.
        series: dict[str, list[tuple[datetime, float]]] = {}
        page_series: dict[str, list] = {}
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
                if r.page_count is not None:
                    page_series.setdefault(key, []).append((r.page_count, float(lvl)))

        for key, pts in series.items():
            supply = supplies_by_key.get(key)
            dte = forecast_days_to_empty(pts)
            pte = forecast_pages_to_empty(page_series.get(key) or [])
            # Persist onto the supply row (None clears a stale estimate).
            if supply is not None:
                supply.days_to_empty = dte
                supply.pages_to_empty = pte
                # forecast_at continues to stamp the DAYS estimate specifically,
                # which is what every existing reader of it means by it.
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


def deliver_events(db: Session, now: Optional[datetime] = None) -> dict:
    """POST every due outbound event to its subscribers, with backoff.

    Shares the notification path's backoff and dead-letter policy rather than
    owning a second one (see central.events.delivery). Bounded per cycle, because
    a backlog against one dead subscriber must not spend the cycle that also has
    alerts to deliver. Also prunes fully-delivered events past their retention.
    """
    return _deliver_events(db, load_settings(db), now or _now())


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
    emit_alert_opened(db, alert, printer)
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
            emit_alert_resolved(db, alert, resolved_at=now)
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


# --------------------------------------------------------------------------- #
# Outbound supply-reorder events (RECOMMEND-ONLY -- see central.reorder)
# --------------------------------------------------------------------------- #
# Machine state, not operator config: a plain app_settings row rather than a
# Spec, exactly like the report send markers in central.reports. load_settings
# ignores non-Spec keys, so it never leaks into the Settings UI.
REORDER_EMIT_MARKER = "reorder.last_emit_at"


def publish_reorder_recommendations(
    db: Session, now: Optional[datetime] = None
) -> dict:
    """Publish ``supply.reorder_recommended`` for every currently-recommended supply.

    Interval-gated (``reorder.emit_interval_min``, default daily) off a marker
    row. The worker cycles every 60 seconds and the recommendation set is stable
    for days at a time, so publishing every cycle would send an ERP the same
    facts 1,440 times a day. Each event still carries a stable ``dedupe_key``, so
    a consumer can recognise a repeat regardless of cadence.

    Nothing here writes a recommendation anywhere. The marker records only WHEN
    we last published -- it is notification bookkeeping of the same kind as the
    report send markers, not order state, and it must stay that way.

    Off by default: with no event bus installed, and with publishing a
    customer's fleet to an external system being an opt-in decision, the honest
    outcome of a default install is "nothing was sent", reported as such.
    """
    from central import reorder as _reorder

    settings = load_settings(db)
    if not settings.get("reorder.emit_events", False):
        return {"reorder_events": "disabled"}

    now = _aware(now) or _now()
    try:
        interval = max(1, int(settings.get("reorder.emit_interval_min", 1440)))
    except (TypeError, ValueError):
        interval = 1440
    last_raw = db.get(m.AppSetting, REORDER_EMIT_MARKER)
    if last_raw is not None and last_raw.value:
        try:
            last = _aware(datetime.fromisoformat(last_raw.value))
        except ValueError:
            last = None  # hand-edited/corrupt marker: publish rather than wedge
        if last is not None and last > now - timedelta(minutes=interval):
            return {}

    recs = _reorder.recommendations(
        db, thresholds=_reorder.ReorderThresholds.from_runtime(settings)
    )
    result = _reorder.publish_recommendations(db, recs)
    # The marker moves only when something was actually transmitted. A run that
    # published nothing because the bus is absent must not consume the interval
    # -- that would silently swallow the first real window after it is installed.
    if result.published:
        if last_raw is None:
            db.add(m.AppSetting(key=REORDER_EMIT_MARKER, value=now.isoformat()))
        else:
            last_raw.value = now.isoformat()
        db.commit()
    return {"reorder_events": result.as_dict()}
