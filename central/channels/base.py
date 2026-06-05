"""Notification channel interface and the payload passed to every channel."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional


@dataclass
class Notification:
    """Channel-agnostic description of an alert to deliver."""

    title: str
    body: str
    severity: str               # "info" | "warning" | "critical"
    client_name: Optional[str] = None
    site_name: Optional[str] = None
    printer_label: Optional[str] = None  # e.g. "HP M404 @ 10.10.0.12"
    alert_id: Optional[int] = None


@dataclass
class ChannelResult:
    ok: bool
    detail: str
    # External reference if the channel created something (e.g. FreeScout convo id).
    external_ref: Optional[str] = None


class NotificationChannel(abc.ABC):
    """A delivery target. Implementations are built from a NotificationChannel row."""

    type: str = "base"

    def __init__(self, name: str, config: Optional[dict] = None):
        self.name = name
        self.config = config or {}

    @abc.abstractmethod
    def send(self, note: Notification) -> ChannelResult:  # pragma: no cover - interface
        ...
