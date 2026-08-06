"""The directory-connection operator surface.

The thing most worth testing here is not the CRUD -- it is that a stored
credential goes in encrypted, never comes back out to the browser, and is not
destroyed by an ordinary edit.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central.main import app
from central.secrets import decrypt_value, is_encrypted
from central.security import hash_password


def _admin(db, username="admin"):
    u = m.User(username=username, password_hash=hash_password("pw12345678"),
               role=m.UserRole.admin)
    db.add(u)
    db.commit()
    return u


def _login(username="admin") -> TestClient:
    cli = TestClient(app)
    cli.post("/login", data={"username": username, "password": "pw12345678"},
             follow_redirects=False)
    return cli


def _client(db, name="Acme"):
    c = m.Client(name=name)
    db.add(c)
    db.commit()
    return c


def _save(cli, client_id, provider="entra", secret="s3cret", **extra):
    data = {"client_id": client_id, "provider": provider, "enabled": "1",
            "cfg_tenant_id": "tenant-abc", "cfg_client_id": "app-xyz"}
    data.update(extra)
    if secret is not None:
        data["secret"] = secret
    return cli.post("/manage/people/directory/save", data=data,
                    follow_redirects=False)


# --------------------------- secrets ---------------------------------------- #

def test_the_secret_is_encrypted_at_rest(db):
    _admin(db)
    c = _client(db)
    _save(_login(), c.id, secret="super-secret-value")

    conn = db.scalar(select(m.DirectoryConnection))
    assert conn is not None
    assert conn.secret != "super-secret-value"
    assert is_encrypted(conn.secret)
    assert decrypt_value(conn.secret) == "super-secret-value"


def test_the_secret_is_never_rendered_back_to_the_browser(db):
    _admin(db)
    c = _client(db)
    cli = _login()
    _save(cli, c.id, secret="super-secret-value")

    body = cli.get(f"/manage/people?client_id={c.id}").text
    assert "super-secret-value" not in body
    # The ciphertext must not leak either -- it is still credential material.
    conn = db.scalar(select(m.DirectoryConnection))
    assert conn.secret not in body


def test_an_empty_secret_field_keeps_the_stored_one(db):
    """Otherwise correcting a base DN silently wipes the credential, and the
    next sync fails for a reason nobody connects to the edit."""
    _admin(db)
    c = _client(db)
    cli = _login()
    _save(cli, c.id, secret="original")
    _save(cli, c.id, secret="", cfg_tenant_id="tenant-corrected")

    db.expire_all()
    conn = db.scalar(select(m.DirectoryConnection))
    assert decrypt_value(conn.secret) == "original"
    assert conn.config["tenant_id"] == "tenant-corrected"


def test_a_new_secret_replaces_the_old_one(db):
    _admin(db)
    c = _client(db)
    cli = _login()
    _save(cli, c.id, secret="original")
    _save(cli, c.id, secret="rotated")

    db.expire_all()
    conn = db.scalar(select(m.DirectoryConnection))
    assert decrypt_value(conn.secret) == "rotated"


def test_the_audit_trail_records_key_names_not_values(db):
    _admin(db)
    c = _client(db)
    _save(_login(), c.id, secret="super-secret-value")

    rows = list(db.scalars(select(m.AuditLog)))
    assert any(r.action == "directory.create" for r in rows)
    for row in rows:
        assert "super-secret-value" not in (row.detail or "")
        assert "super-secret-value" not in (row.target or "")


# --------------------------- config ----------------------------------------- #

def test_entras_client_id_does_not_collide_with_the_tenant_client_id(db):
    """Entra's own config field is called client_id. Without the cfg_ prefix it
    would overwrite the Printer Nanny client id and write to the wrong tenant."""
    _admin(db)
    acme = _client(db, "Acme")
    _client(db, "Globex")
    _save(_login(), acme.id, secret="s", cfg_client_id="entra-app-id")

    conn = db.scalar(select(m.DirectoryConnection))
    assert conn.client_id == acme.id
    assert conn.config["client_id"] == "entra-app-id"


def test_only_the_selected_providers_fields_are_stored(db):
    _admin(db)
    c = _client(db)
    _save(_login(), c.id, provider="ad", secret="pw",
          cfg_server="dc.acme.local", cfg_base_dn="DC=acme,DC=local",
          cfg_bind_dn="CN=svc", cfg_tenant_id="should-not-persist")

    conn = db.scalar(select(m.DirectoryConnection))
    assert conn.config["server"] == "dc.acme.local"
    assert "tenant_id" not in conn.config


def test_one_connection_per_provider_per_client(db):
    _admin(db)
    c = _client(db)
    cli = _login()
    _save(cli, c.id, secret="a")
    _save(cli, c.id, secret="b")
    assert db.query(m.DirectoryConnection).count() == 1


def test_two_clients_can_each_have_their_own_entra(db):
    _admin(db)
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    cli = _login()
    _save(cli, acme.id, secret="a")
    _save(cli, globex.id, secret="b")
    assert db.query(m.DirectoryConnection).count() == 2


def test_manual_is_refused_as_a_connection_provider(db):
    _admin(db)
    c = _client(db)
    r = _save(_login(), c.id, provider="manual", secret="x")
    assert r.status_code == 303
    assert db.query(m.DirectoryConnection).count() == 0


def test_an_unknown_provider_is_refused(db):
    _admin(db)
    c = _client(db)
    _save(_login(), c.id, provider="ldap-ish", secret="x")
    assert db.query(m.DirectoryConnection).count() == 0


# --------------------------- access control --------------------------------- #

def test_readonly_users_cannot_touch_connections(db):
    _admin(db, "viewer")
    db.query(m.User).filter_by(username="viewer").update(
        {"role": m.UserRole.client_readonly}
    )
    db.commit()
    c = _client(db)
    r = _save(_login("viewer"), c.id, secret="x")
    # 403: signed in, not permitted -- not the sign-in form.
    assert r.status_code == 403
    assert db.query(m.DirectoryConnection).count() == 0


def test_anonymous_cannot_touch_connections(db):
    c = _client(db)
    r = TestClient(app).post("/manage/people/directory/save",
                             data={"client_id": c.id, "provider": "entra",
                                   "secret": "x"},
                             follow_redirects=False)
    assert r.status_code == 303
    assert db.query(m.DirectoryConnection).count() == 0


# --------------------------- lifecycle -------------------------------------- #

def test_deleting_a_connection_keeps_the_people_it_synced(db):
    """Removing a connection is about credentials. Cascading to end_users would
    deprovision a whole company because somebody rotated a secret badly."""
    _admin(db)
    c = _client(db)
    cli = _login()
    _save(cli, c.id, secret="x")
    conn = db.scalar(select(m.DirectoryConnection))
    db.add(m.EndUser(client_id=c.id, directory_source=m.DirectorySource.entra,
                     directory_id="oid-1", display_name="Ann"))
    db.commit()

    cli.post(f"/manage/people/directory/{conn.id}/delete", follow_redirects=False)
    db.expire_all()
    assert db.query(m.DirectoryConnection).count() == 0
    survivor = db.scalar(select(m.EndUser))
    assert survivor is not None and survivor.active is True


def test_sync_now_reports_a_provider_failure_without_crashing(db):
    """No network in tests, so the provider genuinely fails here -- which is
    the path an operator hits with a bad secret, and it must be a flash
    message, not a 500."""
    _admin(db)
    c = _client(db)
    cli = _login()
    _save(cli, c.id, secret="definitely-wrong")
    conn = db.scalar(select(m.DirectoryConnection))

    r = cli.post(f"/manage/people/directory/{conn.id}/sync", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    conn = db.scalar(select(m.DirectoryConnection))
    assert conn.last_ok is False
    assert conn.last_error
    assert conn.last_sync_at is not None
    # The stored error must not carry the credential.
    assert "definitely-wrong" not in conn.last_error


def test_a_failed_sync_deactivates_nobody(db):
    _admin(db)
    c = _client(db)
    cli = _login()
    _save(cli, c.id, secret="wrong")
    conn = db.scalar(select(m.DirectoryConnection))
    db.add(m.EndUser(client_id=c.id, directory_source=m.DirectorySource.entra,
                     directory_id="oid-1", display_name="Ann"))
    db.commit()

    cli.post(f"/manage/people/directory/{conn.id}/sync", follow_redirects=False)
    db.expire_all()
    assert db.scalar(select(m.EndUser)).active is True


def test_the_page_renders_all_three_providers(db):
    _admin(db)
    c = _client(db)
    body = _login().get(f"/manage/people?client_id={c.id}").text
    assert "Microsoft Entra ID" in body
    assert "Google Workspace" in body
    assert "On-prem Active Directory" in body
    # Prefixed field names, so the Entra collision cannot come back.
    assert 'name="cfg_tenant_id"' in body
    assert 'name="cfg_base_dn"' in body
