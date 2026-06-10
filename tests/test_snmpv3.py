"""SNMPv3 end-to-end: model column, UI forms, agent config wire format, params.

Covers the chain: operator enters USM creds on /manage/agents -> Subnet.snmp_v3
JSON -> /api/v1/agents/{id}/config response -> agent merges into SubnetConfig
-> SnmpParams.snmp_for() builds a UsmUserData on first poll.

The pysnmp UsmUserData construction itself is exercised indirectly via the
mapping helper -- we don't import pysnmp here to keep tests fast and portable.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from central import models as m
from central.main import app
from central.security import generate_api_key, hash_api_key, hash_password
from printer_nanny_agent.config import SubnetConfig, AgentConfig
from printer_nanny_agent.snmp import SnmpParams


def _login(db, *, role: m.UserRole = m.UserRole.admin) -> TestClient:
    pwd = "pw" + role.value
    db.add(m.User(
        username=f"u_{role.value}",
        password_hash=hash_password(pwd),
        role=role,
    ))
    db.commit()
    cli = TestClient(app)
    resp = cli.post(
        "/login", data={"username": f"u_{role.value}", "password": pwd},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return cli


def _seed_client_site_agent(db) -> tuple[m.Client, m.Site, m.Agent, str]:
    acme = m.Client(name="Acme")
    db.add(acme)
    db.flush()
    site = m.Site(client_id=acme.id, name="HQ")
    db.add(site)
    db.flush()
    api_key = generate_api_key()
    agent = m.Agent(
        site_id=site.id, name="hq-agent",
        api_key_hash=hash_api_key(api_key),
    )
    db.add(agent)
    db.commit()
    return acme, site, agent, api_key


# ---------- Schema / model ----------

def test_subnet_snmp_v3_column_round_trips(db):
    _, site, agent, _ = _seed_client_site_agent(db)
    subnet = m.Subnet(
        site_id=site.id, agent_id=agent.id, cidr="10.0.0.0/24",
        snmp_version="3",
        snmp_v3={
            "user": "noc-readonly",
            "security_level": "authPriv",
            "auth_protocol": "SHA256",
            "auth_password": "secret-auth",
            "priv_protocol": "AES256",
            "priv_password": "secret-priv",
            "context_name": "",
        },
    )
    db.add(subnet)
    db.commit()
    db.refresh(subnet)
    assert subnet.snmp_v3["user"] == "noc-readonly"
    assert subnet.snmp_v3["security_level"] == "authPriv"
    assert subnet.snmp_v3["auth_protocol"] == "SHA256"


# ---------- Form -> blob -> DB ----------

def test_subnet_form_creates_v3_blob(db):
    _, site, agent, _ = _seed_client_site_agent(db)
    http = _login(db)
    resp = http.post(
        f"/manage/agents/{agent.id}/subnets",
        data={
            "cidr": "10.0.1.0/24",
            "snmp_community": "public",
            "snmp_version": "3",
            "bind_interface": "",
            "site_id": "",
            "snmp_v3_user": "noc-readonly",
            "snmp_v3_security_level": "authPriv",
            "snmp_v3_auth_protocol": "SHA256",
            "snmp_v3_auth_password": "auth-secret",
            "snmp_v3_priv_protocol": "AES128",
            "snmp_v3_priv_password": "priv-secret",
            "snmp_v3_context_name": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    sub = db.execute(
        __import__("sqlalchemy").select(m.Subnet).where(m.Subnet.cidr == "10.0.1.0/24")
    ).scalar_one()
    assert sub.snmp_version == "3"
    assert sub.snmp_v3 is not None
    assert sub.snmp_v3["user"] == "noc-readonly"
    assert sub.snmp_v3["security_level"] == "authPriv"
    assert sub.snmp_v3["auth_protocol"] == "SHA256"
    assert sub.snmp_v3["priv_protocol"] == "AES128"
    # USM passwords are encrypted at rest; they decrypt back to the form input.
    from central.secrets import decrypt_value, is_encrypted
    assert is_encrypted(sub.snmp_v3["auth_password"])
    assert is_encrypted(sub.snmp_v3["priv_password"])
    assert decrypt_value(sub.snmp_v3["auth_password"]) == "auth-secret"
    assert decrypt_value(sub.snmp_v3["priv_password"]) == "priv-secret"


def test_subnet_form_without_v3_user_leaves_blob_none(db):
    _, site, agent, _ = _seed_client_site_agent(db)
    http = _login(db)
    http.post(
        f"/manage/agents/{agent.id}/subnets",
        data={
            "cidr": "10.0.2.0/24",
            "snmp_community": "public",
            "snmp_version": "2c",
            # No v3 fields supplied at all.
        },
        follow_redirects=False,
    )
    sub = db.execute(
        __import__("sqlalchemy").select(m.Subnet).where(m.Subnet.cidr == "10.0.2.0/24")
    ).scalar_one()
    assert sub.snmp_v3 is None


def test_subnet_update_clear_flag_wipes_v3(db):
    _, site, agent, _ = _seed_client_site_agent(db)
    sub = m.Subnet(
        site_id=site.id, agent_id=agent.id, cidr="10.0.3.0/24",
        snmp_version="3",
        snmp_v3={"user": "old", "security_level": "authNoPriv"},
    )
    db.add(sub)
    db.commit()
    http = _login(db)
    http.post(
        f"/manage/subnets/{sub.id}",
        data={
            "label": "VLAN30",
            "snmp_community": "",
            "snmp_version": "2c",
            "snmp_v3_clear": "1",
        },
        follow_redirects=False,
    )
    db.refresh(sub)
    assert sub.snmp_v3 is None
    assert sub.snmp_version == "2c"
    assert sub.label == "VLAN30"


def test_subnet_update_preserves_v3_when_form_omits_v3_fields(db):
    """Inline label-rename form must not blow away v3 creds."""
    _, site, agent, _ = _seed_client_site_agent(db)
    original = {"user": "keep-me", "security_level": "authPriv",
                "auth_protocol": "SHA256", "auth_password": "x",
                "priv_protocol": "AES256", "priv_password": "y"}
    sub = m.Subnet(
        site_id=site.id, agent_id=agent.id, cidr="10.0.4.0/24",
        snmp_version="3",
        snmp_v3=dict(original),
    )
    db.add(sub)
    db.commit()
    http = _login(db)
    http.post(
        f"/manage/subnets/{sub.id}",
        data={"label": "VLAN40"},  # only label
        follow_redirects=False,
    )
    db.refresh(sub)
    assert sub.snmp_v3 == original  # untouched
    assert sub.label == "VLAN40"


# ---------- /config endpoint ships v3 to the agent ----------

def test_agent_config_endpoint_serializes_snmp_v3(db):
    _, site, agent, api_key = _seed_client_site_agent(db)
    db.add(m.Subnet(
        site_id=site.id, agent_id=agent.id, cidr="10.0.5.0/24",
        snmp_version="3",
        snmp_v3={
            "user": "noc-rw", "security_level": "authPriv",
            "auth_protocol": "SHA256", "auth_password": "ap",
            "priv_protocol": "AES128", "priv_password": "pp",
        },
    ))
    db.commit()
    cli = TestClient(app)
    resp = cli.get(
        f"/api/v1/agents/{agent.id}/config",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    sub = next(s for s in payload["subnets"] if s["cidr"] == "10.0.5.0/24")
    assert sub["snmp_version"] == "3"
    assert sub["snmp_v3"]["user"] == "noc-rw"
    assert sub["snmp_v3"]["security_level"] == "authPriv"
    assert sub["snmp_v3"]["auth_protocol"] == "SHA256"
    assert sub["snmp_v3"]["priv_protocol"] == "AES128"


# ---------- Agent-side SubnetConfig -> SnmpParams ----------

def test_agent_snmp_for_passes_v3_into_params():
    config = AgentConfig(
        central_url="http://x", agent_id=1, api_key="k",
    )
    sub = SubnetConfig(
        cidr="10.0.0.0/24",
        version="3",
        snmp_v3={
            "user": "noc-rw", "security_level": "authPriv",
            "auth_protocol": "SHA256", "auth_password": "ap",
            "priv_protocol": "AES128", "priv_password": "pp",
            "context_name": "engine-a",
        },
    )
    p = config.snmp_for(sub)
    assert isinstance(p, SnmpParams)
    assert p.version == "3"
    assert p.v3_user == "noc-rw"
    assert p.v3_security_level == "authPriv"
    assert p.v3_auth_protocol == "SHA256"
    assert p.v3_auth_password == "ap"
    assert p.v3_priv_protocol == "AES128"
    assert p.v3_priv_password == "pp"
    assert p.v3_context_name == "engine-a"


def test_agent_snmp_for_v2c_leaves_v3_fields_none():
    config = AgentConfig(central_url="http://x", agent_id=1, api_key="k")
    sub = SubnetConfig(cidr="10.0.0.0/24", version="2c", community="public")
    p = config.snmp_for(sub)
    assert p.version == "2c"
    assert p.v3_user is None
    assert p.v3_security_level is None


def test_merge_remote_picks_up_snmp_v3_from_central():
    """Central serializes snmp_v3 in /config; merge_remote must thread it through."""
    from printer_nanny_agent.config import merge_remote

    config = AgentConfig(central_url="http://x", agent_id=1, api_key="k")
    remote = {
        "poll_interval_seconds": 300,
        "discovery_interval_seconds": 3600,
        "heartbeat_interval_seconds": 60,
        "snmp": {"community": "public", "version": "2c", "timeout": 2, "retries": 1},
        "subnets": [
            {
                "cidr": "10.0.0.0/24",
                "snmp_community": "public",
                "snmp_version": "3",
                "bind_interface": None,
                "snmp_v3": {
                    "user": "noc-rw",
                    "security_level": "authPriv",
                    "auth_protocol": "SHA256",
                    "auth_password": "ap",
                    "priv_protocol": "AES128",
                    "priv_password": "pp",
                },
            },
        ],
    }
    merged = merge_remote(config, remote)
    assert len(merged.subnets) == 1
    sub = merged.subnets[0]
    assert sub.version == "3"
    assert sub.snmp_v3["user"] == "noc-rw"


def test_build_v3_auth_dispatches_on_security_level():
    """Map form values to pysnmp UsmUserData kwargs without importing pysnmp.

    We pass a stand-in factory so the helper's branching logic gets tested in
    isolation -- the real pysnmp protocol objects are looked up inside the
    helper at call time. Captures (positional args, kwargs) so we can assert
    exactly which fields got threaded through.
    """
    from printer_nanny_agent.snmp import _build_v3_auth

    captured = {}

    def fake_usm(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    # noAuthNoPriv: just the username.
    captured.clear()
    p = SnmpParams(version="3", v3_user="noc", v3_security_level="noAuthNoPriv")
    _build_v3_auth(p, fake_usm)
    assert captured["args"] == ("noc",)
    assert captured["kwargs"] == {}

    # Stand-in proto maps (would normally be pysnmp objects). Use a sentinel
    # so we can assert the right key was looked up.
    fake_maps = (
        {"SHA256": "AUTH_SHA256_OBJ", "SHA512": "AUTH_SHA512_OBJ", "": "AUTH_NONE"},
        {"AES256": "PRIV_AES256_OBJ", "": "PRIV_NONE"},
    )

    # authNoPriv: + authKey + authProtocol.
    captured.clear()
    p = SnmpParams(
        version="3", v3_user="noc", v3_security_level="authNoPriv",
        v3_auth_protocol="SHA256", v3_auth_password="ap",
    )
    _build_v3_auth(p, fake_usm, _maps=fake_maps)
    assert captured["args"] == ("noc",)
    assert captured["kwargs"].get("authKey") == "ap"
    assert captured["kwargs"].get("authProtocol") == "AUTH_SHA256_OBJ"
    assert "privKey" not in captured["kwargs"]

    # authPriv: + privKey + privProtocol.
    captured.clear()
    p = SnmpParams(
        version="3", v3_user="noc", v3_security_level="authPriv",
        v3_auth_protocol="SHA512", v3_auth_password="ap",
        v3_priv_protocol="AES256", v3_priv_password="pp",
    )
    _build_v3_auth(p, fake_usm, _maps=fake_maps)
    assert captured["args"] == ("noc",)
    assert captured["kwargs"].get("authKey") == "ap"
    assert captured["kwargs"].get("privKey") == "pp"
    assert captured["kwargs"].get("authProtocol") == "AUTH_SHA512_OBJ"
    assert captured["kwargs"].get("privProtocol") == "PRIV_AES256_OBJ"


def test_agent_snmpv3_blob_keys_align_with_central_form():
    """The central form key map and the agent-side blob key map must match.

    If we ever rename a key on one side but not the other, the blob silently
    drops the field. Pin the contract here so a future refactor breaks loudly.
    """
    EXPECTED = {
        "user", "security_level",
        "auth_protocol", "auth_password",
        "priv_protocol", "priv_password",
        "context_name",
    }
    # The central side builds this blob.
    from central.dashboard.manage import _build_v3_blob
    blob = _build_v3_blob(
        user="noc",
        security_level="authPriv",
        auth_protocol="SHA256",
        auth_password="x",
        priv_protocol="AES128",
        priv_password="y",
        context_name="ctx",
    )
    assert set(blob.keys()) <= EXPECTED
    assert blob["user"] == "noc"
    assert blob["context_name"] == "ctx"


# ---------- Force-action commands ----------

def test_force_poll_now_enqueues_command(db):
    _, _site, agent, _ = _seed_client_site_agent(db)
    http = _login(db)
    resp = http.post(f"/manage/agents/{agent.id}/poll-now", follow_redirects=False)
    assert resp.status_code == 303
    cmd = db.execute(
        __import__("sqlalchemy").select(m.Command).where(m.Command.agent_id == agent.id)
    ).scalars().all()
    assert len(cmd) == 1
    assert cmd[0].type == m.CommandType.poll_now
    assert cmd[0].status == m.CommandStatus.pending


def test_force_rescan_enqueues_command(db):
    _, _site, agent, _ = _seed_client_site_agent(db)
    http = _login(db)
    resp = http.post(f"/manage/agents/{agent.id}/rescan", follow_redirects=False)
    assert resp.status_code == 303
    cmd = db.execute(
        __import__("sqlalchemy").select(m.Command).where(m.Command.agent_id == agent.id)
    ).scalars().one()
    assert cmd.type == m.CommandType.rescan
    assert cmd.status == m.CommandStatus.pending


# ---------- Cross-site subnet creation (multi-client agent) ----------

def test_subnet_can_be_created_at_a_different_site(db):
    """The HQ-multi-client pattern: one agent at site A collects for site B."""
    # Acme/HQ with the agent
    _, hq, agent, _ = _seed_client_site_agent(db)
    # Beta/HQ -- a different client/site
    beta = m.Client(name="Beta")
    db.add(beta)
    db.flush()
    beta_hq = m.Site(client_id=beta.id, name="HQ")
    db.add(beta_hq)
    db.commit()

    http = _login(db)
    resp = http.post(
        f"/manage/agents/{agent.id}/subnets",
        data={
            "cidr": "172.16.0.0/24",
            "snmp_community": "public",
            "snmp_version": "2c",
            "site_id": str(beta_hq.id),  # cross-client assignment
            "bind_interface": "10.10.10.5",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    sub = db.execute(
        __import__("sqlalchemy").select(m.Subnet).where(m.Subnet.cidr == "172.16.0.0/24")
    ).scalar_one()
    # The subnet was assigned to Beta/HQ even though the agent's home is Acme/HQ.
    assert sub.site_id == beta_hq.id
    assert sub.agent_id == agent.id
    assert sub.bind_interface == "10.10.10.5"
