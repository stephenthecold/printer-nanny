"""OAuth-SMTP (XOAUTH2) path: token refresh, SASL string, send-with-Bearer."""

from __future__ import annotations

import base64
import time
from typing import Optional
from unittest.mock import patch

import httpx
import pytest

from central import auth_oauth_smtp as oauth_smtp
from central import runtime
from central.channels import Notification
from central.channels.email import EmailChannel


NOTE = Notification(
    title="Disk full",
    body="Tray 2 paper jam on HP M404",
    severity="critical",
)


# --- XOAUTH2 SASL builder --------------------------------------------------- #
def test_build_xoauth2_format():
    raw = oauth_smtp.build_xoauth2("user@example.com", "ya29.abc")
    decoded = base64.b64decode(raw).decode("utf-8")
    # RFC: user=<email>^Aauth=Bearer <token>^A^A (where ^A = 0x01)
    assert decoded == "user=user@example.com\x01auth=Bearer ya29.abc\x01\x01"


# --- Token refresh logic ---------------------------------------------------- #
class _FakeResponse:
    def __init__(self, json_data: dict, status: int = 200):
        self._json = json_data
        self.status_code = status

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("oops", request=None, response=None)


def _settings(**overrides) -> dict:
    base = runtime.default_settings()
    base.update(overrides)
    return base


def test_refresh_returns_cached_token_when_not_expired(db):
    """A token that doesn't expire for another hour must be returned as-is — no
    network call, no DB write."""
    now = int(time.time())
    settings = _settings(**{
        "smtp.auth_type": "oauth_google",
        "smtp.oauth_client_id": "cid",
        "smtp.oauth_refresh_token": "rt",
        "smtp.oauth_access_token": "cached_token",
        "smtp.oauth_access_token_expires_at": now + 3600,
    })
    with patch.object(httpx, "post") as post:
        token = oauth_smtp.refresh_access_token(db, settings)
    assert token == "cached_token"
    post.assert_not_called()


def test_refresh_calls_token_endpoint_when_expired(db):
    """Expired access token → POST to the provider's token endpoint, persist
    the new token + expires_at via runtime.save_settings."""
    now = int(time.time())
    settings = _settings(**{
        "smtp.auth_type": "oauth_google",
        "smtp.oauth_client_id": "cid",
        "smtp.oauth_client_secret": "csec",
        "smtp.oauth_refresh_token": "rt",
        "smtp.oauth_access_token": "old",
        "smtp.oauth_access_token_expires_at": now - 100,  # expired
    })
    fake = _FakeResponse({"access_token": "fresh_token", "expires_in": 3599})
    with patch.object(httpx, "post", return_value=fake) as post:
        token = oauth_smtp.refresh_access_token(db, settings)
    assert token == "fresh_token"
    # Posted to Google's token endpoint with the right grant params.
    args, kwargs = post.call_args
    assert args[0] == "https://oauth2.googleapis.com/token"
    assert kwargs["data"]["grant_type"] == "refresh_token"
    assert kwargs["data"]["refresh_token"] == "rt"
    assert kwargs["data"]["client_id"] == "cid"
    assert kwargs["data"]["client_secret"] == "csec"
    # Persisted: a fresh load_settings sees the new token.
    saved = runtime.load_settings(db)
    assert saved["smtp.oauth_access_token"] == "fresh_token"
    assert int(saved["smtp.oauth_access_token_expires_at"]) >= now + 3000


def test_refresh_persists_rolling_refresh_token(db):
    """Google sometimes rotates the refresh token; if the provider returns one,
    we must overwrite the stored value or future refreshes will fail."""
    settings = _settings(**{
        "smtp.auth_type": "oauth_microsoft",
        "smtp.oauth_client_id": "cid",
        "smtp.oauth_refresh_token": "old_rt",
        "smtp.oauth_access_token_expires_at": 0,
    })
    fake = _FakeResponse({
        "access_token": "ms_token",
        "refresh_token": "new_rt",
        "expires_in": 3600,
    })
    with patch.object(httpx, "post", return_value=fake):
        oauth_smtp.refresh_access_token(db, settings)
    saved = runtime.load_settings(db)
    assert saved["smtp.oauth_refresh_token"] == "new_rt"


def test_refresh_no_op_when_basic_auth(db):
    """auth_type=basic must skip the OAuth machinery entirely."""
    settings = _settings(**{"smtp.auth_type": "basic"})
    with patch.object(httpx, "post") as post:
        assert oauth_smtp.refresh_access_token(db, settings) is None
    post.assert_not_called()


def test_refresh_no_op_without_refresh_token(db):
    """Missing refresh token (operator hasn't run Connect yet) → None, no call."""
    settings = _settings(**{
        "smtp.auth_type": "oauth_google", "smtp.oauth_refresh_token": "",
    })
    with patch.object(httpx, "post") as post:
        assert oauth_smtp.refresh_access_token(db, settings) is None
    post.assert_not_called()


# --- Provider config -------------------------------------------------------- #
def test_provider_config_google():
    cfg = oauth_smtp._provider_config("google", "")
    assert cfg["token_url"] == "https://oauth2.googleapis.com/token"
    assert "mail.google.com" in cfg["scope"]
    assert cfg["extra_auth"]["access_type"] == "offline"


def test_provider_config_microsoft_uses_tenant():
    cfg = oauth_smtp._provider_config("microsoft", "abc-tenant-guid")
    assert "abc-tenant-guid" in cfg["auth_url"]
    assert "abc-tenant-guid" in cfg["token_url"]
    assert "offline_access" in cfg["scope"]
    assert "SMTP.Send" in cfg["scope"]


