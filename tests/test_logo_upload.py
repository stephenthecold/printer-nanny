"""Logo upload: storage, serve, size + type guards, app.logo_url auto-wire."""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central import runtime
from central.dashboard.settings_routes import (
    LOGO_ASSET_NAME, LOGO_MAX_BYTES,
)
from central.main import app
from central.security import hash_password


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"          # PNG magic
    b"\x00\x00\x00\rIHDR"          # IHDR chunk
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def admin_http(db) -> TestClient:
    db.add(m.User(
        username="admin", password_hash=hash_password("admin"), role=m.UserRole.admin,
    ))
    db.commit()
    http = TestClient(app)
    resp = http.post(
        "/login", data={"username": "admin", "password": "admin"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    return http


def test_upload_logo_persists_and_sets_app_logo_url(admin_http, db):
    resp = admin_http.post(
        "/settings/branding/logo",
        files={"logo": ("brand.png", io.BytesIO(PNG_BYTES), "image/png")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    asset = db.get(m.AppAsset, LOGO_ASSET_NAME)
    assert asset is not None
    assert asset.content_type == "image/png"
    assert asset.data == PNG_BYTES
    settings = runtime.load_settings(db)
    assert settings["app.logo_url"] == "/branding/logo"


def test_serve_logo_returns_bytes_with_cache_header(admin_http, db):
    admin_http.post(
        "/settings/branding/logo",
        files={"logo": ("brand.png", io.BytesIO(PNG_BYTES), "image/png")},
        follow_redirects=False,
    )
    resp = TestClient(app).get("/branding/logo")
    assert resp.status_code == 200
    assert resp.content == PNG_BYTES
    assert resp.headers["content-type"] == "image/png"
    assert "max-age" in resp.headers.get("cache-control", "")


def test_serve_logo_404_when_unset(db):
    resp = TestClient(app).get("/branding/logo")
    assert resp.status_code == 404


def test_upload_rejects_unsupported_content_type(admin_http, db):
    admin_http.post(
        "/settings/branding/logo",
        files={"logo": ("evil.exe", io.BytesIO(b"MZ\x90\x00"), "application/x-msdownload")},
        follow_redirects=False,
    )
    assert db.get(m.AppAsset, LOGO_ASSET_NAME) is None
    # The error is queued in the session so the next page render shows it.
    resp = admin_http.get("/settings")
    assert "Unsupported file type" in resp.text


def test_upload_rejects_oversized(admin_http, db):
    big = b"\x00" * (LOGO_MAX_BYTES + 1)
    admin_http.post(
        "/settings/branding/logo",
        files={"logo": ("huge.png", io.BytesIO(big), "image/png")},
        follow_redirects=False,
    )
    assert db.get(m.AppAsset, LOGO_ASSET_NAME) is None


def test_upload_overwrites_existing(admin_http, db):
    admin_http.post(
        "/settings/branding/logo",
        files={"logo": ("a.png", io.BytesIO(PNG_BYTES), "image/png")},
        follow_redirects=False,
    )
    second = b"\x89PNG\r\n\x1a\nDIFFERENT"
    admin_http.post(
        "/settings/branding/logo",
        files={"logo": ("b.png", io.BytesIO(second), "image/png")},
        follow_redirects=False,
    )
    asset = db.get(m.AppAsset, LOGO_ASSET_NAME)
    assert asset.data == second  # latest upload wins, no duplicate row


def test_delete_logo_clears_asset_and_app_logo_url(admin_http, db):
    admin_http.post(
        "/settings/branding/logo",
        files={"logo": ("a.png", io.BytesIO(PNG_BYTES), "image/png")},
        follow_redirects=False,
    )
    assert runtime.load_settings(db)["app.logo_url"] == "/branding/logo"
    admin_http.post("/settings/branding/logo/delete", follow_redirects=False)
    assert db.get(m.AppAsset, LOGO_ASSET_NAME) is None
    # The auto-wired URL is cleared so templates fall back to the emoji.
    assert runtime.load_settings(db)["app.logo_url"] == ""


def test_delete_preserves_external_logo_url(admin_http, db):
    """An operator who pasted an external CDN URL doesn't want it wiped by a
    delete that's really aimed at the upload."""
    runtime.save_settings(db, {"app.logo_url": "https://cdn.example.com/logo.png"})
    admin_http.post("/settings/branding/logo/delete", follow_redirects=False)
    assert runtime.load_settings(db)["app.logo_url"] == "https://cdn.example.com/logo.png"


def test_upload_does_not_clobber_tls_and_channel_settings(admin_http, db):
    """Uploading a logo must not switch off STARTTLS / the alert channels / SSO.

    The upload writes one setting (app.logo_url) outside any settings-form
    group; it used to be treated as a full form post, so every checkbox missing
    from it -- STARTTLS, every channel `enabled`, oidc.enabled -- was recorded
    as unchecked. Credentials survived, so the UI still looked configured while
    alerting was dark and the SMTP password went out in the clear.
    """
    # Configure the fleet the way an operator would: the real Settings form.
    assert admin_http.post("/settings", data={
        "_group": "notifications",
        "smtp.host": "smtp.office365.com", "smtp.port": "587",
        "smtp.user": "alerts@msp.example", "smtp.password": "correct-horse",
        "smtp.use_tls": "on",
        "email.enabled": "on", "email.default_recipients": "ops@msp.example",
        "slack.enabled": "on", "slack.webhook_url": "https://hooks.slack.com/x",
        "teams.enabled": "on", "teams.webhook_url": "https://outlook.office.com/x",
    }, follow_redirects=False).status_code == 303
    assert admin_http.post("/settings", data={
        "_group": "auth", "oidc.enabled": "on", "oidc.issuer": "https://idp/",
    }, follow_redirects=False).status_code == 303
    armed = ("smtp.use_tls", "email.enabled", "slack.enabled", "teams.enabled",
             "oidc.enabled")
    before = runtime.load_settings(db)
    assert all(before[k] is True for k in armed)

    resp = admin_http.post(
        "/settings/branding/logo",
        files={"logo": ("brand.png", io.BytesIO(PNG_BYTES), "image/png")},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    after = runtime.load_settings(db)
    assert after["app.logo_url"] == "/branding/logo"   # the upload still wired up
    assert [k for k in armed if not after[k]] == []    # and nothing else moved
    # The alerting path is still live end-to-end.
    from central.channels import active_channels
    assert {c.name for c in active_channels(after)} >= {"Email", "Slack", "Teams"}


def test_delete_logo_does_not_clobber_tls_and_channel_settings(admin_http, db):
    """Same for the delete path, which clears app.logo_url the same way."""
    admin_http.post(
        "/settings/branding/logo",
        files={"logo": ("a.png", io.BytesIO(PNG_BYTES), "image/png")},
        follow_redirects=False,
    )
    runtime.save_settings(db, {"smtp.use_tls": "on", "email.enabled": "on"})
    admin_http.post("/settings/branding/logo/delete", follow_redirects=False)
    after = runtime.load_settings(db)
    assert after["app.logo_url"] == ""
    assert after["smtp.use_tls"] is True
    assert after["email.enabled"] is True


def test_upload_requires_admin(db):
    db.add(m.User(username="t", password_hash=hash_password("t"), role=m.UserRole.tech))
    db.commit()
    http = TestClient(app)
    http.post("/login", data={"username": "t", "password": "t"}, follow_redirects=False)
    resp = http.post(
        "/settings/branding/logo",
        files={"logo": ("a.png", io.BytesIO(PNG_BYTES), "image/png")},
        follow_redirects=False,
    )
    # Tech bounces to /login (admin-only).
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert db.get(m.AppAsset, LOGO_ASSET_NAME) is None
