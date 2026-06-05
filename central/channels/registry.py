"""Build channel implementations from DB rows and dispatch notifications."""

from __future__ import annotations

from typing import Iterable, Optional

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


def dispatch(
    note: Notification,
    channel_rows: Iterable[m.NotificationChannel],
    runtime: Optional[dict] = None,
) -> list[tuple[str, ChannelResult]]:
    """Send ``note`` to every enabled channel row; return (channel_name, result)."""
    results: list[tuple[str, ChannelResult]] = []
    for row in channel_rows:
        if not row.enabled:
            continue
        channel = build_channel(row, runtime)
        try:
            results.append((row.name, channel.send(note)))
        except Exception as exc:  # noqa: BLE001 - one bad channel shouldn't kill the rest
            results.append((row.name, ChannelResult(ok=False, detail=f"unhandled: {exc}")))
    return results
