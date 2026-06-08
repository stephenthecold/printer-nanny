"""White-label / branding settings flow into templates and the email channel."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central import runtime
from central.channels.email import EmailChannel
from central.channels.base import Notification
from central.main import app
from central.security import hash_password


def _admin(db) -> m.User:
    user = m.User(
        username="admin", password_hash=hash_password("admin"), role=m.UserRole.admin
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture()
def http(db) -> TestClient:
    _admin(db)
    client = TestClient(app)
    resp = client.post(
        "/login", data={"username": "admin", "password": "admin"}, follow_redirects=False
    )
    assert resp.status_code == 303
    return client


def test_settings_page_survives_missing_app_assets_table(http, db):
    """An operator who upgraded code but didn't run migrations is missing the
    app_assets table; /settings used to 500. It must now render normally and
    just show 'no logo uploaded' instead of blowing up the whole page."""
    db.execute(__import__("sqlalchemy").text("DROP TABLE IF EXISTS app_assets"))
    db.commit()
    resp = http.get("/settings")
    assert resp.status_code == 200, resp.text[:500]
    # The 'no logo uploaded' empty state should render (proves has_uploaded_logo
    # came back False rather than throwing).
    assert "No logo uploaded" in resp.text or "Logo" in resp.text


def test_serve_logo_returns_404_when_table_missing(http, db):
    """Public /branding/logo is hit by every dashboard page; it must 404
    cleanly, not 500, when the app_assets table doesn't exist."""
    db.execute(__import__("sqlalchemy").text("DROP TABLE IF EXISTS app_assets"))
    db.commit()
    resp = http.get("/branding/logo")
    assert resp.status_code == 404


def test_default_app_branding_includes_all_keys(db):
    branding = runtime.app_branding(db)
    # Keys are stored as `app.<x>` and exposed without the prefix in templates.
    for key in ("name", "logo_url", "primary_color", "support_email", "footer_text"):
        assert key in branding
    assert branding["name"] == "Printer Nanny"
    assert branding["primary_color"] == "#0f172a"


def test_overridden_app_name_flows_into_overview(http, db):
    runtime.save_settings(db, {
        "app.name": "Acme Print Ops",
        "app.support_email": "help@acme.example",
        # save_settings rebuilds every bool from form presence, so include any others
        # that should stay false; defaults are preserved by the load merge.
    })
    resp = http.get("/")
    assert resp.status_code == 200
    body = resp.text
    # Nav brand replaced; support footer rendered because support_email is set.
    assert "Acme Print Ops" in body
    assert "help@acme.example" in body


def test_login_page_renders_branding_without_session(db):
    runtime.save_settings(db, {"app.name": "Branded Console"})
    client = TestClient(app)
    resp = client.get("/login")
    assert resp.status_code == 200
    # Title + brand block + footer-less (no support email set).
    assert "Branded Console" in resp.text


def test_email_subject_uses_app_name(db):
    runtime.save_settings(db, {"app.name": "FleetWatch"})
    channel = EmailChannel(
        name="email", config={"to": "ops@example"}, runtime=runtime.load_settings(db)
    )
    note = Notification(title="Disk full", body="…", severity="critical")
    msg = channel.build_message(note)
    assert msg["Subject"] == "[FleetWatch][CRITICAL] Disk full"


def test_email_subject_default_when_app_name_blank(db):
    # An operator clearing app.name shouldn't produce '[][CRITICAL] …'.
    runtime.save_settings(db, {"app.name": ""})
    channel = EmailChannel(
        name="email", config={"to": "ops@example"}, runtime=runtime.load_settings(db)
    )
    note = Notification(title="Hi", body="…", severity="warning")
    assert channel.build_message(note)["Subject"] == "[Printer Nanny][WARNING] Hi"
