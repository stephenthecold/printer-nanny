"""Outdated-agent detection + scoped "update all outdated" action.

Covers three things the operator relies on:
  1. The pure version compare (``central.agent_release``): older/newer/equal,
     never-reported (None), malformed strings, and the ``+marker`` suffix that
     must be ignored. A string compare would get "0.10.0" vs "0.9.0" wrong --
     these tests pin the tuple-of-ints behaviour.
  2. The Agents page badge: an outdated agent shows "Update available", a
     current agent shows "up to date", a never-reported agent shows "unknown".
  3. POST /manage/agents/update-outdated queues update_agent ONLY for outdated
     agents, audits the action, and never double-queues an agent that already
     has a pending update.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from central import models as m
from central.agent_release import (
    agent_base,
    bundled_agent_version,
    compare_versions,
    needs_update,
    update_state,
)
from central.main import app
from central.security import generate_api_key, hash_api_key, hash_password

TARGET = "0.3.0"  # what bundled_agent_version() resolves to in this checkout


# --------------------------------------------------------------------------- #
# Pure version compare
# --------------------------------------------------------------------------- #

def test_bundled_agent_version_matches_package_base():
    """The served target is the agent package's base version, no marker."""
    from printer_nanny_agent import __base_version__
    assert bundled_agent_version() == agent_base(__base_version__)
    assert "+" not in bundled_agent_version()


def test_agent_base_strips_install_marker():
    assert agent_base("0.1.0+20250101-000000") == "0.1.0"
    assert agent_base("0.3.0") == "0.3.0"
    assert agent_base("0.3.0+20260601-000000") == "0.3.0"


def test_agent_base_none_and_empty():
    assert agent_base(None) == ""
    assert agent_base("") == ""
    assert agent_base("   ") == ""


def test_needs_update_older_is_true():
    assert needs_update("0.1.0+20250101-000000", TARGET) is True
    assert needs_update("0.2.9", TARGET) is True


def test_needs_update_equal_is_false():
    assert needs_update("0.3.0+20260601-000000", TARGET) is False
    assert needs_update("0.3.0", TARGET) is False


def test_needs_update_newer_is_false():
    # Ahead/canary agent -- not "needs update".
    assert needs_update("0.4.0", TARGET) is False
    assert needs_update("1.0.0+20260601-000000", TARGET) is False


def test_needs_update_none_is_false_but_unknown_state():
    """A never-reported agent must be UNKNOWN, not silently up-to-date."""
    assert needs_update(None, TARGET) is False
    assert update_state(None, TARGET) == "unknown"
    assert needs_update("", TARGET) is False
    assert update_state("", TARGET) == "unknown"


def test_needs_update_malformed_is_unknown():
    assert needs_update("garbage", TARGET) is False
    assert update_state("garbage", TARGET) == "unknown"
    assert needs_update("v", TARGET) is False


def test_compare_uses_numeric_not_string_order():
    """0.10.0 > 0.9.0 numerically; a naive string compare gets this wrong."""
    assert compare_versions("0.10.0", "0.9.0") == 1
    assert needs_update("0.9.0", "0.10.0") is True
    assert needs_update("0.10.0", "0.9.0") is False


def test_compare_pads_short_versions():
    assert compare_versions("0.3", "0.3.0") == 0
    assert compare_versions("0.3", "0.3.1") == -1


def test_compare_tolerates_leading_v_and_prerelease_suffix():
    assert compare_versions("v0.3.0", "0.3.0") == 0
    assert compare_versions("0.3.0-rc1", "0.3.0") == 0  # leading dotted-int run only


def test_update_state_buckets():
    assert update_state("0.1.0+x", TARGET) == "outdated"
    assert update_state("0.3.0", TARGET) == "current"
    assert update_state("0.9.0", TARGET) == "ahead"
    assert update_state(None, TARGET) == "unknown"


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #

def _seed_site(db) -> m.Site:
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    return site


def _add_agent(db, site, name, version):
    agent = m.Agent(
        site_id=site.id, name=name,
        api_key_hash=hash_api_key(generate_api_key()),
        version=version,
    )
    db.add(agent)
    db.flush()
    return agent


def _set_real_pip_source(db):
    db.add(m.AppSetting(
        key="agent.pip_source",
        value="git+https://github.com/stephenthecold/printer-nanny.git#subdirectory=agent",
    ))


def _login_admin(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("pw"), role=m.UserRole.admin))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"}, follow_redirects=False)
    return cli


def _login_tech(db) -> TestClient:
    db.add(m.User(username="tech", password_hash=hash_password("pw"), role=m.UserRole.tech))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "tech", "password": "pw"}, follow_redirects=False)
    return cli


def _pending_update_agent_ids(db) -> set:
    return set(db.scalars(
        select(m.Command.agent_id).where(
            m.Command.type == m.CommandType.update_agent,
            m.Command.status == m.CommandStatus.pending,
        )
    ))


# --------------------------------------------------------------------------- #
# Badge rendering
# --------------------------------------------------------------------------- #

