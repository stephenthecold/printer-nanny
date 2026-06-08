"""SMTP email notification channel."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from central.channels.base import ChannelResult, Notification, NotificationChannel


class EmailChannel(NotificationChannel):
    type = "email"

    def _recipients(self) -> list[str]:
        to = self.config.get("to")
        if isinstance(to, str):
            return [addr.strip() for addr in to.split(",") if addr.strip()]
        if isinstance(to, list):
            return to
        return []

    def build_message(self, note: Notification) -> EmailMessage:
        msg = EmailMessage()
        app_name = self.setting("app.name", "Printer Nanny") or "Printer Nanny"
        msg["Subject"] = f"[{app_name}][{note.severity.upper()}] {note.title}"
        msg["From"] = self.config.get("from") or self.setting("smtp.from")
        msg["To"] = ", ".join(self._recipients())
        lines = [note.body, ""]
        if note.printer_label:
            lines.append(f"Printer: {note.printer_label}")
        if note.site_name:
            lines.append(f"Site: {note.site_name}")
        if note.client_name:
            lines.append(f"Client: {note.client_name}")
        msg.set_content("\n".join(lines))
        return msg

    def send(self, note: Notification) -> ChannelResult:
        recipients = self._recipients()
        if not recipients:
            return ChannelResult(ok=False, detail="no recipients configured")
        msg = self.build_message(note)
        host = self.setting("smtp.host")
        port = int(self.setting("smtp.port", 25))
        user = self.setting("smtp.user")
        try:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                if self.setting("smtp.use_tls"):
                    smtp.starttls()
                if user:
                    smtp.login(user, self.setting("smtp.password", ""))
                smtp.send_message(msg)
            return ChannelResult(ok=True, detail=f"emailed {len(recipients)} recipient(s)")
        except OSError as exc:
            return ChannelResult(ok=False, detail=f"smtp error: {exc}")
