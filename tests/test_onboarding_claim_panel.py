"""The minted code must actually reach the operator's screen, once."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central.main import app
from central.security import hash_password


def _admin(db):
    u = m.User(username="admin", password_hash=hash_password("pw12345678"),
               role=m.UserRole.admin)
    db.add(u)
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    db.add(m.Site(client_id=client.id, name="HQ"))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw12345678"},
             follow_redirects=False)
    return cli


def test_minted_claim_code_is_shown_once_on_the_agents_page(db):
    cli = _admin(db)
    site = db.scalar(select(m.Site))
    cli.post("/manage/agents/claim-code",
             data={"site_id": site.id, "name": "HQ agent", "ttl_minutes": 60},
             follow_redirects=False)
    body = cli.get("/manage/agents").text
    assert "Claim code for" in body
    assert "--claim-code pnc_" in body, "install command missing the code"
    assert "PN_CLAIM_CODE" in body, "windows command missing the code"

    # Shown ONCE: a reload must not re-surface a live credential.
    again = cli.get("/manage/agents").text
    assert "Claim code for" not in again
    assert "pnc_" not in again


def test_mint_form_is_on_the_page(db):
    cli = _admin(db)
    body = cli.get("/manage/agents").text
    assert 'action="/manage/agents/claim-code"' in body
