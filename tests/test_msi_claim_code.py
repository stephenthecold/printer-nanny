"""MSI builds that carry a claim code instead of a permanent key.

The gap this closes: an .msi gets staged on shares, attached to tickets and
carried on USB sticks, and the pre-minted variant baked in an API key that stays
valid for the life of the agent. A claim code is spent by the first machine that
installs it, so a stale copy is inert.

The round-trip matters as much as the rendering -- a config.toml that central
emits but the agent can't parse is a silently un-startable service, and the
failure would only show up on a customer's server.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import services
from central.main import app
from central.msi_builder import render_config_toml
from central.security import hash_password
from printer_nanny_agent import config as agent_config


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


def _site(db):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.commit()
    return site


# ---------- rendering ----------

def test_claim_mode_emits_a_code_and_no_key(db):
    out = render_config_toml(central_url="https://c/", claim_code="pnc_abc")
    assert 'claim_code = "pnc_abc"' in out
    assert "api_key" not in out and "agent_id" not in out


def test_key_mode_is_unchanged(db):
    out = render_config_toml(central_url="https://c/", agent_id=7, api_key="pn_k")
    assert "agent_id = 7" in out and 'api_key = "pn_k"' in out
    assert "claim_code" not in out


def test_exactly_one_enrollment_shape_is_required(db):
    with pytest.raises(ValueError):
        render_config_toml(central_url="https://c")
    with pytest.raises(ValueError):
        render_config_toml(central_url="https://c", agent_id=1, api_key="k",
                           claim_code="pnc_x")
    with pytest.raises(ValueError):
        # Half a key pair is not a key pair.
        render_config_toml(central_url="https://c", agent_id=1)


def test_a_hostile_claim_code_cannot_break_out_of_the_toml_string(db):
    """The value is escaped, not trusted -- an unescaped quote would emit
    invalid TOML and a service that never starts."""
    nasty = 'pnc_a"b\\c\nd'
    out = render_config_toml(central_url="https://c", claim_code=nasty)
    assert '\\"' in out and "\\\\" in out and "\\n" in out


# ---------- the round-trip the agent actually performs ----------

def test_rendered_claim_config_parses_and_enrolls(db, tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(render_config_toml(central_url="https://c", claim_code="pnc_go"))

    calls = {"n": 0}

    class _Resp:
        def read(self):
            return b'{"agent_id": 42, "api_key": "pn_issued"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        calls["n"] += 1
        return _Resp()

    from printer_nanny_agent import enrollment

    monkeypatch.setattr(enrollment.urlrequest, "urlopen", fake_urlopen)
    cfg = agent_config.load_config(str(path))
    assert (cfg.agent_id, cfg.api_key) == (42, "pn_issued")
    assert calls["n"] == 1
    # And the spent code is gone from disk.
    assert "claim_code" not in path.read_text()


def test_rendered_key_config_still_parses(db, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(render_config_toml(central_url="https://c", agent_id=7, api_key="pn_k"))
    cfg = agent_config.load_config(str(path))
    assert (cfg.agent_id, cfg.api_key) == (7, "pn_k")


def test_a_hostile_code_survives_the_round_trip_intact(db, tmp_path):
    """Escaping has to be reversible, not just safe."""
    nasty = 'pnc_a"b\\c'
    path = tmp_path / "config.toml"
    path.write_text(render_config_toml(central_url="https://c", claim_code=nasty))
    raw = agent_config._load(open(path, "rb"))
    assert raw["claim_code"] == nasty


# ---------- the route ----------

def test_route_refuses_a_spent_code_without_building(db):
    _mk_user(db, "admin", m.UserRole.admin)
    site = _site(db)
    token, code = services.mint_claim_token(db, site_id=site.id, agent_name="a")
    db.commit()
    TestClient(app).post("/api/v1/agents/register", json={"claim_code": code})

    cli = _login("admin")
    resp = cli.post("/manage/agents/claim-msi",
                    data={"claim_code": code, "site_label": "Acme"},
                    follow_redirects=False)
    assert resp.status_code == 303
    builds = [a for a in db.scalars(select(m.AuditLog))
              if a.action == "agent.msi_build"]
    assert not builds, "a spent code should be rejected before any build work"


def test_route_refuses_an_unknown_code(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _site(db)
    resp = _login("admin").post("/manage/agents/claim-msi",
                                data={"claim_code": "pnc_never", "site_label": "x"},
                                follow_redirects=False)
    assert resp.status_code == 303


def test_route_requires_a_manager(db):
    _mk_user(db, "viewer", m.UserRole.client_readonly)
    site = _site(db)
    _, code = services.mint_claim_token(db, site_id=site.id, agent_name="a")
    db.commit()
    resp = _login("viewer").post("/manage/agents/claim-msi",
                                 data={"claim_code": code, "site_label": "x"},
                                 follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert "/login" in resp.headers.get("location", "")


def test_the_claim_code_is_never_audited(db):
    """Whether the build succeeds or the toolchain is missing, the code is a
    live credential until redeemed and must not land in the trail."""
    _mk_user(db, "admin", m.UserRole.admin)
    site = _site(db)
    _, code = services.mint_claim_token(db, site_id=site.id, agent_name="a")
    db.commit()
    _login("admin").post("/manage/agents/claim-msi",
                         data={"claim_code": code, "site_label": "Acme"},
                         follow_redirects=False)
    for row in db.scalars(select(m.AuditLog)):
        assert code not in ((row.detail or "") + (row.target or ""))


def test_the_claim_panel_offers_the_msi(db, monkeypatch):
    """Forced available: whether wixl is installed in the test image is not what
    this asserts, and letting it decide would make the test silently vacuous."""
    import central.msi_builder as msi

    monkeypatch.setattr(
        msi, "msi_build_available",
        lambda: msi.MsiCapability(available=True, reason="ok", wixl="/usr/bin/wixl",
                                  msiinfo="/usr/bin/msiinfo"),
    )
    _mk_user(db, "admin", m.UserRole.admin)
    site = _site(db)
    cli = _login("admin")
    cli.post("/manage/agents/claim-code",
             data={"site_id": site.id, "name": "HQ agent", "ttl_minutes": 60},
             follow_redirects=False)
    body = cli.get("/manage/agents").text
    assert 'action="/manage/agents/claim-msi"' in body
    assert "Enrolls one machine" in body


def test_the_claim_panel_says_so_when_the_builder_is_missing(db, monkeypatch):
    import central.msi_builder as msi

    monkeypatch.setattr(
        msi, "msi_build_available",
        lambda: msi.MsiCapability(available=False, reason="wixl missing"),
    )
    _mk_user(db, "admin", m.UserRole.admin)
    site = _site(db)
    cli = _login("admin")
    cli.post("/manage/agents/claim-code",
             data={"site_id": site.id, "name": "HQ agent", "ttl_minutes": 60},
             follow_redirects=False)
    body = cli.get("/manage/agents").text
    assert "wixl missing" in body
    assert 'action="/manage/agents/claim-msi"' not in body
