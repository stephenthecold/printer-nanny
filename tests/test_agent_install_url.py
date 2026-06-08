"""The Agents page emits HTTPS install commands when fronted by TLS."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central import runtime
from central.main import app
from central.security import hash_password


def _enroll_agent(http: TestClient, site_id: int) -> None:
    """Create an agent so the install command snippet renders in the next GET."""
    resp = http.post(
        "/manage/agents", data={"site_id": site_id, "name": "hq-agent"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


@pytest.fixture()
def http(db) -> TestClient:
    db.add(m.User(
        username="admin", password_hash=hash_password("admin"), role=m.UserRole.admin,
    ))
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    cli.site_id = site.id  # for tests
    return cli


def test_install_command_uses_public_url_when_pinned(http, db):
    """app.public_url overrides the request URL, so the snippet on /manage/agents
    is always the public HTTPS hostname even when this request hit via http://."""
    runtime.save_settings(db, {"app.public_url": "https://printers.example.com"})
    _enroll_agent(http, http.site_id)
    resp = http.get("/manage/agents")
    assert resp.status_code == 200
    assert "https://printers.example.com" in resp.text


def test_install_command_honors_x_forwarded_proto_when_no_public_url(http, db):
    """Without an app.public_url pin, the ProxyHeadersMiddleware lets the
    install snippet pick up the X-Forwarded-Proto header from Caddy."""
    _enroll_agent(http, http.site_id)
    resp = http.get(
        "/manage/agents",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "printers.test"},
    )
    assert resp.status_code == 200
    # When the public URL isn't pinned, the request URL is used. With
    # X-Forwarded-Proto=https, that's https://… . We don't assert the host
    # because Starlette joins the path back as testserver here; only the
    # scheme matters for "agent → central traffic is TLS".
    assert "--central-url https://" in resp.text


def test_install_command_falls_back_to_request_when_no_proxy_headers(http, db):
    _enroll_agent(http, http.site_id)
    resp = http.get("/manage/agents")
    assert resp.status_code == 200
    # The TestClient default base is http://testserver — that's what we expect
    # the install snippet to use when there are no proxy headers and no pin.
    assert "--central-url http://testserver" in resp.text


def test_enroll_renders_windows_powershell_command(http, db):
    """Enrolling a new agent must show a Windows PowerShell one-liner alongside
    the Linux one — Windows Server is the canonical agent host for MSPs whose
    Linux box can't reach the printer VLAN."""
    runtime.save_settings(db, {"app.public_url": "https://printers.example.com"})
    _enroll_agent(http, http.site_id)
    resp = http.get("/manage/agents")
    assert resp.status_code == 200
    assert "install-agent.ps1" in resp.text
    assert "iwr -useb" in resp.text
    assert "$env:PN_CENTRAL_URL" in resp.text
    assert "$env:PN_AGENT_ID" in resp.text
    assert "$env:PN_API_KEY" in resp.text
    # PipSource must be carried in via env var, because `iwr | iex` can't pass
    # parameters to the executed script — without this the PS1 falls back to
    # the 'your-org' placeholder and refuses to install.
    assert "$env:PN_PIP_SOURCE" in resp.text
