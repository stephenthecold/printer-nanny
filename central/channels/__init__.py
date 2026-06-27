"""Pluggable notification channels (email, FreeScout, Teams, webhook, Slack)."""

from __future__ import annotations

from central.channels.base import ChannelResult, NotificationChannel, Notification
from central.channels.registry import (
    RoutableChannel,
    active_channels,
    build_channel,
    dispatch,
    routable_channels,
    route_channels,
)

__all__ = [
    "ChannelResult",
    "Notification",
    "NotificationChannel",
    "RoutableChannel",
    "active_channels",
    "build_channel",
    "dispatch",
    "routable_channels",
    "route_channels",
]
