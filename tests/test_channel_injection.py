"""Hostile device/user strings must not become markup in any channel.

Every value a channel interpolates is untrusted: ``printer_label`` is assembled
from SNMP brand/model/hostname strings read off devices on customer LANs,
``body`` carries device status text, and the customer portal's "Report a
problem" form feeds title and body from end-user free text.

Each channel composes a *different* document, so each has its own escaping
contract and each gets its own assertions here rather than one shared rule:

- FreeScout thread bodies are HTML (rendered raw via ``safe_raw_html`` in
  ``conversations/partials/thread.blade.php``) -- entity-encode.
- Slack parses ``&``, ``<``, ``>`` as control characters -- replace exactly
  those three, and no more (Slack decodes only those).
- Teams connector cards render limited HTML *and* Markdown -- entity-encode
  and neutralise Markdown's link syntax.
- The generic webhook composes nothing -- values must arrive verbatim.
- Email is ``text/plain`` -- values must arrive verbatim, and a multi-line SNMP
  model must not blow up the RFC 5322 Subject header.
"""

from __future__ import annotations

import re

from central.channels.base import Notification
from central.channels.email import EmailChannel
from central.channels.freescout import FreeScoutChannel
from central.channels.slack import SlackChannel
from central.channels.teams import TeamsChannel
from central.channels.webhook import WebhookChannel

# A printer that names itself this is not hypothetical: the model string is
# copied verbatim from SNMP into ``printers.model`` and straight on into the
# alert title and printer label.
XSS = '<img src=x onerror=alert(1)>MFC-L8900'


def _note(**over) -> Notification:
    base = dict(
        title="Low toner",
        body="Level 4%",
        severity="warning",
        printer_label="HP M404 @ 10.0.0.5",
        site_name="HQ",
        client_name="Acme",
    )
    base.update(over)
    return Notification(**base)


# --------------------------------------------------------------------------- #
# FreeScout -- thread body is HTML
# --------------------------------------------------------------------------- #
def _fs(note: Notification) -> str:
    return FreeScoutChannel(name="fs", config={}, runtime={}).build_payload(note)[
        "threads"
    ][0]["text"]


def test_freescout_escapes_printer_label():
    text = _fs(_note(printer_label=XSS))
    assert "<img" not in text
    assert "onerror" not in text.replace("&lt;img src=x onerror=alert(1)&gt;", "")
    assert "&lt;img src=x onerror=alert(1)&gt;MFC-L8900" in text


def test_freescout_escapes_body_site_and_client():
    """Every interpolated value, not just the one that was reported."""
    text = _fs(
        _note(
            body="<script>alert(1)</script>",
            site_name="<b>HQ</b>",
            client_name="<i>Acme</i>",
        )
    )
    for raw in ("<script>", "</script>", "<b>", "<i>"):
        assert raw not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "&lt;b&gt;HQ&lt;/b&gt;" in text


def test_freescout_keeps_its_own_br_structure():
    """The <br> separators are the channel's own markup and must survive.

    Escaping the assembled string instead of each value would render them as
    literal "&lt;br&gt;" and flatten the ticket into one line.
    """
    text = _fs(_note())
    assert text == "Level 4%<br>Printer: HP M404 @ 10.0.0.5<br>Site: HQ<br>Client: Acme"
    assert "&lt;br&gt;" not in text


def test_freescout_device_cannot_forge_a_body_line():
    """A device must not be able to fake the structured trailer.

    ``<br>Client: Someone Else`` in a model string would otherwise render as a
    genuine-looking line in the ticket.
    """
    text = _fs(_note(printer_label="p<br>Client: Someone Else"))
    assert text.count("<br>") == 3  # Printer, Site, Client -- ours only
    assert "&lt;br&gt;Client: Someone Else" in text


