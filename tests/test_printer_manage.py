"""Dashboard management routes for individual printers.

Covers the new metadata fields (notes/asset_tag/tags), edit-and-approve,
re-ignore, delete from the detail page, and per-printer Poll-now command.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central.main import app
from central.security import hash_password


@pytest.fixture()
def env(db):
    """Create a tech user + client/site/agent and return them, with a logged-in client."""
    user = m.User(
        username="tech", password_hash=hash_password("tech"), role=m.UserRole.tech
    )
    client = m.Client(name="Acme")
    db.add_all([user, client])
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    agent = m.Agent(site_id=site.id, name="hq agent", api_key_hash="x")
    db.add(agent)
    db.commit()

    http = TestClient(app)
    # Use the real login endpoint so the session cookie is set the same way the
    # browser sets it — that exercises the auth middleware too.
    resp = http.post(
        "/login", data={"username": "tech", "password": "tech"}, follow_redirects=False
    )
    assert resp.status_code == 303
    return {
        "http": http, "user_id": user.id, "client_id": client.id,
        "site_id": site.id, "agent_id": agent.id,
    }


def _fresh(db, model, obj_id):
    """Pull a row fresh from the DB so we see commits from the request handler."""
    db.expire_all()
    return db.get(model, obj_id)


def test_create_printer_persists_notes_asset_tag_tags(env, db):
    resp = env["http"].post("/manage/printers", data={
        "client_id": env["client_id"], "site_id": env["site_id"], "ip": "10.0.0.20",
        "asset_tag": "ACME-IT-001", "tags": "lease, vip, color",
        "notes": "Front desk leased unit.",
    }, follow_redirects=False)
    assert resp.status_code == 303
    p = db.query(m.Printer).filter_by(ip="10.0.0.20").one()
    assert p.asset_tag == "ACME-IT-001"
    assert p.tags == ["lease", "vip", "color"]
    assert p.notes == "Front desk leased unit."
    # New printers default to approved (manually added → already trusted).
    assert p.discovery_state == m.DiscoveryState.approved


def test_blank_tags_stored_as_none(env, db):
    env["http"].post("/manage/printers", data={
        "client_id": env["client_id"], "site_id": env["site_id"], "ip": "10.0.0.21",
        "tags": "  ,  ,",  # all whitespace/commas → no real tags
    }, follow_redirects=False)
    p = db.query(m.Printer).filter_by(ip="10.0.0.21").one()
    assert p.tags is None


def test_update_with_approve_flag_approves_pending_printer(env, db):
    pending = m.Printer(
        client_id=env["client_id"], site_id=env["site_id"], ip="10.0.0.30",
        discovery_state=m.DiscoveryState.pending,
    )
    db.add(pending)
    db.commit()
    resp = env["http"].post(f"/manage/printers/{pending.id}", data={
        "site_id": env["site_id"], "ip": "10.0.0.30", "location": "Lobby",
        "approve": "1",
    }, follow_redirects=False)
    assert resp.status_code == 303
    # Approved printers bounce back to /approvals so the operator keeps working.
    assert resp.headers["location"] == "/approvals"
    p = _fresh(db, m.Printer, pending.id)
    assert p.discovery_state == m.DiscoveryState.approved
    assert p.location == "Lobby"


def test_update_without_approve_keeps_pending(env, db):
    pending = m.Printer(
        client_id=env["client_id"], site_id=env["site_id"], ip="10.0.0.31",
        discovery_state=m.DiscoveryState.pending,
    )
    db.add(pending)
    db.commit()
    env["http"].post(f"/manage/printers/{pending.id}", data={
        "site_id": env["site_id"], "ip": "10.0.0.31", "location": "Lobby",
    }, follow_redirects=False)
    p = _fresh(db, m.Printer, pending.id)
    assert p.discovery_state == m.DiscoveryState.pending
    assert p.location == "Lobby"


def test_printer_ignore_moves_to_ignored(env, db):
    approved = m.Printer(
        client_id=env["client_id"], site_id=env["site_id"], ip="10.0.0.40",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(approved)
    db.commit()
    resp = env["http"].post(
        f"/manage/printers/{approved.id}/ignore", follow_redirects=False
    )
    assert resp.status_code == 303
    p = _fresh(db, m.Printer, approved.id)
    assert p.discovery_state == m.DiscoveryState.ignored


def test_printer_approve_route_works_for_ignored(env, db):
    """Re-approval (resume monitoring) is the same one-click route."""
    ignored = m.Printer(
        client_id=env["client_id"], site_id=env["site_id"], ip="10.0.0.41",
        discovery_state=m.DiscoveryState.ignored,
    )
    db.add(ignored)
    db.commit()
    env["http"].post(f"/manage/printers/{ignored.id}/approve", follow_redirects=False)
    p = _fresh(db, m.Printer, ignored.id)
    assert p.discovery_state == m.DiscoveryState.approved


def test_poll_now_enqueues_command_for_owning_agent(env, db):
    printer = m.Printer(
        client_id=env["client_id"], site_id=env["site_id"], ip="10.0.0.50",
        discovery_state=m.DiscoveryState.approved,
        discovered_by_agent_id=env["agent_id"],
    )
    db.add(printer)
    db.commit()
    resp = env["http"].post(
        f"/manage/printers/{printer.id}/poll", follow_redirects=False
    )
    assert resp.status_code == 303
    cmd = db.query(m.Command).filter_by(agent_id=env["agent_id"]).one()
    assert cmd.type == m.CommandType.poll_printer
    assert cmd.payload == {"printer_id": printer.id, "ip": "10.0.0.50"}
    assert cmd.status == m.CommandStatus.pending


def test_poll_now_falls_back_to_site_agent_when_no_discoverer(env, db):
    """If the printer was added manually it has no discovered_by_agent_id; use the site's agent."""
    printer = m.Printer(
        client_id=env["client_id"], site_id=env["site_id"], ip="10.0.0.51",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.commit()
    env["http"].post(f"/manage/printers/{printer.id}/poll", follow_redirects=False)
    cmd = db.query(m.Command).filter_by(agent_id=env["agent_id"]).one()
    assert cmd.payload["ip"] == "10.0.0.51"


def test_poll_now_without_any_agent_flashes_error(env, db):
    """No agent at the site → no command, just a flash. (Don't crash.)"""
    other_client = m.Client(name="Other")
    db.add(other_client)
    db.flush()
    other_site = m.Site(client_id=other_client.id, name="branch")  # no agent on this site
    db.add(other_site)
    db.flush()
    printer = m.Printer(
        client_id=other_client.id, site_id=other_site.id, ip="10.0.0.60",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.commit()
    resp = env["http"].post(
        f"/manage/printers/{printer.id}/poll", follow_redirects=False
    )
    assert resp.status_code == 303
    assert db.query(m.Command).count() == 0


def test_printer_form_renders_tags_as_csv(env, db):
    printer = m.Printer(
        client_id=env["client_id"], site_id=env["site_id"], ip="10.0.0.70",
        tags=["lease", "vip"],
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.commit()
    resp = env["http"].get(f"/manage/printers/{printer.id}/edit")
    assert resp.status_code == 200
    # Tags rendered comma-joined so the input round-trips on save.
    assert "lease, vip" in resp.text


def test_printer_detail_shows_action_buttons_for_tech(env, db):
    printer = m.Printer(
        client_id=env["client_id"], site_id=env["site_id"], ip="10.0.0.80",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.commit()
    resp = env["http"].get(f"/printers/{printer.id}")
    assert resp.status_code == 200
    # Poll-now, Edit, Stop monitoring should all be reachable from the detail view.
    assert f"/manage/printers/{printer.id}/poll" in resp.text
    assert f"/manage/printers/{printer.id}/edit" in resp.text
    assert f"/manage/printers/{printer.id}/ignore" in resp.text


def test_approvals_page_links_to_review_form(env, db):
    pending = m.Printer(
        client_id=env["client_id"], site_id=env["site_id"], ip="10.0.0.90",
        discovery_state=m.DiscoveryState.pending,
    )
    db.add(pending)
    db.commit()
    resp = env["http"].get("/approvals")
    assert resp.status_code == 200
    assert f"/manage/printers/{pending.id}/edit?from_approvals=1" in resp.text
