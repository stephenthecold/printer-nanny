"""Build channel implementations and dispatch notifications.

Channels are driven by the Settings page: ``active_channels(runtime)`` returns the
channels enabled there (email / FreeScout), so turning FreeScout on is just a
toggle + creds in the UI — no channel rows to manage. ``build_channel`` still
exists for the optional per-rule NotificationChannel rows.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from central import models as m
from central.channels.base import ChannelResult, Notification, NotificationChannel
from central.channels.email import EmailChannel
from central.channels.freescout import FreeScoutChannel
from central.channels.teams import TeamsChannel

_IMPLS = {
    m.ChannelType.email: EmailChannel,
    m.ChannelType.freescout: FreeScoutChannel,
    m.ChannelType.teams: TeamsChannel,
}


def build_channel(
    row: m.NotificationChannel, runtime: Optional[dict] = None
) -> NotificationChannel:
    impl = _IMPLS[row.type]
    return impl(name=row.name, config=row.config or {}, runtime=runtime)


def active_channels(runtime: dict) -> List[NotificationChannel]:
    """Channels enabled on the Settings page, as ready-to-use instances."""
    channels: List[NotificationChannel] = []
    if runtime.get("email.enabled") and runtime.get("email.default_recipients"):
        channels.append(
            EmailChannel("Email", {"to": runtime["email.default_recipients"]}, runtime)
        )
    if runtime.get("freescout.enabled"):
        channels.append(FreeScoutChannel("FreeScout", {}, runtime))
    return channels


def dispatch(
    note: Notification, channels: Iterable[NotificationChannel]
) -> List[tuple]:
    """Send ``note`` to each built channel; return (channel_name, ChannelResult)."""
    results: List[tuple] = []
    for channel in channels:
        try:
            results.append((channel.name, channel.send(note)))
        except Exception as exc:  # noqa: BLE001 - one bad channel shouldn't kill the rest
            results.append((channel.name, ChannelResult(ok=False, detail=f"unhandled: {exc}")))
    return results
