"""Webhook + Slack channels: payload builders, severity filter, dispatch wiring."""

from __future__ import annotations

import httpx

from central.channels import Notification, active_channels, dispatch
from central.channels.slack import SlackChannel
from central.channels.webhook import WebhookChannel
from central.runtime import default_settings

NOTE_WARN = Notification(
    title="Low magenta on HP M404 @ 10.0.0.5",
    body="Magenta at 4% (threshold 10%).",
    severity="warning",
    client_name="Acme",
    site_name="HQ",
    printer_label="HP M404 @ 10.0.0.5",
    alert_id=42,
)

NOTE_INFO = Notification(
    title="Maintenance reminder",
    body="Quarterly clean due next week.",
    severity="info",
)

NOTE_CRIT = Notification(
    title="Offline 30 minutes",
    body="No SNMP response from 10.0.0.5",
    severity="critical",
)


# ---------- webhook ----------

def test_webhook_payload_shape_is_stable():
    ch = WebhookChannel("hooks", {})
    payload = ch.build_payload(NOTE_WARN)
    assert payload["source"] == "printer-nanny"
    assert payload["title"] == NOTE_WARN.title
    assert payload["body"] == NOTE_WARN.body
    assert payload["severity"] == "warning"
    assert payload["client"] == "Acme"
    assert payload["site"] == "HQ"
    assert payload["printer"] == "HP M404 @ 10.0.0.5"
    assert payload["alert_id"] == 42
    # Timestamp present and ISO-8601 with Z suffix.
    assert payload["timestamp"].endswith("Z")
    # app name picks up app.name default.
    assert payload["app"] == "Printer Nanny"


def test_webhook_dry_run_without_url():
    res = WebhookChannel("hooks", {}).send(NOTE_WARN)
    assert res.ok is True
    assert "dry-run" in res.detail


def test_webhook_min_severity_filters(monkeypatch):
    rt = default_settings()
    rt["webhook.url"] = "https://example.invalid/hook"
    rt["webhook.min_severity"] = "critical"
    ch = WebhookChannel("hooks", {}, rt)

    posted = []

    def fake_post(url, **kwargs):
        posted.append((url, kwargs))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    # Info + warning: skipped (below critical).
    assert ch.send(NOTE_INFO).ok is True
    assert "skipped" in ch.send(NOTE_INFO).detail
    assert ch.send(NOTE_WARN).ok is True
    assert "skipped" in ch.send(NOTE_WARN).detail
    assert posted == []  # no HTTP calls fired
    # Critical: fires.
    assert ch.send(NOTE_CRIT).ok is True
    assert len(posted) == 1