def test_freescout_close_note_is_escaped():
    """close_ticket's text is a thread body too, and carries alert.title."""
    sent = {}

    class _Resp:
        status_code = 200
        headers: dict = {}
        text = ""

        def json(self):
            return {}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent["payload"] = json
        return _Resp()

    import httpx

    orig = httpx.post
    httpx.post = fake_post
    try:
        ch = FreeScoutChannel(
            name="fs",
            config={},
            runtime={"freescout.base_url": "https://h.example", "freescout.api_key": "k"},
        )
        ch.close_ticket("42", f"Auto-resolved by Printer Nanny: Low toner on {XSS}")
    finally:
        httpx.post = orig

    assert "<img" not in sent["payload"]["text"]
    assert "&lt;img src=x onerror=alert(1)&gt;" in sent["payload"]["text"]


def test_freescout_subject_is_not_double_encoded():
    """FreeScout escapes the subject itself (Blade ``{{ }}``), so we must not.

    Encoding here would show "Bob&#x27;s &amp; Co" in the conversation list and
    in the ticket email's Subject line.
    """
    payload = FreeScoutChannel(name="fs", config={}, runtime={}).build_payload(
        _note(title="Low toner on Bob's & Co printer")
    )
    assert payload["subject"] == "[WARNING] Low toner on Bob's & Co printer"


# --------------------------------------------------------------------------- #
# Slack -- & < > are control characters
# --------------------------------------------------------------------------- #
def _slack(note: Notification) -> dict:
    return SlackChannel(name="s", config={}, runtime={}).build_payload(note)


def test_slack_printer_cannot_broadcast_to_the_channel():
    """``<!channel>`` in a model string would page the whole ops room."""
    payload = _slack(_note(printer_label="<!channel> toner is fine"))
    value = payload["attachments"][0]["fields"][2]["value"]
    assert "<!channel>" not in value
    assert value == "&lt;!channel&gt; toner is fine"


def test_slack_body_cannot_forge_a_link():
    """``<url|text>`` renders as a link whose visible text the device picks."""
    payload = _slack(_note(body="<https://evil.example|https://help.acme.example>"))
    text = payload["attachments"][0]["text"]
    assert "<https://evil.example|" not in text
    assert text == "&lt;https://evil.example|https://help.acme.example&gt;"


def test_slack_escapes_title_and_every_field():
    payload = _slack(
        _note(title="<!here> down", client_name="A & B", site_name="<https://e|HQ>")
    )
    assert payload["text"] == ":warning: *&lt;!here&gt; down*"
    assert payload["attachments"][0]["fields"][0]["value"] == "A &amp; B"
    assert payload["attachments"][0]["fields"][1]["value"] == "&lt;https://e|HQ&gt;"


def test_slack_escapes_only_the_three_documented_characters():
    """Slack decodes only ``&amp;``/``&lt;``/``&gt;``.

    Using ``html.escape`` here would emit ``&#x27;`` and ``&quot;``, which Slack
    passes through literally -- so every printer named after somebody's office
    would read "Bob&#x27;s".
    """
    payload = _slack(_note(printer_label='Bob\'s "big" printer'))
    assert payload["attachments"][0]["fields"][2]["value"] == 'Bob\'s "big" printer'


def test_slack_ampersand_is_not_double_encoded():
    """``&`` must be replaced first, or ``<`` -> ``&lt;`` becomes ``&amp;lt;``."""
    payload = _slack(_note(printer_label="<x>"))
    assert payload["attachments"][0]["fields"][2]["value"] == "&lt;x&gt;"


# --------------------------------------------------------------------------- #
# Teams -- connector cards render limited HTML *and* Markdown
# --------------------------------------------------------------------------- #
def _teams(note: Notification) -> str:
    return TeamsChannel(name="t", config={}, runtime={}).build_payload(note)["text"]


