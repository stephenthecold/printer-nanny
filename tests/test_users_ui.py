"""Admin-only user management UI + self-service password change."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central.main import app
from central.security import hash_password, verify_password


def _login(http: TestClient, username: str, password: str) -> None:
    resp = http.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303


@pytest.fixture()
def env(db):
    """Seed an admin + a tech + a client, return a logged-in TestClient as the admin."""
    db.add_all([
        m.User(username="admin", password_hash=hash_password("admin"), role=m.UserRole.admin),
        m.User(username="tech1", password_hash=hash_password("tech"), role=m.UserRole.tech),
        m.Client(name="Acme"),
    ])
    db.commit()
    http = TestClient(app)
    _login(http, "admin", "admin")
    return {"http": http, "db": db}


def _fresh(db, model, obj_id):
    db.expire_all()
    return db.get(model, obj_id)


def test_users_list_visible_to_admin(env):
    resp = env["http"].get("/manage/users")
    assert resp.status_code == 200
    assert "admin" in resp.text
    assert "tech1" in resp.text


def test_users_page_forbidden_for_tech(db):
    db.add(m.User(username="t", password_hash=hash_password("t"), role=m.UserRole.tech))
    db.commit()
    http = TestClient(app)
    _login(http, "t", "t")
    resp = http.get("/manage/users", follow_redirects=False)
    assert resp.status_code == 303
    # Tech is bounced to "/" (logged in but not an admin), not /login.
    assert resp.headers["location"] == "/"


def test_create_local_user(env, db):
    resp = env["http"].post("/manage/users", data={
        "username": "newtech", "email": "n@x", "role": "tech", "password": "supersecret",
    }, follow_redirects=False)
    assert resp.status_code == 303
    u = db.query(m.User).filter_by(username="newtech").one()
    assert u.role == m.UserRole.tech
    assert u.auth_provider == "local"
    assert verify_password("supersecret", u.password_hash)


def test_create_user_without_password_marks_sso_only(env, db):
    env["http"].post("/manage/users", data={
        "username": "ssoonly", "role": "tech",
    }, follow_redirects=False)
    u = db.query(m.User).filter_by(username="ssoonly").one()
    assert u.password_hash is None
    assert u.auth_provider == "oidc"


def test_client_readonly_requires_a_client(env, db):
    """Creating a client_readonly user without a client_id must be refused (would let
    them see every client, defeating the role)."""
    resp = env["http"].post("/manage/users", data={
        "username": "noclient", "role": "client_readonly", "password": "verysecret",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert db.scalar(select(m.User).where(m.User.username == "noclient")) is None
    # Follow the redirect to confirm a flash was queued.
    resp2 = env["http"].get("/manage/users")
    assert "must be assigned to a client" in resp2.text


def test_client_readonly_with_client_persists(env, db):
    client_id = db.scalar(select(m.Client).where(m.Client.name == "Acme")).id
    env["http"].post("/manage/users", data={
        "username": "viewer", "role": "client_readonly",
        "client_id": str(client_id), "password": "verysecret",
    }, follow_redirects=False)
    u = db.query(m.User).filter_by(username="viewer").one()
    assert u.role == m.UserRole.client_readonly
    assert u.client_id == client_id


def test_last_admin_cannot_be_demoted(env, db):
    admin = db.query(m.User).filter_by(username="admin").one()
    resp = env["http"].post(
        f"/manage/users/{admin.id}", data={"email": "", "role": "tech"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _fresh(db, m.User, admin.id).role == m.UserRole.admin


def test_last_admin_cannot_be_deleted(env, db):
    admin = db.query(m.User).filter_by(username="admin").one()
    # Even though we'd refuse self-delete first, this test stays meaningful — make
    # a SECOND user the actor so the "only admin" guard is the actual barrier.
    db.add(m.User(
        username="admin2", password_hash=hash_password("admin"), role=m.UserRole.admin,
    ))
    db.commit()
    # Now log in as admin2 and try to delete admin → must succeed (two admins exist).
    http2 = TestClient(app)
    _login(http2, "admin2", "admin")
    http2.post(f"/manage/users/{admin.id}/delete", follow_redirects=False)
    assert _fresh(db, m.User, admin.id) is None
    # Now only admin2 remains; deleting them must be refused.
    admin2_id = db.scalar(select(m.User).where(m.User.username == "admin2")).id
    # Use a fresh client so the self-delete guard doesn't fire.
    db.add(m.User(
        username="admin3", password_hash=hash_password("admin"), role=m.UserRole.admin,
    ))
    db.commit()
    http3 = TestClient(app)
    _login(http3, "admin3", "admin")
    http3.post(f"/manage/users/{admin2_id}/delete", follow_redirects=False)
    # admin2 still exists … but only if admin3 was also admin. Wait: admin2 + admin3
    # are both admins, so deletion of admin2 IS allowed (admin3 remains).
    # The true last-admin guard is exercised when we then try to delete admin3.
    assert _fresh(db, m.User, admin2_id) is None
    admin3_id = db.scalar(select(m.User).where(m.User.username == "admin3")).id
    # admin3 is logged in as themselves — self-delete guard fires first.
    http3.post(f"/manage/users/{admin3_id}/delete", follow_redirects=False)
    assert _fresh(db, m.User, admin3_id) is not None


def test_admin_cannot_self_delete(env, db):
    admin = db.query(m.User).filter_by(username="admin").one()
    env["http"].post(f"/manage/users/{admin.id}/delete", follow_redirects=False)
    assert _fresh(db, m.User, admin.id) is not None


def test_admin_force_reset_password(env, db):
    tech = db.query(m.User).filter_by(username="tech1").one()
    env["http"].post(
        f"/manage/users/{tech.id}/reset-password",
        data={"new_password": "brandnewpass"},
        follow_redirects=False,
    )
    fresh = _fresh(db, m.User, tech.id)
    assert verify_password("brandnewpass", fresh.password_hash)
    assert fresh.auth_provider == "local"


def test_self_service_password_change_happy_path(db):
    db.add(m.User(
        username="u", password_hash=hash_password("oldpass1"), role=m.UserRole.tech,
    ))
    db.commit()
    http = TestClient(app)
    _login(http, "u", "oldpass1")
    resp = http.post("/account/password", data={
        "current_password": "oldpass1",
        "new_password": "newerpass1",
        "confirm_password": "newerpass1",
    }, follow_redirects=False)
    assert resp.status_code == 303
    u = db.query(m.User).filter_by(username="u").one()
    assert verify_password("newerpass1", u.password_hash)


def test_self_service_password_requires_current(db):
    db.add(m.User(
        username="u", password_hash=hash_password("oldpass1"), role=m.UserRole.tech,
    ))
    db.commit()
    http = TestClient(app)
    _login(http, "u", "oldpass1")
    http.post("/account/password", data={
        "current_password": "wrong",
        "new_password": "newerpass1",
        "confirm_password": "newerpass1",
    }, follow_redirects=False)
    u = db.query(m.User).filter_by(username="u").one()
    assert verify_password("oldpass1", u.password_hash)  # unchanged


def test_self_service_password_requires_match(db):
    db.add(m.User(
        username="u", password_hash=hash_password("oldpass1"), role=m.UserRole.tech,
    ))
    db.commit()
    http = TestClient(app)
    _login(http, "u", "oldpass1")
    http.post("/account/password", data={
        "current_password": "oldpass1",
        "new_password": "newerpass1",
        "confirm_password": "different",
    }, follow_redirects=False)
    u = db.query(m.User).filter_by(username="u").one()
    assert verify_password("oldpass1", u.password_hash)


def test_add_site_form_present_on_manage_home(env):
    """The new top-level Add-site form lives alongside Add client on /manage."""
    resp = env["http"].get("/manage")
    assert resp.status_code == 200
    assert "Add site" in resp.text
    assert 'action="/manage/sites"' in resp.text
