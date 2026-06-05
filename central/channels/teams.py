"""Microsoft Teams channel (stub).

Posts an Adaptive-Card-ish message to an incoming-webhook URL. Implemented enough
to be wired up, but treated as a stub until the Teams workflow is finalized.
"""

from __future__ import annotations

import httpx

from central.channels.base import ChannelResult, Notification, NotificationChannel
from central.config import settings


class TeamsChannel(NotificationChannel):
    type = "teams"

    def _webhook(self) -> str:
        return self.config.get("webhook_url") or settings.teams_webhook_url

    def build_payload(self, note: Notification) -> dict:
        text = f"**[{note.severity.upper()}] {note.title}**\n\n{note.body}"
        if note.printer_label:
            text += f"\n\n_Printer:_ {note.printer_label}"
        return {"text": text}

    def send(self, note: Notification) -> ChannelResult:
        webhook = self._webhook()
        payload = self.build_payload(note)
        if not webhook:
            return ChannelResult(ok=True, detail="teams dry-run (no webhook configured)")
        try:
            resp = httpx.post(webhook, json=payload, timeout=15)
        except httpx.HTTPError as exc:
            return ChannelResult(ok=False, detail=f"teams request error: {exc}")
        if resp.status_code in (200, 202):
            return ChannelResult(ok=True, detail="posted to teams")
        return ChannelResult(ok=False, detail=f"teams {resp.status_code}: {resp.text[:200]}")
