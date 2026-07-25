"""The one-screen onboarding flow, and the subnet adoption it depends on.

With claim-code enrollment the agent does not exist when the subnet is declared,
so the wizard writes a subnet with no agent and redemption adopts it. That
handoff is the part worth testing: get it wrong and the operator's agent comes
online with nothing to scan.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import services
from central.main import app
from central.security import hash_password


def _mk_user(db, username: str, role: m.UserRole, password: str = "pw12345678") -> m.User:
    user = m.User(username=username, password_hash=hash_password(password), role=role)
    db.add(user)
    db.commit()
    return user


def _login(username: str, password: str = "pw12345678") -> TestClient:
    cli = TestClient(app)
    cli.post("/login", data={"username": username, "password": password},
             follow_redirects=False)
    return cli


def _submit(cli, **overrides):
    data = {
        "client_name": "Acme Ltd",
        "site_name": "Head office",
        "cidr": "10.10.0.0/24",
        "snmp_community": "public",
        "snmp_version": "2c",
        "ttl_minutes": 1440,
    }
    data.update(overrides)
    return cli.post("/manage/onboard", data=data, follow_redirects=False)


# ---------- the flow ----------

def test_one_submit_creates_the_whole_customer(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _submit(_login("admin"))
    db.expire_all()

    client = db.scalar(select(m.Client).where(m.Client.name == "Acme Ltd"))
    assert client is not None
    site = db.scalar(select(m.Site).where(m.Site.client_id == client.id))
    assert site is not None and site.name == "Head office"
    subnet = db.scalar(select(m.Subnet).where(m.Subnet.site_id == site.id))
    assert subnet is not None and subnet.cidr == "10.10.0.0/24"
    # No agent yet -- that is the point; it arrives when the code is redeemed.
    assert subnet.agent_id is None
    token = db.scalar(select(m.AgentClaimToken).where(m.AgentClaimToken.site_id == site.id))
    assert token is not None and token.used_at is None


def test_an_existing_client_is_reused_not_duplicated(db):
    _mk_user(db, "admin", m.UserRole.admin)
    db.add(m.Client(name="Acme Ltd"))
    db.commit()
    _submit(_login("admin"), site_name="Second site")
    db.expire_all()
    assert len(list(db.scalars(select(m.Client).where(m.Client.name == "Acme Ltd")))) == 1
    assert len(list(db.scalars(select(m.Site)))) == 1


def test_an_existing_timezone_is_never_overwritten(db):
    _mk_user(db, "admin", m.UserRole.admin)
    db.add(m.Client(name="Acme Ltd", timezone="America/New_York"))
    db.commit()
    _submit(_login("admin"), client_timezone="Europe/London")
    db.expire_all()
    client = db.scalar(select(m.Client).where(m.Client.name == "Acme Ltd"))
    assert client.timezone == "America/New_York"


def test_timezone_is_set_when_the_client_has_none(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _submit(_login("admin"), client_timezone="Europe/London")
    db.expire_all()
    client = db.scalar(select(m.Client).where(m.Client.name == "Acme Ltd"))
    assert client.timezone == "Europe/London"


def test_an_unresolvable_timezone_is_rejected_before_anything_is_created(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _submit(_login("admin"), client_timezone="Mars/Olympus_Mons")
    db.expire_all()
    assert not list(db.scalars(select(m.Client)))
    assert not list(db.scalars(select(m.AgentClaimToken)))


def test_missing_names_create_nothing(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _submit(_login("admin"), client_name="   ")
    db.expire_all()
    assert not list(db.scalars(select(m.Client)))


def test_the_subnet_is_optional(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _submit(_login("admin"), cidr="")
    db.expire_all()
    assert not list(db.scalars(select(m.Subnet)))
    assert list(db.scalars(select(m.AgentClaimToken)))


def test_trusted_carries_through_the_wizard(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _submit(_login("admin"), trusted="1")
    db.expire_all()
    assert db.scalar(select(m.Subnet)).trusted is True


def test_wizard_is_audited(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _submit(_login("admin"))
    actions = {a.action for a in db.scalars(select(m.AuditLog))}
    assert {"client.create", "site.create", "subnet.create",
            "agent.claim_code_mint"} <= actions


def test_client_readonly_cannot_onboard(db):
    _mk_user(db, "viewer", m.UserRole.client_readonly)
    _submit(_login("viewer"))
    db.expire_all()
    assert not list(db.scalars(select(m.Client)))


def test_anonymous_cannot_onboard(db):
    _submit(TestClient(app))
    db.expire_all()
    assert not list(db.scalars(select(m.Client)))


def test_the_form_renders(db):
    _mk_user(db, "admin", m.UserRole.admin)
    body = _login("admin").get("/manage/onboard").text
    assert 'action="/manage/onboard"' in body
    assert 'name="client_name"' in body and 'name="cidr"' in body


# ---------- the handoff to enrollment ----------

def test_enrolling_adopts_the_sites_orphan_subnets(db):
    _mk_user(db, "admin", m.UserRole.admin)
    cli = _login("admin")
    _submit(cli)
    db.expire_all()
    site = db.scalar(select(m.Site))

    # Mint a fresh code (the wizard's was shown once and isn't recoverable here).
    _, code = services.mint_claim_token(db, site_id=site.id, agent_name="a")
    db.commit()
    resp = TestClient(app).post("/api/v1/agents/register", json={"claim_code": code})
    assert resp.status_code == 200

    db.expire_all()
    subnet = db.scalar(select(m.Subnet))
    assert subnet.agent_id == resp.json()["agent_id"]


def test_adoption_never_steals_another_agents_subnet(db):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    incumbent = m.Agent(site_id=site.id, name="incumbent", api_key_hash="x" * 64)
    db.add(incumbent)
    db.flush()
    owned = m.Subnet(site_id=site.id, agent_id=incumbent.id, cidr="10.0.1.0/24")
    orphan = m.Subnet(site_id=site.id, agent_id=None, cidr="10.0.2.0/24")
    db.add_all([owned, orphan])
    db.commit()

    _, code = services.mint_claim_token(db, site_id=site.id, agent_name="newcomer")
    db.commit()
    resp = TestClient(app).post("/api/v1/agents/register", json={"claim_code": code})
    newcomer_id = resp.json()["agent_id"]

    db.expire_all()
    assert db.get(m.Subnet, owned.id).agent_id == incumbent.id
    assert db.get(m.Subnet, orphan.id).agent_id == newcomer_id


def test_adoption_stays_inside_the_site(db):
    """An orphan subnet at another site must not be swept up."""
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    mine = m.Site(client_id=client.id, name="HQ")
    other = m.Site(client_id=client.id, name="Branch")
    db.add_all([mine, other])
    db.flush()
    elsewhere = m.Subnet(site_id=other.id, agent_id=None, cidr="10.9.9.0/24")
    db.add(elsewhere)
    db.commit()

    _, code = services.mint_claim_token(db, site_id=mine.id, agent_name="a")
    db.commit()
    TestClient(app).post("/api/v1/agents/register", json={"claim_code": code})

    db.expire_all()
    assert db.get(m.Subnet, elsewhere.id).agent_id is None


def test_adoption_is_audited(db):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    db.add(m.Subnet(site_id=site.id, agent_id=None, cidr="10.0.1.0/24"))
    db.commit()

    _, code = services.mint_claim_token(db, site_id=site.id, agent_name="a")
    db.commit()
    TestClient(app).post("/api/v1/agents/register", json={"claim_code": code})

    rows = list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "subnet.auto_assign")
    ))
    assert len(rows) == 1 and "10.0.1.0/24" in rows[0].detail