def test_agents_page_badges_outdated_current_unknown(db):
    site = _seed_site(db)
    _add_agent(db, site, "old", "0.1.0+20250101-000000")
    _add_agent(db, site, "cur", f"{TARGET}+20260601-000000")
    _add_agent(db, site, "new", None)
    _set_real_pip_source(db)
    cli = _login_admin(db)
    resp = cli.get("/manage/agents", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    # Outdated agent surfaces the "Update available" badge pointing at target.
    assert "Update available" in body
    assert f"&rarr; v{TARGET}" in body or f"-> v{TARGET}" in body
    # Current + unknown states render too.
    assert "up to date" in body
    assert "version unknown" in body


def test_agents_page_current_only_shows_no_update_badge(db):
    site = _seed_site(db)
    _add_agent(db, site, "cur", f"{TARGET}+20260601-000000")
    _set_real_pip_source(db)
    cli = _login_admin(db)
    body = cli.get("/manage/agents", follow_redirects=False).text
    assert "Update available" not in body
    assert "up to date" in body
    # Nothing outdated -> the bulk action is disabled.
    assert "All up to date" in body


def test_agents_page_shows_outdated_count_in_bulk_button(db):
    site = _seed_site(db)
    _add_agent(db, site, "old1", "0.1.0+x")
    _add_agent(db, site, "old2", "0.2.0+x")
    _add_agent(db, site, "cur", TARGET)
    _set_real_pip_source(db)
    cli = _login_admin(db)
    body = cli.get("/manage/agents", follow_redirects=False).text
    assert "Update all outdated (2)" in body


# --------------------------------------------------------------------------- #
# update-outdated action: scope, audit, dedupe
# --------------------------------------------------------------------------- #

def test_update_outdated_queues_only_outdated(db):
    site = _seed_site(db)
    old = _add_agent(db, site, "old", "0.1.0+20250101-000000")
    cur = _add_agent(db, site, "cur", f"{TARGET}+20260601-000000")
    unk = _add_agent(db, site, "unk", None)
    _set_real_pip_source(db)
    cli = _login_admin(db)
    resp = cli.post("/manage/agents/update-outdated", follow_redirects=False)
    assert resp.status_code in (302, 303)

    queued = _pending_update_agent_ids(db)
    assert queued == {old.id}
    assert cur.id not in queued
    assert unk.id not in queued

    # Audited.
    audit = db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "agent.update_outdated")
    ).first()
    assert audit is not None
    assert "queued=1" in (audit.detail or "")


def test_legacy_update_all_path_is_also_outdated_only(db):
    """The old /update-all path now aliases the outdated-only action so any
    bookmarked form keeps working without mass-updating current agents."""
    site = _seed_site(db)
    old = _add_agent(db, site, "old", "0.1.0+x")
    cur = _add_agent(db, site, "cur", TARGET)
    _set_real_pip_source(db)
    cli = _login_admin(db)
    cli.post("/manage/agents/update-all", follow_redirects=False)
    assert _pending_update_agent_ids(db) == {old.id}
    assert cur.id not in _pending_update_agent_ids(db)


def test_update_outdated_skips_already_pending(db):
    site = _seed_site(db)
    old = _add_agent(db, site, "old", "0.1.0+x")
    other = _add_agent(db, site, "old2", "0.2.0+x")
    _set_real_pip_source(db)
    # Pre-queue a pending update for `old` only.
    db.add(m.Command(
        agent_id=old.id, type=m.CommandType.update_agent,
        payload={"pip_source": "x"}, status=m.CommandStatus.pending,
    ))
    db.commit()
    cli = _login_admin(db)
    cli.post("/manage/agents/update-outdated", follow_redirects=False)

    # `old` still has exactly ONE pending update (not double-queued); `other`
    # got its first.
    old_count = db.scalar(
        select(func.count()).select_from(m.Command).where(
            m.Command.agent_id == old.id,
            m.Command.type == m.CommandType.update_agent,
            m.Command.status == m.CommandStatus.pending,
        )
    )
    assert old_count == 1
    other_count = db.scalar(
        select(func.count()).select_from(m.Command).where(
            m.Command.agent_id == other.id,
            m.Command.type == m.CommandType.update_agent,
        )
    )
    assert other_count == 1
    audit = db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "agent.update_outdated")
    ).first()
    assert "skipped_pending=1" in (audit.detail or "")


def test_update_outdated_admin_only(db):
    site = _seed_site(db)
    _add_agent(db, site, "old", "0.1.0+x")
    _set_real_pip_source(db)
    cli = _login_tech(db)
    cli.post("/manage/agents/update-outdated", follow_redirects=False)
    # Tech is not admin -> nothing queued.
    assert _pending_update_agent_ids(db) == set()


def test_update_outdated_blocks_on_placeholder_pip_source(db):
    site = _seed_site(db)
    _add_agent(db, site, "old", "0.1.0+x")
    db.add(m.AppSetting(
        key="agent.pip_source",
        value="git+https://github.com/your-org/printer-nanny.git#subdirectory=agent",
    ))
    db.commit()
    cli = _login_admin(db)
    cli.post("/manage/agents/update-outdated", follow_redirects=False)
    # Placeholder source -> refuse to queue anything.
    assert _pending_update_agent_ids(db) == set()
