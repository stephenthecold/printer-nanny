"""Generic webhook channel.

Sends each notification as a JSON POST to an operator-configured URL. Built so
MSPs can wire Printer Nanny into anything that accepts a webhook -- PSA tools
(ConnectWise, HaloPSA, Autotask), incident routers (PagerDuty events API v2,
Opsgenie heartbeats), Zapier/Make scenarios, internal automation, or
custom-built endpoints.

Knobs (from runtime settings):
- ``webhook.url`` -- destination (skipped if blank, so the channel can stay
  enabled in settings without firing).
- ``webhook.auth_header`` + ``webhook.auth_token`` -- optional header pair.
  When ``auth_token`` is set, the channel sends ``<auth_header>: <token>``.
  ``auth_header`` defaults to ``Authorization``; set the token to e.g.
  ``Bearer abc123`` to pass a bearer creds, or change the header name for
  X-Api-Key style schemes.
- ``webhook.min_severity`` -- "info" (default) | "warning" | "critical". The
  channel returns a no-op success for severities below the threshold so a
  PagerDuty webhook can be wired up and only paged on critical.

Payload is a stable JSON shape so subscribers can rely on field names::

    {
      "source": "printer-nanny",
      "app": "<app.name>",
      "title": "...",
      "body": "...",
      "severity": "warning",
      "client": "Acme",
      "site": "HQ",
      "printer": "HP M404 @ 10.0.0.5",
      "alert_id": 123,
      "timestamp": "2026-06-09T15:30:00Z"
    }
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from central.channels.base import ChannelResult, Notification, NotificationChannel


class WebhookChannel(NotificationChannel):
    type = "webhook"

    def _url(self) -> str:
        return str(self.config.get("url") or self.setting("webhook.url") or "")

    def min_severity(self) -> str:
        return str(self.setting("webhook.min_severity", "info") or "info").lower()

    def _min_severity(self) -> str:  # backward-compatible alias
        return self.min_severity()

    def _auth_pair(self) -> tuple[str, str]:
        header = str(self.setting("webhook.auth_header", "Authorization") or "Authorization")
        token = str(self.setting("webhook.auth_token", "") or "")
        return header, token

    def _meets_threshold(self, severity: str) -> bool:
        return self.meets_severity(severity)

    def build_payload(self, note: Notification) -> dict:
        return {
            "source": "printer-nanny",
            "app": self.setting("app.name", "Printer Nanny") or "Printer Nanny",
            "title": note.title,
            "body": note.body,
            "severity": note.severity,
            "client": note.client_name,
            "site": note.site_name,
            "printer": note.printer_label,
            "alert_id": note.alert_id,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def send(self, note: Notification) -> ChannelResult:
        url = self._url()
        if not url:
            return ChannelResult(ok=True, detail="webhook dry-run (no url configured)")
        if not self._meets_threshold(note.severity):
            return ChannelResult(
                ok=True,
                detail=f"webhook skipped: severity {note.severity} below {self._min_severity()}",
            )
        headers = {"Content-Type": "application/json"}
        auth_header, auth_token = self._auth_pair()
        if auth_token:
            headers[auth_header] = auth_token
        try:
            resp = httpx.post(url, json=self.build_payload(note), headers=headers, timeout=15)
        except httpx.HTTPError as exc:
            return ChannelResult(ok=False, detail=f"webhook request error: {exc}")
        if 200 <= resp.status_code < 300:
            return ChannelResult(
                ok=True, detail=f"posted to webhook ({resp.status_code})"
            )
        return ChannelResult(
            ok=False, detail=f"webhook {resp.status_code}: {resp.text[:200]}"
        )
