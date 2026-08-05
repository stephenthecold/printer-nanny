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


def _esc(value: object) -> str:
    """Escape a value for Slack's message parser.

    Slack parses ``<…>`` as a control sequence, not as text: ``<https://evil|
    https://help.acme.example>`` renders as a link whose *visible* text is the
    attacker's choice, and ``<!channel>`` / ``<!here>`` broadcast to everyone in
    the channel. Values reaching here are SNMP brand/model/hostname strings off
    devices on customer LANs plus operator and end-user free text, so a printer
    can name itself ``<!channel>`` and page an MSP's ops room, or forge a
    plausible link in a message the team trusts because the monitoring bot sent
    it.

    Slack documents exactly three replacements for this and warns against doing
    more: "You shouldn't encode the entire piece of text, as only the specific
    characters shown above will be decoded for display in Slack." So this is
    deliberately **not** ``html.escape`` -- that also rewrites quotes, which
    Slack does not decode, leaving a literal ``&#x27;`` in every printer named
    after somebody's office. ``&`` must be replaced first or the ampersands
    introduced by the other two get double-encoded.

    What this does not cover is ``mrkdwn`` emphasis (``*bold*``, ``_italic_``):
    Slack offers no escape for it and it can only restyle text, never redirect
    it.
    """
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
        # Field values are escaped too, not just the top-level text: Slack
        # parses control sequences in attachment text and field values alike,
        # so an unescaped field is the same hole one layer down.
        if note.client_name:
            fields.append({"title": "Client", "value": _esc(note.client_name), "short": True})
        if note.site_name:
            fields.append({"title": "Site", "value": _esc(note.site_name), "short": True})
        if note.printer_label:
            fields.append({"title": "Printer", "value": _esc(note.printer_label), "short": False})
        app_name = self.setting("app.name", "Printer Nanny") or "Printer Nanny"
        return {
            "text": f"{emoji} *{_esc(note.title)}*".strip(),
            "attachments": [
                {
                    "color": color,
                    "text": _esc(note.body),
                    "fields": fields,
                    "footer": _esc(app_name),
                    "mrkdwn_in": ["text"],
                }
            ],
        }

    def send(self, note: Notification) -> ChannelResult:
        webhook = self._webhook()
        if not webhook:
            return ChannelResult(
                ok=True, detail="slack dry-run (no webhook configured)", sent=False
            )
        if not self._meets_threshold(note.severity):
            return ChannelResult(
                ok=True,
                detail=f"slack skipped: severity {note.severity} below {self._min_severity()}",
                sent=False,
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
