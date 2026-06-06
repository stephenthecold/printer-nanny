"""Pluggable notification channels (email, FreeScout, Teams)."""

from __future__ import annotations

from central.channels.base import ChannelResult, NotificationChannel, Notification
from central.channels.registry import active_channels, build_channel, dispatch

__all__ = [
    "ChannelResult",
    "Notification",
    "NotificationChannel",
    "active_channels",
    "build_channel",
    "dispatch",
]
