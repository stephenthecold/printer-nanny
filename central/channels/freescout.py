"""FreeScout ticketing channel.

Creates a conversation via the FreeScout REST API (the "API & Webhooks" module):
    POST {base}/api/conversations
    headers: X-FreeScout-API-Key, Content-Type/Accept: application/json
Docs: https://api-docs.freescout.net/
"""

from __future__ import annotations

import httpx

from central.channels.base import ChannelResult, Notification, NotificationChannel
from central.config import settings

# FreeScout severity → conversation status hint. New tickets stay "active".
_DEFAULT_STATUS = "active"


class FreeScoutChannel(NotificationChannel):
    type = "freescout"

    def _base_url(self) -> str:
        return (self.config.get("base_url") or settings.freescout_base_url or "").rstrip("/")

    def _api_key(self) -> str:
        return self.config.get("api_key") or settings.freescout_api_key

    def _mailbox_id(self) -> int:
        return int(self.config.get("mailbox_id") or settings.freescout_mailbox_id)

    def build_payload(self, note: Notification) -> dict:
        """Build the POST /api/conversations body. Pure function — unit tested."""
        customer_email = self.config.get("customer_email") or "alerts@printer-nanny.local"
        customer_name = note.client_name or "Printer Nanny"
        body_lines = [note.body]
        if note.printer_label:
            body_lines.append(f"<br>Printer: {note.printer_label}")
        if note.site_name:
            body_lines.append(f"<br>Site: {note.site_name}")
        if note.client_name:
            body_lines.append(f"<br>Client: {note.client_name}")
        return {
            "type": "email",
            "mailboxId": self._mailbox_id(),
            "subject": f"[{note.severity.upper()}] {note.title}",
            "status": _DEFAULT_STATUS,
            "customer": {"email": customer_email, "firstName": customer_name},
            "threads": [
                {
                    "type": "customer",
                    "customer": {"email": customer_email},
                    "text": "".join(body_lines),
                }
            ],
            "tags": ["printer-nanny", note.severity],
        }

    def send(self, note: Notification) -> ChannelResult:
        base = self._base_url()
        api_key = self._api_key()
        payload = self.build_payload(note)
        if not base or not api_key:
            # Dry-run so the system is demoable without a live FreeScout.
            return ChannelResult(
                ok=True, detail="freescout dry-run (no base_url/api_key configured)"
            )
        try:
            resp = httpx.post(
                f"{base}/api/conversations",
                json=payload,
                headers={
                    "X-FreeScout-API-Key": api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=20,
            )
        except httpx.HTTPError as exc:
            return ChannelResult(ok=False, detail=f"freescout request error: {exc}")

        if resp.status_code in (200, 201):
            convo_id = resp.headers.get("Resource-ID") or str(resp.json().get("id", ""))
            return ChannelResult(ok=True, detail="ticket created", external_ref=convo_id)
        return ChannelResult(
            ok=False, detail=f"freescout {resp.status_code}: {resp.text[:200]}"
        )
