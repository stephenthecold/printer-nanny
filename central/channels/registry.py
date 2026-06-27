"""Build channel implementations and dispatch notifications.

Channels are driven by the Settings page: ``active_channels(runtime)`` returns the
channels enabled there, so turning a destination on is just a toggle + creds in
the UI -- no channel rows to manage. ``build_channel`` still exists for the
optional per-rule NotificationChannel rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central.channels.base import ChannelResult, Notification, NotificationChannel
from central.channels.email import EmailChannel
from central.channels.freescout import FreeScoutChannel
from central.channels.slack import SlackChannel
from central.channels.teams import TeamsChannel
from central.channels.webhook import WebhookChannel

_IMPLS = {
    m.ChannelType.email: EmailChannel,
    m.ChannelType.freescout: FreeScoutChannel,
    m.ChannelType.teams: TeamsChannel,
    m.ChannelType.webhook: WebhookChannel,
    m.ChannelType.slack: SlackChannel,
}


def build_channel(
    row: m.NotificationChannel, runtime: Optional[dict] = None
) -> NotificationChannel:
    impl = _IMPLS[row.type]
    return impl(name=row.name, config=row.config or {}, runtime=runtime)


def active_channels(runtime: dict) -> List[NotificationChannel]:
    """Channels enabled on the Settings page, as ready-to-use instances.

    The email channel receives a lazy DB session factory so OAuth send paths can
    persist refreshed access tokens. Worker code constructs settings once per
    dispatch cycle; the channel only opens a session when an OAuth refresh runs.
    """
    channels: List[NotificationChannel] = []
    if runtime.get("email.enabled") and runtime.get("email.default_recipients"):
        from central.db import SessionLocal

        channels.append(EmailChannel(
            "Email",
            config={"to": runtime["email.default_recipients"]},
            runtime=runtime,
            db_factory=SessionLocal,
        ))
    if runtime.get("freescout.enabled"):
        channels.append(FreeScoutChannel("FreeScout", {}, runtime))
    if runtime.get("teams.enabled"):
        channels.append(TeamsChannel("Teams", {}, runtime))
    if runtime.get("webhook.enabled"):
        channels.append(WebhookChannel("Webhook", {}, runtime))
    if runtime.get("slack.enabled"):
        channels.append(SlackChannel("Slack", {}, runtime))
    return channels


@dataclass
class RoutableChannel:
    """A built channel paired with the routing metadata used to address it.

    ``row_id`` is the ``NotificationChannel.id`` for DB-row channels (so an
    AlertRule.channel_ids list can restrict dispatch to specific channels) or
    ``None`` for the global runtime channels enabled on the Settings page.
    ``scope`` / ``scope_id`` mirror the NotificationChannel columns: a channel
    scoped to client/site X only receives alerts whose printer belongs to X.
    """

    channel: NotificationChannel
    row_id: Optional[int]
    scope: m.AlertScope
    scope_id: Optional[int]


def routable_channels(db: Session, runtime: dict) -> List[RoutableChannel]:
    """All addressable channels: the global Settings-page ones plus DB rows.

    Global channels (no row id) carry ``scope=global`` so they match every
    printer. NotificationChannel rows keep their own scope/scope_id, which the
    router filters against the alert's printer.
    """
    out: List[RoutableChannel] = []
    for ch in active_channels(runtime):
        out.append(RoutableChannel(ch, None, m.AlertScope.global_, None))
    for row in db.scalars(
        select(m.NotificationChannel).where(m.NotificationChannel.enabled.is_(True))
    ):
        out.append(
            RoutableChannel(
                build_channel(row, runtime), row.id, row.scope, row.scope_id
            )
        )
    return out


def _scope_matches(
    scope: m.AlertScope, scope_id: Optional[int], printer: Optional[m.Printer]
) -> bool:
    """Does a scoped channel apply to this alert's printer?

    Global scope matches everything. Client/site/printer scopes match only when
    the alert carries a printer that belongs to that client/site/printer. An
    agent-only alert (no printer) matches global channels only.
    """
    if scope == m.AlertScope.global_ or scope_id is None:
        return True
    if printer is None:
        return False
    if scope == m.AlertScope.client:
        return printer.client_id == scope_id
    if scope == m.AlertScope.site:
        return printer.site_id == scope_id
    if scope == m.AlertScope.printer:
        return printer.id == scope_id
    return True


def route_channels(
    candidates: Iterable[RoutableChannel],
    *,
    rule: Optional[m.AlertRule] = None,
    printer: Optional[m.Printer] = None,
    severity: Optional[str] = None,
) -> List[NotificationChannel]:
    """Pick the channels an alert dispatches to.

    Applies, in order:
      1. ``rule.channel_ids`` — when set (non-empty), only DB-row channels whose
         id is listed are eligible (global runtime channels are excluded, since
         the rule has explicitly named its destinations).
      2. ``NotificationChannel.scope`` / ``scope_id`` — a channel scoped to a
         client/site/printer only fires for alerts whose printer belongs to it.
      3. per-channel ``min_severity`` — drop channels whose minimum exceeds the
         alert's severity (generalized from the Slack/webhook-only filter).
    """
    rule_ids = set(rule.channel_ids) if rule and rule.channel_ids else None
    picked: List[NotificationChannel] = []
    for rc in candidates:
        if rule_ids is not None:
            # Rule named explicit destinations: only those rows qualify.
            if rc.row_id is None or rc.row_id not in rule_ids:
                continue
        if not _scope_matches(rc.scope, rc.scope_id, printer):
            continue
        if severity is not None and not rc.channel.meets_severity(severity):
            continue
        picked.append(rc.channel)
    return picked


def dispatch(
    note: Notification, channels: Iterable[NotificationChannel]
) -> List[tuple]:
    """Send ``note`` to each built channel; return (channel_name, ChannelResult).

    The per-channel severity gate is applied here too so a channel below the
    notification's severity is skipped uniformly (not just Slack/webhook). The
    router already filters by severity when given one, so this is a belt-and-
    suspenders guard for direct dispatch callers (e.g. scheduled reports pass
    no severity and are unaffected — info-level passes every gate).
    """
    results: List[tuple] = []
    for channel in channels:
        try:
            if not channel.meets_severity(note.severity):
                results.append((
                    channel.name,
                    ChannelResult(
                        ok=True,
                        detail=(
                            f"skipped: severity {note.severity} below "
                            f"{channel.min_severity()}"
                        ),
                    ),
                ))
                continue
            results.append((channel.name, channel.send(note)))
        except Exception as exc:  # noqa: BLE001 - one bad channel shouldn't kill the rest
            results.append((channel.name, ChannelResult(ok=False, detail=f"unhandled: {exc}")))
    return results
