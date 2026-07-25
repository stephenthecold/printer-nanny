"""Claim codes: the credential that travels is the one that expires.

The properties worth testing are the ones a caller would try to break: reuse,
expiry, choosing your own tenant, and learning from the error message whether a
code exists.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import services
from central.main import app
from central.security import hash_api_key, hash_claim_code


def _site(db, client_name: str = "Acme", site_name: str = "HQ"):
    client = m.Client(name=client_name)
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name=site_name)
    db.add(site)
    db.commit()
    return site


def _mint(db, site, name: str = "Site agent", ttl_minutes: int = 60):
    token, code = services.mint_claim_token(
        db, site_id=site.id, agent_name=name, ttl_minutes=ttl_minutes
    )
    db.commit()
    return token, code


# ---------- storage discipline ----------

def test_the_plaintext_code_is_never_stored(db):
    site = _site(db)
    token, code = _mint(db, site)
    db.expire_all()
    stored = db.get(m.AgentClaimToken, token.id)
    assert stored.token_hash == hash_claim_code(code)
    assert code not in stored.token_hash
    # Nothing anywhere in the row should carry the plaintext.
    assert code not in " ".join(
        str(getattr(stored, col.name)) for col in stored.__table__.columns
    )


def test_minted_codes_are_unique_and_high_entropy(db):
    site = _site(db)
    codes = {_mint(db, site)[1] for _ in range(25)}
    assert len(codes) == 25
    assert all(c.startswith("pnc_") and len(c) > 40 for c in codes)


# ---------- redemption ----------

def test_redeeming_mints_a_working_agent(db):
    site = _site(db)
    _, code = _mint(db, site, name="Acme HQ agent")
    cli = TestClient(app)
    resp = cli.post("/api/v1/agents/register",
                    json={"claim_code": code, "hostname": "printsrv01"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["site_id"] == site.id
    assert "Acme HQ agent" in body["name"]
    assert "printsrv01" in body["name"]

    db.expire_all()
    agent = db.get(m.Agent, body["agent_id"])
    assert agent is not None
    # The returned key must actually authenticate as that agent.
    assert agent.api_key_hash == hash_api_key(body["api_key"])
    resp2 = cli.post(
        f"/api/v1/agents/{agent.id}/heartbeat",
        json={"version": "0.7.0"},
        headers={"Authorization": f"Bearer {body['api_key']}"},
    )
    assert resp2.status_code == 200


def test_a_code_works_exactly_once(db):
    site = _site(db)
    _, code = _mint(db, site)
    cli = TestClient(app)
    first = cli.post("/api/v1/agents/register", json={"claim_code": code})
    second = cli.post("/api/v1/agents/register", json={"claim_code": code})
    assert first.status_code == 200
    assert second.status_code == 401
    db.expire_all()
    assert len(list(db.scalars(select(m.Agent)))) == 1


def test_an_expired_code_is_refused(db):
    site = _site(db)
    token, code = _mint(db, site)
    token.expires_at = services._now() - timedelta(minutes=1)
    db.commit()
    resp = TestClient(app).post("/api/v1/agents/register", json={"claim_code": code})
    assert resp.status_code == 401
    db.expire_all()
    assert not list(db.scalars(select(m.Agent)))


def test_an_expired_code_is_not_burned_by_the_attempt(db):
    """Failing on expiry must not mark the row used -- 'used' and 'expired' are
    different states and conflating them would misreport what happened."""
    site = _site(db)
    token, code = _mint(db, site)
    token.expires_at = services._now() - timedelta(minutes=1)
    db.commit()
    TestClient(app).post("/api/v1/agents/register", json={"claim_code": code})
    db.expire_all()
    assert db.get(m.AgentClaimToken, token.id).used_at is None


def test_an_unknown_code_is_refused(db):
    _site(db)
    resp = TestClient(app).post("/api/v1/agents/register",
                                json={"claim_code": "pnc_not-a-real-code"})
    assert resp.status_code == 401


def test_failures_are_indistinguishable(db):
    """Unknown / expired / spent must not be tellable apart from the response."""
    site = _site(db)
    token, spent = _mint(db, site)
    TestClient(app).post("/api/v1/agents/register", json={"claim_code": spent})
    expired_token, expired = _mint(db, site)
    expired_token.expires_at = services._now() - timedelta(minutes=1)
    db.commit()

    cli = TestClient(app)
    bodies, codes = set(), set()
    for code in (spent, expired, "pnc_never-existed"):
        resp = cli.post("/api/v1/agents/register", json={"claim_code": code})
        codes.add(resp.status_code)
        bodies.add(resp.text)
    assert codes == {401}
    assert len(bodies) == 1


def test_the_caller_cannot_choose_its_tenant(db):
    """site_id comes from the token, never the request body."""
    victim = _site(db, "Victim", "Vault")
    mine = _site(db, "Mine", "Branch")
    _, code = _mint(db, mine)
    resp = TestClient(app).post(
        "/api/v1/agents/register",
        json={"claim_code": code, "site_id": victim.id, "hostname": "x"},
    )
    assert resp.status_code == 200
    assert resp.json()["site_id"] == mine.id


def test_a_hostile_hostname_cannot_take_over_the_name(db):
    site = _site(db)
    _, code = _mint(db, site, name="Real agent")
    resp = TestClient(app).post("/api/v1/agents/register", json={
        "claim_code": code, "hostname": "x" * 500,
    })
    name = resp.json()["name"]
    assert name.startswith("Real agent")
    assert len(name) <= 200


def test_redemption_is_audited_without_the_code(db):
    site = _site(db)
    _, code = _mint(db, site)
    TestClient(app).post("/api/v1/agents/register", json={"claim_code": code})
    rows = list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "agent.register")
    ))
    assert len(rows) == 1
    assert code not in (rows[0].detail or "") + (rows[0].target or "")


def test_failed_redemption_is_audited(db):
    _site(db)
    TestClient(app).post("/api/v1/agents/register", json={"claim_code": "pnc_nope"})
    rows = list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "agent.register_failed")
    ))
    assert len(rows) == 1
    assert "pnc_nope" not in (rows[0].detail or "")


def test_token_records_which_agent_claimed_it(db):
    site = _site(db)
    token, code = _mint(db, site)
    resp = TestClient(app).post("/api/v1/agents/register", json={"claim_code": code})
    db.expire_all()
    stored = db.get(m.AgentClaimToken, token.id)
    assert stored.used_at is not None
    assert stored.used_by_agent_id == resp.json()["agent_id"]


def test_deleted_site_fails_closed(db):
    site = _site(db)
    token, code = _mint(db, site)
    db.delete(site)
    db.commit()
    resp = TestClient(app).post("/api/v1/agents/register", json={"claim_code": code})
    assert resp.status_code == 401


def test_ttl_is_clamped(db):
    site = _site(db)
    token, _ = services.mint_claim_token(
        db, site_id=site.id, agent_name="a", ttl_minutes=10 ** 9
    )
    db.commit()
    horizon = services._now() + timedelta(minutes=services.CLAIM_CODE_MAX_TTL_MINUTES)
    assert token.expires_at <= horizon + timedelta(seconds=5)


def test_purge_removes_only_long_dead_tokens(db):
    site = _site(db)
    live, _ = _mint(db, site)
    old, _ = _mint(db, site)
    old.expires_at = services._now() - timedelta(days=30)
    db.commit()
    removed = services.purge_expired_claim_tokens(db)
    db.commit()
    db.expire_all()
    assert removed == 1
    assert db.get(m.AgentClaimToken, live.id) is not None
    assert db.get(m.AgentClaimToken, old.id) is None
