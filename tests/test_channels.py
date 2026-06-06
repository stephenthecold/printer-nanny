"""Notification channel payload builders and dispatch (no network)."""

from __future__ import annotations

from central.channels import Notification, active_channels, dispatch
from central.channels.email import EmailChannel
from central.channels.freescout import FreeScoutChannel
from central.channels.teams import TeamsChannel
from central.runtime import default_settings

NOTE = Notification(
    title="Low magenta on HP M404 @ 10.0.0.5",
    body="Magenta at 4% (threshold 10%).",
    severity="warning",
    client_name="Acme",
    site_name="HQ",
    printer_label="HP M404 @ 10.0.0.5",
)


def test_email_message_builder():
    ch = EmailChannel("ops", {"to": "a@x.com, b@x.com", "from": "noc@x.com"})
    msg = ch.build_message(NOTE)
    assert msg["To"] == "a@x.com, b@x.com"
    assert msg["From"] == "noc@x.com"
    assert "WARNING" in msg["Subject"]
    body = msg.get_content()
    assert "HP M404 @ 10.0.0.5" in body
    assert "Acme" in body


def test_email_requires_recipients():
    res = EmailChannel("ops", {}).send(NOTE)
    assert res.ok is False
    assert "recipient" in res.detail


def test_freescout_payload():
    ch = FreeScoutChannel("tickets", {"mailbox_id": 7, "customer_email": "alerts@x.com"})
    payload = ch.build_payload(NOTE)
    assert payload["mailboxId"] == 7
    assert payload["type"] == "email"
    assert payload["subject"].startswith("[WARNING]")
    assert payload["threads"][0]["customer"]["email"] == "alerts@x.com"
    assert "printer-nanny" in payload["tags"]


def test_freescout_dry_run_without_creds():
    # No base_url/api_key configured → dry-run success, no HTTP call.
    res = FreeScoutChannel("tickets", {}).send(NOTE)
    assert res.ok is True
    assert "dry-run" in res.detail


def test_teams_payload_and_dry_run():
    ch = TeamsChannel("teams", {})
    assert "WARNING" in ch.build_payload(NOTE)["text"]
    assert ch.send(NOTE).ok is True  # dry-run without webhook


def test_active_channels_from_settings():
    rt = default_settings()
    assert active_channels(rt) == []  # nothing enabled by default
    rt.update({"email.enabled": True, "email.default_recipients": "ops@x.com"})
    rt["freescout.enabled"] = True
    chans = active_channels(rt)
    types = {c.type for c in chans}
    assert types == {"email", "freescout"}
    # email needs recipients to be active
    rt2 = default_settings()
    rt2["email.enabled"] = True  # but no recipients
    assert active_channels(rt2) == []


def test_dispatch_reports_each_channel():
    rt = default_settings()
    rt.update({"freescout.enabled": True})  # dry-run (no creds) → ok
    results = dispatch(NOTE, active_channels(rt))
    assert [name for name, _ in results] == ["FreeScout"]
    assert results[0][1].ok is True  # dry-run success
