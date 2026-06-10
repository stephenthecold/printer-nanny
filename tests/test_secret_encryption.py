"""Encryption-at-rest for stored credentials (design doc §846).

Coverage: the crypto helpers, the settings load/save round trip, the lazy
plaintext migration (save sweep + startup helper), SNMPv3 USM password
encryption through the subnet form, and decryption on the agent-config
endpoint -- the one place plaintext legitimately leaves the database.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import runtime
from central.main import app
from central.secrets import ENC_PREFIX, decrypt_value, encrypt_value, is_encrypted
from central.security import generate_api_key, hash_api_key, hash_password


def _login_admin(db) -> TestClient:
    db.add(m.User(
        username="admin", password_hash=hash_password("pw"), role=m.UserRole.admin,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"},
             follow_redirects=False)
    return cli


# ---------- crypto helpers ----------

def test_encrypt_decrypt_round_trip():
    token = encrypt_value("hunter2-smtp-password")
    assert token.startswith(ENC_PREFIX)
    assert "hunter2" not in token  # ciphertext doesn't leak plaintext
    assert decrypt_value(token) == "hunter2-smtp-password"


def test_empty_value_stays_empty():
    assert encrypt_value("") == ""
    assert decrypt_value("") == ""


def test_plaintext_passes_through_decrypt():
    """Legacy rows (pre-encryption) must keep working unchanged."""
    assert decrypt_value("legacy-plaintext-key") == "legacy-plaintext-key"
    assert decrypt_value(None) is None
    assert decrypt_value(12345) == 12345


def test_tampered_token_degrades_to_unset():
    """A corrupted token (or a SECRET_KEY rotation) must not crash settings
    loading -- the secret reads back as '' (unset) with a logged warning."""
    token = encrypt_value("secret")
    tampered = token[:-4] + "AAAA"
    assert decrypt_value(tampered) == ""


def test_is_encrypted_detection():
    assert is_encrypted(encrypt_value("x")) is True
    assert is_encrypted("plaintext") is False
    assert is_encrypted(None) is False
    assert is_encrypted({"$enc": "nope"}) is False


# ---------- settings round trip ----------

def test_save_settings_stores_secret_encrypted(db):
    runtime.save_settings(db, {"smtp.password": "mail-secret-42"})
    row = db.scalar(select(m.AppSetting).where(m.AppSetting.key == "smtp.password"))
    assert is_encrypted(row.value)
    assert "mail-secret-42" not in str(row.value)
    # ...and loads back as plaintext for the app to use.
    values = runtime.load_settings(db)
    assert values["smtp.password"] == "mail-secret-42"


def test_save_settings_leaves_non_secrets_plaintext(db):
    runtime.save_settings(db, {"smtp.host": "mail.example.com"})
    row = db.scalar(select(m.AppSetting).where(m.AppSetting.key == "smtp.host"))
    assert row.value == "mail.example.com"  # str specs stay readable in the DB


def test_legacy_plaintext_secret_loads_and_gets_swept_on_save(db):
    # Simulate a pre-encryption deployment: plaintext secret row in the DB.
    db.add(m.AppSetting(key="freescout.api_key", value="legacy-key-123"))
    db.commit()
    # Loads fine before any migration.
    assert runtime.load_settings(db)["freescout.api_key"] == "legacy-key-123"
    # Any settings save sweeps it into encrypted form...
    runtime.save_settings(db, {"smtp.host": "mail.example.com"})
    row = db.scalar(select(m.AppSetting).where(m.AppSetting.key == "freescout.api_key"))
    assert is_encrypted(row.value)
    # ...without changing what the app reads.
    assert runtime.load_settings(db)["freescout.api_key"] == "legacy-key-123"


def test_encrypt_existing_settings_startup_sweep(db):
    db.add(m.AppSetting(key="smtp.password", value="plain-1"))
    db.add(m.AppSetting(key="slack.webhook_url", value="https://hooks.slack.com/x"))
    db.add(m.AppSetting(key="smtp.host", value="mail.example.com"))  # not a secret
    db.commit()
    updated = runtime.encrypt_existing_settings(db)
    assert updated == 2
    for key in ("smtp.password", "slack.webhook_url"):
        row = db.scalar(select(m.AppSetting).where(m.AppSetting.key == key))
        assert is_encrypted(row.value)
    host_row = db.scalar(select(m.AppSetting).where(m.AppSetting.key == "smtp.host"))
    assert host_row.value == "mail.example.com"
    # Idempotent: second run touches nothing.
    assert runtime.encrypt_existing_settings(db) == 0


def test_masked_for_form_still_masks_decrypted_secrets(db):
    runtime.save_settings(db, {"smtp.password": "mail-secret"})
    values = runtime.load_settings(db)
    masked = runtime.masked_for_form(values)
    assert masked["smtp.password"] == runtime.SECRET_PLACEHOLDER


def test_settings_page_never_echoes_secret_or_ciphertext(db):
    runtime.save_settings(db, {"smtp.password": "super-secret-pw"})
    cli = _login_admin(db)
    resp = cli.get("/settings", follow_redirects=False)
    assert resp.status_code == 200
    assert "super-secret-pw" not in resp.text
    assert ENC_PREFIX not in resp.text


# ---------- SNMPv3 USM passwords ----------

def _seed_site_agent(db):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    api_key = generate_api_key()
    agent = m.Agent(site_id=site.id, name="hq", api_key_hash=hash_api_key(api_key))
    db.add(agent)
    db.commit()
    return site, agent, api_key


def test_subnet_form_stores_v3_passwords_encrypted(db):
    _site, agent, _key = _seed_site_agent(db)
    cli = _login_admin(db)
    resp = cli.post(
        f"/manage/agents/{agent.id}/subnets",
        data={
            "cidr": "10.0.9.0/24",
            "snmp_community": "public",
            "snmp_version": "3",
            "snmp_v3_user": "noc-ro",
            "snmp_v3_security_level": "authPriv",
            "snmp_v3_auth_protocol": "SHA256",
            "snmp_v3_auth_password": "auth-secret-xyz",
            "snmp_v3_priv_protocol": "AES128",
            "snmp_v3_priv_password": "priv-secret-xyz",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    sub = db.scalar(select(m.Subnet).where(m.Subnet.cidr == "10.0.9.0/24"))
    assert is_encrypted(sub.snmp_v3["auth_password"])
    assert is_encrypted(sub.snmp_v3["priv_password"])
    assert "auth-secret-xyz" not in str(sub.snmp_v3)
    # Non-secret fields stay readable for the UI.
    assert sub.snmp_v3["user"] == "noc-ro"
    assert sub.snmp_v3["auth_protocol"] == "SHA256"


def test_agent_config_delivers_decrypted_v3_passwords(db):
    """The agent must receive working USM creds -- decryption happens exactly
    here, on the authenticated agent-config endpoint."""
    site, agent, api_key = _seed_site_agent(db)
    from central.secrets import encrypt_value as enc
    db.add(m.Subnet(
        site_id=site.id, agent_id=agent.id, cidr="10.0.9.0/24",
        snmp_version="3",
        snmp_v3={
            "user": "noc-ro", "security_level": "authPriv",
            "auth_protocol": "SHA256", "auth_password": enc("auth-secret-xyz"),
            "priv_protocol": "AES128", "priv_password": enc("priv-secret-xyz"),
        },
    ))
    db.commit()
    cli = TestClient(app)
    resp = cli.get(
        f"/api/v1/agents/{agent.id}/config",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200
    sub = next(s for s in resp.json()["subnets"] if s["cidr"] == "10.0.9.0/24")
    assert sub["snmp_v3"]["auth_password"] == "auth-secret-xyz"
    assert sub["snmp_v3"]["priv_password"] == "priv-secret-xyz"


def test_agent_config_passes_legacy_plaintext_v3_through(db):
    """Subnets created before encryption shipped keep working unchanged."""
    site, agent, api_key = _seed_site_agent(db)
    db.add(m.Subnet(
        site_id=site.id, agent_id=agent.id, cidr="10.0.8.0/24",
        snmp_version="3",
        snmp_v3={"user": "old", "security_level": "authNoPriv",
                 "auth_protocol": "SHA", "auth_password": "legacy-plain"},
    ))
    db.commit()
    cli = TestClient(app)
    resp = cli.get(
        f"/api/v1/agents/{agent.id}/config",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    sub = next(s for s in resp.json()["subnets"] if s["cidr"] == "10.0.8.0/24")
    assert sub["snmp_v3"]["auth_password"] == "legacy-plain"


def test_oauth_token_persistence_is_encrypted(db):
    """The SMTP OAuth flow persists tokens via runtime.save_settings -- they
    must land encrypted like any other secret."""
    runtime.save_settings(db, {
        "smtp.oauth_refresh_token": "1//refresh-token-abc",
        "smtp.oauth_access_token": "ya29.access-token-def",
    })
    for key in ("smtp.oauth_refresh_token", "smtp.oauth_access_token"):
        row = db.scalar(select(m.AppSetting).where(m.AppSetting.key == key))
        assert is_encrypted(row.value)
    values = runtime.load_settings(db)
    assert values["smtp.oauth_refresh_token"] == "1//refresh-token-abc"
    assert values["smtp.oauth_access_token"] == "ya29.access-token-def"