def test_provider_config_microsoft_defaults_to_common():
    cfg = oauth_smtp._provider_config("microsoft", "")
    assert "/common/" in cfg["auth_url"]


def test_provider_config_unknown_raises():
    with pytest.raises(ValueError):
        oauth_smtp._provider_config("aws-ses", "")


# --- EmailChannel OAuth send path ------------------------------------------ #
class _FakeSMTP:
    """In-memory SMTP that records auth + send without touching the network."""

    def __init__(self, host: str, port: int, timeout: int):
        self.host, self.port = host, port
        self.started_tls = False
        self.basic_login: Optional[tuple] = None
        self.auth_lines: list = []
        self.messages: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.basic_login = (user, password)

    def docmd(self, cmd, args):
        self.auth_lines.append((cmd, args))
        return (235, b"OK")

    def send_message(self, msg):
        self.messages.append(msg)


def _runtime_oauth_ready(**overrides) -> dict:
    rt = runtime.default_settings()
    rt.update({
        "app.name": "Test",
        "smtp.host": "smtp.gmail.com",
        "smtp.port": 587,
        "smtp.user": "alerts@example.com",
        "smtp.use_tls": True,
        "smtp.auth_type": "oauth_google",
        "smtp.oauth_access_token": "ya29.fake",
        "smtp.oauth_access_token_expires_at": int(time.time()) + 3600,
    })
    rt.update(overrides)
    return rt


def test_email_send_uses_xoauth2_when_auth_type_oauth():
    """OAuth send path: STARTTLS, AUTH XOAUTH2 <b64>, send_message — no basic login."""
    rt = _runtime_oauth_ready()
    channel = EmailChannel("Email", config={"to": "ops@example"}, runtime=rt)
    with patch("smtplib.SMTP") as smtp_cls:
        smtp_cls.return_value = _FakeSMTP("smtp.gmail.com", 587, 15)
        result = channel.send(NOTE)
    assert result.ok is True
    fake = smtp_cls.return_value
    assert fake.started_tls is True
    assert fake.basic_login is None
    assert fake.auth_lines and fake.auth_lines[0][0] == "AUTH"
    sasl_b64 = fake.auth_lines[0][1].split(" ", 1)[1]
    decoded = base64.b64decode(sasl_b64).decode("utf-8")
    assert decoded == "user=alerts@example.com\x01auth=Bearer ya29.fake\x01\x01"
    assert len(fake.messages) == 1


def test_email_send_falls_back_to_no_token_error():
    rt = _runtime_oauth_ready(**{"smtp.oauth_access_token": ""})
    channel = EmailChannel("Email", config={"to": "ops@example"}, runtime=rt)
    with patch("smtplib.SMTP") as smtp_cls:
        smtp_cls.return_value = _FakeSMTP("h", 587, 15)
        result = channel.send(NOTE)
    assert result.ok is False
    assert "no OAuth access token" in result.detail


def test_email_send_oauth_requires_smtp_user():
    rt = _runtime_oauth_ready(**{"smtp.user": ""})
    channel = EmailChannel("Email", config={"to": "ops@example"}, runtime=rt)
    with patch("smtplib.SMTP") as smtp_cls:
        smtp_cls.return_value = _FakeSMTP("h", 587, 15)
        result = channel.send(NOTE)
    assert result.ok is False
    assert "smtp.user" in result.detail


def test_email_send_basic_path_unchanged_when_auth_type_basic():
    """Backwards compatibility: with auth_type=basic the channel still calls smtp.login()."""
    rt = runtime.default_settings()
    rt.update({
        "smtp.host": "h", "smtp.port": 587, "smtp.user": "u",
        "smtp.password": "p", "smtp.use_tls": True, "smtp.auth_type": "basic",
    })
    channel = EmailChannel("Email", config={"to": "ops@example"}, runtime=rt)
    with patch("smtplib.SMTP") as smtp_cls:
        smtp_cls.return_value = _FakeSMTP("h", 587, 15)
        channel.send(NOTE)
    fake = smtp_cls.return_value
    assert fake.basic_login == ("u", "p")
    assert fake.auth_lines == []  # no XOAUTH2


def test_email_send_oauth_auth_failure_surfaces_error():
    """Provider rejects the Bearer token → SMTPAuthenticationError → ChannelResult.ok=False."""
    import smtplib

    rt = _runtime_oauth_ready()
    channel = EmailChannel("Email", config={"to": "ops@example"}, runtime=rt)

    class _FakeSMTPFail(_FakeSMTP):
        def docmd(self, cmd, args):
            return (535, b"auth failed")

    with patch("smtplib.SMTP") as smtp_cls:
        smtp_cls.return_value = _FakeSMTPFail("h", 587, 15)
        # Ensure we still get a clean ChannelResult, not an exception.
        result = channel.send(NOTE)
    assert result.ok is False
    assert "smtp auth failed" in result.detail
    # Sanity: the test never imported smtplib for nothing.
    assert smtplib is not None


# --- Settings masking persists across refresh ------------------------------ #
def test_refresh_token_is_masked_for_form(db):
    settings = _settings(**{"smtp.oauth_refresh_token": "supersecret"})
    masked = runtime.masked_for_form(settings)
    assert masked["smtp.oauth_refresh_token"] == runtime.SECRET_PLACEHOLDER
    assert masked["smtp.oauth_access_token"] != "supersecret"
