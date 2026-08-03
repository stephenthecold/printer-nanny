"""Microsoft Teams channel (stub).

Posts an Adaptive-Card-ish message to an incoming-webhook URL. Implemented enough
to be wired up, but treated as a stub until the Teams workflow is finalized.
"""

from __future__ import annotations

import html

import httpx

from central.channels.base import ChannelResult, Notification, NotificationChannel
from central.config import settings

# Markdown characters that can forge a *link* -- the only Markdown constructs
# that redirect a reader rather than merely restyle text. Both `[text](url)`
# and `![alt](url)` need a literal "[", so escaping the brackets is enough and
# leaves ordinary alert text ("Magenta at 4% (threshold 10%)") untouched. The
# backslash MUST come first: escaping "[" in `\[x](u)` without escaping the
# backslash first yields `\\[x](u)`, which renders a backslash followed by a
# *live* link.
_MD_LINK_CHARS = ("\\", "[", "]")


def _esc(value: object) -> str:
    """Escape a value for a Teams connector card's text field.

    Teams renders connector-card text as **both** limited HTML and Markdown --
    "Connector cards support limited Markdown and HTML formatting", with
    ``<a href>`` and ``<img src>`` in the supported-tag table -- so a value
    interpolated here is live markup twice over. The values are SNMP
    brand/model/hostname strings off devices on customer LANs plus operator and
    end-user free text, so both halves have to be closed:

    1. HTML entity encoding kills the ``<a>``/``<img>`` half. Unlike Slack,
       encoding here is *correct* rather than merely safe -- the card really
       does decode entities, so ``&amp;`` renders as ``&``.
    2. Backslash-escaping ``\\ [ ]`` kills the Markdown half. Microsoft's own
       guidance is to do this ("since all fields are processed as Markdown, be
       sure to escape Markdown special characters").

    Emphasis, headings and blockquotes are left alone: they can restyle text
    but never point it somewhere, and backslashing every ``*`` and ``#`` would
    disfigure ordinary alert text for no security gain.
    """
    escaped = html.escape(str(value), quote=True)
    for char in _MD_LINK_CHARS:
        escaped = escaped.replace(char, "\\" + char)
    return escaped


class TeamsChannel(NotificationChannel):
    type = "teams"

    def _webhook(self) -> str:
        return self.config.get("webhook_url") or settings.teams_webhook_url

    def build_payload(self, note: Notification) -> dict:
        # The "**", "[…]" and "_…_" here are ours, added around escaped values
        # rather than to them -- escaping the assembled string would render the
        # card's own formatting as literal punctuation.
        text = f"**[{_esc(note.severity.upper())}] {_esc(note.title)}**\n\n{_esc(note.body)}"
        if note.printer_label:
            text += f"\n\n_Printer:_ {_esc(note.printer_label)}"
        return {"text": text}

    def send(self, note: Notification) -> ChannelResult:
        webhook = self._webhook()
        payload = self.build_payload(note)
        if not webhook:
            return ChannelResult(
                ok=True, detail="teams dry-run (no webhook configured)", sent=False
            )
        try:
            resp = httpx.post(webhook, json=payload, timeout=15)
        except httpx.HTTPError as exc:
            return ChannelResult(ok=False, detail=f"teams request error: {exc}")
        if resp.status_code in (200, 202):
            return ChannelResult(ok=True, detail="posted to teams")
        return ChannelResult(ok=False, detail=f"teams {resp.status_code}: {resp.text[:200]}")
