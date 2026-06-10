"""SMTP email notification channel.

Supports two auth modes selected by ``smtp.auth_type``:

- ``basic`` (default): classic SMTP AUTH LOGIN with ``smtp.user`` / ``smtp.password``.
- ``oauth_google`` / ``oauth_microsoft``: XOAUTH2 SASL with a Bearer access token
  refreshed on demand. See ``central.auth_oauth_smtp`` for the consent flow that
  populates ``smtp.oauth_refresh_token``.

The OAuth refresh path takes a ``db`` session at send time via the optional
``db_factory`` hook so token refreshes persist to ``app_settings``. When the
channel is constructed without a db_factory (the seed/test path), OAuth send
falls back to the cached access token only.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Callable, Optional

from sqlalchemy.orm import Session

from central.channels.base import ChannelResult, Notification, NotificationChannel


class EmailChannel(NotificationChannel):
    type = "email"

    def __init__(
        self,
        name: str,
        config: Optional[dict] = None,
        runtime: Optional[dict] = None,
        db_factory: Optional[Callable[[], Session]] = None,
    ):
        super().__init__(name, config, runtime)
        # OAuth needs a DB session to persist refreshed access tokens. Callers
        # that don't supply one (tests, seed scripts) still work — the channel
        # uses the cached token from `runtime` and skips persistence.
        self._db_factory = db_factory

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
        # File attachments (scheduled reports' CSVs). (filename, mime, bytes).
        for filename, content_type, payload in note.attachments or []:
            maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
            msg.add_attachment(
                payload, maintype=maintype, subtype=subtype or "octet-stream",
                filename=filename,
            )
        return msg

    def _auth_type(self) -> str:
        return str(self.setting("smtp.auth_type") or "basic")

    def _oauth_access_token(self) -> Optional[str]:
        """Return a fresh access token, refreshing via the DB if available."""
        if self._db_factory is None:
            cached = self.setting("smtp.oauth_access_token", "")
            return str(cached) if cached else None
        from central.auth_oauth_smtp import refresh_access_token

        db = self._db_factory()
        try:
            return refresh_access_token(db, self.runtime)
        finally:
            db.close()

    def _do_oauth_auth(self, smtp: smtplib.SMTP, email: str, token: str) -> None:
        """Send the SASL XOAUTH2 string. Provider returns 235 on success."""
        from central.auth_oauth_smtp import build_xoauth2

        challenge = build_xoauth2(email, token)
        code, response = smtp.docmd("AUTH", "XOAUTH2 " + challenge.decode("ascii"))
        if code != 235:
            raise smtplib.SMTPAuthenticationError(code, response)

    def send(self, note: Notification) -> ChannelResult:
        recipients = self._recipients()
        if not recipients:
            return ChannelResult(ok=False, detail="no recipients configured")
        msg = self.build_message(note)
        host = self.setting("smtp.host")
        port = int(self.setting("smtp.port", 25))
        user = self.setting("smtp.user")
        auth_type = self._auth_type()
        try:
            with smtplib.SMTP(host, port, timeout=15) as smtp:
                if self.setting("smtp.use_tls"):
                    smtp.starttls()
                if auth_type in ("oauth_google", "oauth_microsoft"):
                    if not user:
                        return ChannelResult(ok=False, detail="OAuth needs smtp.user (mailbox)")
                    token = self._oauth_access_token()
                    if not token:
                        return ChannelResult(
                            ok=False, detail="no OAuth access token — run the Connect flow"
                        )
                    self._do_oauth_auth(smtp, str(user), token)
                elif user:
                    smtp.login(user, self.setting("smtp.password", ""))
                smtp.send_message(msg)
            return ChannelResult(ok=True, detail=f"emailed {len(recipients)} recipient(s)")
        except smtplib.SMTPAuthenticationError as exc:
            return ChannelResult(ok=False, detail=f"smtp auth failed: {exc}")
        except OSError as exc:
            return ChannelResult(ok=False, detail=f"smtp error: {exc}")
