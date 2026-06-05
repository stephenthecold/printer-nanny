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
    """A delivery target. Implementations are built from a NotificationChannel row.

    ``runtime`` is the operator settings map (see central.runtime). When omitted,
    channels fall back to env-derived defaults so direct construction still works.
    """

    type: str = "base"

    def __init__(
        self, name: str, config: Optional[dict] = None, runtime: Optional[dict] = None
    ):
        self.name = name
        self.config = config or {}
        if runtime is None:
            from central.runtime import default_settings

            runtime = default_settings()
        self.runtime = runtime

    def setting(self, key: str, default=None):
        """Channel-row config overrides the global runtime setting."""
        return self.config.get(key.split(".")[-1], self.runtime.get(key, default))

    @abc.abstractmethod
    def send(self, note: Notification) -> ChannelResult:  # pragma: no cover - interface
        ...
