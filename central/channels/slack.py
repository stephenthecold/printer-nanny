"""Slack channel (Incoming Webhook).

Posts to a Slack ``Incoming Webhook`` URL using the legacy attachments shape
which Slack still accepts on incoming webhooks and which colors the message
bar by severity. The newer Block Kit shape doesn't give us the color stripe.

To set up: in Slack, add the "Incoming Webhooks" app, pick the channel, copy
the webhook URL, paste it into Settings -> Slack -> Webhook URL.

Severity colors (matches Slack attachment color tokens):
- info -> ``good`` (green)
- warning -> ``warning`` (yellow)
- critical -> ``danger`` (red)

A no-URL configuration is treated as a dry-run (returns ok=True with detail)
so an MSP can enable the channel for testing without immediately wiring Slack.
"""

from __future__ import annotations

import httpx

from central.channels.base import ChannelResult, Notification, NotificationChannel

_SEVERITY_COLOR = {
    "info": "good",
    "warning": "warning",
    "critical": "danger",
}
_SEVERITY_EMOJI = {
    "info": ":information_source:",
    "warning": ":warning:",
    "critical": ":rotating_light:",
}


class SlackChannel(NotificationChannel):
    type = "slack"

    def _webhook(self) -> str:
        return str(self.config.get("webhook_url") or self.setting("slack.webhook_url") or "")

    def min_severity(self) -> str:
        return str(self.setting("slack.min_severity", "info") or "info").lower()

    def _min_severity(self) -> str:  # backward-compatible alias
        return self.min_severity()

    def _meets_threshold(self, severity: str) -> bool:
        return self.meets_severity(severity)

    def build_payload(self, note: Notification) -> dict:
        emoji = _SEVERITY_EMOJI.get(note.severity, "")
        color = _SEVERITY_COLOR.get(note.severity, "good")
        fields = []
        if note.client_name:
            fields.append({"title": "Client", "value": note.client_name, "short": True})
        if note.site_name:
            fields.append({"title": "Site", "value": note.site_name, "short": True})
        if note.printer_label:
            fields.append({"title": "Printer", "value": note.printer_label, "short": False})
        return {
            "text": f"{emoji} *{note.title}*".strip(),
            "attachments": [
                {
                    "color": color,
                    "text": note.body,
                    "fields": fields,
                    "footer": self.setting("app.name", "Printer Nanny") or "Printer Nanny",
                    "mrkdwn_in": ["text"],
                }
            ],
        }

    def send(self, note: Notification) -> ChannelResult:
        webhook = self._webhook()
        if not webhook:
            return ChannelResult(ok=True, detail="slack dry-run (no webhook configured)")
        if not self._meets_threshold(note.severity):
            return ChannelResult(
                ok=True,
                detail=f"slack skipped: severity {note.severity} below {self._min_severity()}",
            )
        try:
            resp = httpx.post(webhook, json=self.build_payload(note), timeout=15)
        except httpx.HTTPError as exc:
            return ChannelResult(ok=False, detail=f"slack request error: {exc}")
        # Slack returns 200 with body "ok" on success.
        if resp.status_code == 200:
            return ChannelResult(ok=True, detail="posted to slack")
        return ChannelResult(
            ok=False, detail=f"slack {resp.status_code}: {resp.text[:200]}"
        )
