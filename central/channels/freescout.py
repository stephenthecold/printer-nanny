"""FreeScout ticketing channel.

Creates a conversation via the FreeScout REST API (the "API & Webhooks" module):
    POST {base}/api/conversations
    headers: X-FreeScout-API-Key, Content-Type/Accept: application/json
Docs: https://api-docs.freescout.net/
"""

from __future__ import annotations

import httpx

from central.channels.base import ChannelResult, Notification, NotificationChannel

# FreeScout severity → conversation status hint. New tickets stay "active".
_DEFAULT_STATUS = "active"


class FreeScoutChannel(NotificationChannel):
    type = "freescout"

    def _base_url(self) -> str:
        return str(self.setting("freescout.base_url") or "").rstrip("/")

    def _api_key(self) -> str:
        return self.setting("freescout.api_key") or ""

    def _mailbox_id(self) -> int:
        return int(self.setting("freescout.mailbox_id") or 1)

    def build_payload(self, note: Notification) -> dict:
        """Build the POST /api/conversations body. Pure function — unit tested."""
        customer_email = self.config.get("customer_email") or "alerts@printer-nanny.local"
        app_name = self.setting("app.name", "Printer Nanny") or "Printer Nanny"
        customer_name = note.client_name or app_name
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

    def close_ticket(self, external_ref: str, note: str) -> ChannelResult:
        """Post a 'resolved' note to the conversation and close it (closed loop).

        Adds a ``note`` thread with ``status: "closed"`` via
        ``POST /api/conversations/{id}/threads``. A FreeScout-side already-closed
        ticket (FreeScout answers 412 Precondition Failed when the status is
        already the requested one) is treated as success -- the loop's intent
        (ticket is closed) is satisfied either way.
        """
        external_ref = str(external_ref or "").strip()
        if not external_ref:
            return ChannelResult(ok=False, detail="freescout close: no external_ref")
        base = self._base_url()
        api_key = self._api_key()
        if not base or not api_key:
            return ChannelResult(
                ok=True, detail="freescout dry-run close (no base_url/api_key configured)"
            )
        payload = {"type": "note", "text": note, "status": "closed"}
        try:
            resp = httpx.post(
                f"{base}/api/conversations/{external_ref}/threads",
                json=payload,
                headers={
                    "X-FreeScout-API-Key": api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=20,
            )
        except httpx.HTTPError as exc:
            return ChannelResult(ok=False, detail=f"freescout close request error: {exc}")

        if resp.status_code in (200, 201):
            return ChannelResult(ok=True, detail="ticket closed", external_ref=external_ref)
        # 404: ticket vanished; 412: status already closed. Either way the loop
        # is satisfied (the ticket is not an open thing we still need to chase).
        if resp.status_code in (404, 412):
            return ChannelResult(
                ok=True,
                detail=f"freescout close no-op ({resp.status_code}: already closed/gone)",
                external_ref=external_ref,
            )
        return ChannelResult(
            ok=False, detail=f"freescout close {resp.status_code}: {resp.text[:200]}"
        )
