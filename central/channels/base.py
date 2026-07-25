"""Notification channel interface and the payload passed to every channel."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

# Shared severity ordering so a per-channel minimum can gate any notification.
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


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
    # Optional file attachments: (filename, content_type, payload bytes).
    # Channels that can't carry files (Slack webhook, Teams, generic webhook)
    # simply ignore them; the email channel attaches them. Used by scheduled
    # reports (monthly billing CSV).
    attachments: Optional[list] = None


@dataclass
class ChannelResult:
    """Outcome of one channel send.

    ``ok`` answers "did anything go wrong", ``sent`` answers "did a message
    actually leave the building". They are deliberately separate: a severity
    skip and an unconfigured dry-run are both ``ok=True`` (nothing failed,
    nothing to retry) but ``sent=False`` (nobody was told). Collapsing the two
    made the durable delivery log record a *delivered* row -- and the alerts
    page render a green tick -- for a notification that was never transmitted.
    Anything that reports success without transmitting must set ``sent=False``.

    ``sent`` is only meaningful when ``ok`` is True. A failure is already its own
    terminal/retryable state, so error paths leave ``sent`` at its default rather
    than each having to restate it -- do not read ``sent=True`` on an ``ok=False``
    result as a claim that anything was transmitted.
    """

    ok: bool
    detail: str
    # External reference if the channel created something (e.g. FreeScout convo id).
    external_ref: Optional[str] = None
    # False when the call succeeded but no message was transmitted (dry-run,
    # severity gate). Defaults True so a real send needs no extra ceremony.
    sent: bool = True


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

    def min_severity(self) -> str:
        """Lowest severity this channel will deliver.

        Defaults to ``info`` (deliver everything). Channels with their own
        ``<type>.min_severity`` knob (Slack, webhook) override this; a per-row
        ``min_severity`` in ``config`` takes precedence for any channel. The
        dispatcher consults this so the severity gate applies uniformly to
        every channel, not just the two that historically implemented it.
        """
        return str(self.config.get("min_severity", "info") or "info").lower()

    def meets_severity(self, severity: str) -> bool:
        return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK.get(self.min_severity(), 0)

    @abc.abstractmethod
    def send(self, note: Notification) -> ChannelResult:  # pragma: no cover - interface
        ...