def test_webhook_auth_header_default_is_authorization(monkeypatch):
    rt = default_settings()
    rt["webhook.url"] = "https://example.invalid/hook"
    rt["webhook.auth_token"] = "Bearer abc123"
    ch = WebhookChannel("hooks", {}, rt)

    captured = {}

    def fake_post(url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return httpx.Response(202, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    res = ch.send(NOTE_WARN)
    assert res.ok is True
    assert captured["headers"]["Authorization"] == "Bearer abc123"
    assert captured["headers"]["Content-Type"] == "application/json"


def test_webhook_custom_auth_header(monkeypatch):
    rt = default_settings()
    rt["webhook.url"] = "https://example.invalid/hook"
    rt["webhook.auth_header"] = "X-Api-Key"
    rt["webhook.auth_token"] = "secret-xyz"
    ch = WebhookChannel("hooks", {}, rt)

    captured = {}

    def fake_post(url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    ch.send(NOTE_WARN)
    assert captured["headers"]["X-Api-Key"] == "secret-xyz"
    assert "Authorization" not in captured["headers"]


def test_webhook_non_2xx_reported_as_failure(monkeypatch):
    rt = default_settings()
    rt["webhook.url"] = "https://example.invalid/hook"
    ch = WebhookChannel("hooks", {}, rt)

    def fake_post(url, **kwargs):
        return httpx.Response(
            500, content=b"server crashed", request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    res = ch.send(NOTE_WARN)
    assert res.ok is False
    assert "500" in res.detail


def test_webhook_http_error_reported(monkeypatch):
    rt = default_settings()
    rt["webhook.url"] = "https://example.invalid/hook"
    ch = WebhookChannel("hooks", {}, rt)

    def fake_post(url, **kwargs):
        raise httpx.ConnectError("dns failed", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    res = ch.send(NOTE_WARN)
    assert res.ok is False
    assert "request error" in res.detail


# ---------- slack ----------

def test_slack_payload_severity_colors():
    ch = SlackChannel("slack", {})
    warn = ch.build_payload(NOTE_WARN)
    info = ch.build_payload(NOTE_INFO)
    crit = ch.build_payload(NOTE_CRIT)
    assert warn["attachments"][0]["color"] == "warning"
    assert info["attachments"][0]["color"] == "good"
    assert crit["attachments"][0]["color"] == "danger"


def test_slack_payload_fields_and_footer():
    ch = SlackChannel("slack", {})
    payload = ch.build_payload(NOTE_WARN)
    assert "Low magenta" in payload["text"]
    fields = {f["title"]: f["value"] for f in payload["attachments"][0]["fields"]}
    assert fields["Client"] == "Acme"
    assert fields["Site"] == "HQ"
    assert fields["Printer"] == "HP M404 @ 10.0.0.5"
    assert payload["attachments"][0]["footer"] == "Printer Nanny"
    assert payload["attachments"][0]["text"] == NOTE_WARN.body


def test_slack_payload_skips_missing_fields():
    """A plain notification (no client/site/printer) shouldn't render empty fields."""
    ch = SlackChannel("slack", {})
    payload = ch.build_payload(NOTE_INFO)
    assert payload["attachments"][0]["fields"] == []


def test_slack_dry_run_without_webhook():
    res = SlackChannel("slack", {}).send(NOTE_WARN)
    assert res.ok is True
    assert "dry-run" in res.detail


def test_slack_min_severity_filters(monkeypatch):
    rt = default_settings()
    rt["slack.webhook_url"] = "https://hooks.slack.com/services/T/B/x"
    rt["slack.min_severity"] = "warning"
    ch = SlackChannel("slack", {}, rt)

    posted = []

    def fake_post(url, **kwargs):
        posted.append(url)
        return httpx.Response(200, content=b"ok", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    ch.send(NOTE_INFO)  # filtered
    ch.send(NOTE_WARN)  # fired
    ch.send(NOTE_CRIT)  # fired
    assert len(posted) == 2


def test_slack_failure_reported(monkeypatch):
    rt = default_settings()
    rt["slack.webhook_url"] = "https://hooks.slack.com/services/T/B/x"
    ch = SlackChannel("slack", {}, rt)

    def fake_post(url, **kwargs):
        return httpx.Response(
            403, content=b"invalid_token", request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    res = ch.send(NOTE_WARN)
    assert res.ok is False
    assert "403" in res.detail


# ---------- dispatch wiring ----------

def test_active_channels_includes_webhook_and_slack_when_enabled():
    rt = default_settings()
    assert active_channels(rt) == []  # baseline -- nothing enabled
    rt["webhook.enabled"] = True
    rt["slack.enabled"] = True
    types = {c.type for c in active_channels(rt)}
    assert "webhook" in types
    assert "slack" in types


def test_teams_active_channel_now_returned_when_enabled():
    """Teams used to be defined but never returned by active_channels -- fixed."""
    rt = default_settings()
    rt["teams.enabled"] = True
    types = {c.type for c in active_channels(rt)}
    assert "teams" in types


def test_dispatch_reports_each_new_channel():
    rt = default_settings()
    rt["webhook.enabled"] = True  # dry-run (no url) -> ok
    rt["slack.enabled"] = True    # dry-run (no webhook) -> ok
    results = dispatch(NOTE_WARN, active_channels(rt))
    by_name = {name: result for name, result in results}
    assert "Webhook" in by_name
    assert "Slack" in by_name
    assert by_name["Webhook"].ok is True
    assert by_name["Slack"].ok is True