def test_teams_escapes_html():
    """``<a href>`` and ``<img src>`` are in Teams' supported-tag table."""
    text = _teams(_note(printer_label='<a href="https://evil.example">Reset password</a>'))
    assert "<a href" not in text
    assert "&lt;a href=&quot;https://evil.example&quot;&gt;" in text


def test_teams_escapes_markdown_link_syntax():
    """HTML escaping alone leaves ``[text](url)`` live -- Teams renders it."""
    text = _teams(_note(body="[Reset your password](https://evil.example)"))
    assert "[Reset your password](" not in text
    assert "\\[Reset your password\\](https://evil.example)" in text


def test_teams_escapes_markdown_image_syntax():
    text = _teams(_note(printer_label="![beacon](https://evil.example/px.gif)"))
    assert "![beacon](" not in text
    assert "!\\[beacon\\]" in text


def test_teams_backslash_is_escaped_before_brackets():
    """Otherwise ``\\[x](u)`` becomes a rendered backslash plus a *live* link."""
    text = _teams(_note(printer_label="\\[x](https://evil.example)"))
    assert "\\\\\\[x\\]" in text
    # No live "](" sequence survives: every bracket is backslash-escaped.
    assert not re.search(r"(?<!\\)\]\(", text)


def test_teams_keeps_its_own_formatting():
    """The card's own ``**``/``[]``/``_`` structure must not be escaped."""
    text = _teams(_note())
    assert text.startswith("**[WARNING] Low toner**")
    assert "\n\n_Printer:_ HP M404 @ 10.0.0.5" in text


# --------------------------------------------------------------------------- #
# Generic webhook -- composes nothing, so values must arrive verbatim
# --------------------------------------------------------------------------- #
def test_webhook_passes_values_through_unmodified():
    """This is a "do not escape" regression test, not an oversight.

    The payload is discrete JSON values that ``json.dumps`` encodes completely;
    subscribers parse it as data. Escaping here would corrupt the value for
    every subscriber that stores or matches on it, and would still be the wrong
    encoding for one that renders HTML.
    """
    payload = WebhookChannel(name="w", config={}, runtime={}).build_payload(
        _note(printer_label=XSS, body="a & b < c", title="Bob's printer")
    )
    assert payload["printer"] == XSS
    assert payload["body"] == "a & b < c"
    assert payload["title"] == "Bob's printer"


# --------------------------------------------------------------------------- #
# Email -- text/plain, and a header that must survive an SNMP string
# --------------------------------------------------------------------------- #
def _email(note: Notification):
    return EmailChannel(
        "ops", {"to": "a@x.com", "from": "noc@x.com"}, runtime={}
    ).build_message(note)


def test_email_body_is_plain_text_and_carries_values_verbatim():
    msg = _email(_note(printer_label=XSS))
    assert msg.get_content_type() == "text/plain"
    assert not msg.is_multipart()
    assert XSS in msg.get_content()


def test_email_subject_survives_a_multiline_snmp_model():
    """A multi-line ``sysDescr`` is ordinary, and used to kill the alert.

    ``printers.model`` is copied verbatim from SNMP into the alert title and on
    into the Subject header, where ``email.policy.default`` refuses CR/LF. The
    alert then failed on every evaluation with a stack-trace fragment instead
    of mailing.
    """
    msg = _email(_note(title="Low toner on HP LaserJet\nMFP M479\r\nfw 1.2 @ 10.0.0.5"))
    subject = msg["Subject"]
    assert "\n" not in subject and "\r" not in subject
    assert subject == (
        "[Printer Nanny][WARNING] Low toner on HP LaserJet MFP M479 fw 1.2 @ 10.0.0.5"
    )


def test_email_subject_cannot_smuggle_a_second_header():
    msg = _email(_note(title="Low toner\nBcc: attacker@evil.example"))
    assert msg["Bcc"] is None
    assert b"\nBcc:" not in msg.as_bytes()
    assert msg["Subject"] == "[Printer Nanny][WARNING] Low toner Bcc: attacker@evil.example"
