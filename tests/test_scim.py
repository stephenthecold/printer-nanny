"""SCIM 2.0 provisioning + deprovisioning.

Covers the enterprise off-boarding gate: an IdP provisions users, then
deactivates them (PATCH active=false) on termination -- after which login must
be rejected. Also: filter/get lookups, reactivation, bad-token 401, the
disabled-feature 404, the last-admin guard, and audit-row recording.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import runtime
from central.main import app
from central.security import hash_password

SCIM_TOKEN = "scim-test-token-abc123"
AUTH = {"Authorization": f"Bearer {SCIM_TOKEN}"}
SCIM_JSON = {"Content-Type": "application/scim+json"}


def _enable_scim(db, *, default_role: str = "tech") -> None:
    runtime.save_settings(
        db,
        {
            "scim.enabled": "on",
            "scim.bearer_token_hash": SCIM_TOKEN,
            "scim.default_role": default_role,
        },
        sections={"SCIM provisioning"},
    )
    db.commit()


def _client() -> TestClient:
    return TestClient(app)


def _provision(cli: TestClient, *, username: str, email: str, active: bool = True):
    return cli.post(
        "/scim/v2/Users",
        headers={**AUTH, **SCIM_JSON},
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": username,
            "emails": [{"value": email, "primary": True}],
            "active": active,
            "externalId": "ext-" + username,
        },
    )


# --------------------------------------------------------------------------- #
# Deprovision kills a LIVE dashboard session (regression)
# --------------------------------------------------------------------------- #
def test_deactivation_kills_live_dashboard_session(db):
    """A deactivated account's already-live dashboard cookie must stop working on
    its very next request -- not only block a fresh login.

    Regression: the dashboard modules resolved the session user with their own
    db.get(User, uid) helpers that skipped the active check in deps.current_user,
    so a SCIM-deprovisioned operator kept full dashboard access until the cookie
    expired.
    """
    pwd = "pw-ops-123"
    db.add(m.User(
        username="ops", password_hash=hash_password(pwd),
        role=m.UserRole.admin, active=True,
    ))
    db.commit()
    cli = TestClient(app)
    r = cli.post("/login", data={"username": "ops", "password": pwd}, follow_redirects=False)
    assert r.status_code == 303
    # Live session reaches the operator dashboard + an admin-only page.
    assert cli.get("/", follow_redirects=False).status_code == 200
    assert cli.get("/manage/users", follow_redirects=False).status_code == 200

    # Deprovision (what SCIM PATCH active=false does).
    user = db.scalar(select(m.User).where(m.User.username == "ops"))
    user.active = False
    db.commit()

    # The SAME cookie is now rejected everywhere -- not 200.
    for path in ("/", "/manage/users", "/settings", "/admin/backup"):
        resp = cli.get(path, follow_redirects=False)
        assert resp.status_code != 200, f"{path} still served a deactivated session"
        assert resp.status_code in (302, 303, 401, 403), f"{path} -> {resp.status_code}"


# --------------------------------------------------------------------------- #
# Provisioning
# --------------------------------------------------------------------------- #
def test_provision_creates_user(db):
    _enable_scim(db)
    cli = _client()
    r = _provision(cli, username="alice@acme.test", email="alice@acme.test")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["userName"] == "alice@acme.test"
    assert body["active"] is True
    assert body["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:User"]
    assert body["id"]
    assert body["meta"]["resourceType"] == "User"
    assert body["externalId"] == "ext-alice@acme.test"

    user = db.scalar(select(m.User).where(m.User.username == "alice@acme.test"))
    assert user is not None
    assert user.active is True
    assert user.auth_provider == "scim"
    assert user.password_hash is None  # IdP-authenticated, no local password
    assert user.role == m.UserRole.tech  # default role
    assert user.email == "alice@acme.test"


def test_provision_uses_configured_default_role(db):
    _enable_scim(db, default_role="client_readonly")
    cli = _client()
    r = _provision(cli, username="bob@acme.test", email="bob@acme.test")
    assert r.status_code == 201
    user = db.scalar(select(m.User).where(m.User.username == "bob@acme.test"))
    assert user.role == m.UserRole.client_readonly


def test_provision_is_idempotent_and_reactivates(db):
    _enable_scim(db)
    cli = _client()
    r1 = _provision(cli, username="carol@acme.test", email="carol@acme.test")
    assert r1.status_code == 201
    uid = r1.json()["id"]
    # Deactivate, then re-POST -> reactivates, returns 200 (not a duplicate 201).
    cli.patch(
        f"/scim/v2/Users/{uid}",
        headers={**AUTH, **SCIM_JSON},
        json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    r2 = _provision(cli, username="carol@acme.test", email="carol@acme.test")
    assert r2.status_code == 200
    assert r2.json()["active"] is True


# --------------------------------------------------------------------------- #
# Filter / get
# --------------------------------------------------------------------------- #
def test_filter_by_username_and_get(db):
    _enable_scim(db)
    cli = _client()
    _provision(cli, username="dave@acme.test", email="dave@acme.test")
    _provision(cli, username="erin@acme.test", email="erin@acme.test")

    r = cli.get(
        '/scim/v2/Users?filter=userName eq "dave@acme.test"', headers=AUTH
    )
    assert r.status_code == 200
    body = r.json()
    assert body["totalResults"] == 1
    assert body["Resources"][0]["userName"] == "dave@acme.test"
    uid = body["Resources"][0]["id"]

    r2 = cli.get(f"/scim/v2/Users/{uid}", headers=AUTH)
    assert r2.status_code == 200
    assert r2.json()["userName"] == "dave@acme.test"

    # Unknown user -> 404
    assert cli.get("/scim/v2/Users/999999", headers=AUTH).status_code == 404


def test_filter_by_email(db):
    _enable_scim(db)
    cli = _client()
    _provision(cli, username="frank", email="frank@acme.test")
    r = cli.get('/scim/v2/Users?filter=emails eq "frank@acme.test"', headers=AUTH)
    assert r.status_code == 200
    assert r.json()["totalResults"] == 1
    assert r.json()["Resources"][0]["userName"] == "frank"


def test_list_all_users(db):
    _enable_scim(db)
    cli = _client()
    _provision(cli, username="g@acme.test", email="g@acme.test")
    _provision(cli, username="h@acme.test", email="h@acme.test")
    r = cli.get("/scim/v2/Users", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["totalResults"] >= 2
    assert r.json()["schemas"] == [
        "urn:ietf:params:scim:api:messages:2.0:ListResponse"
    ]


# --------------------------------------------------------------------------- #
# Deprovision (the gate) + reactivation
# --------------------------------------------------------------------------- #
def test_patch_active_false_deactivates_and_blocks_login(db):
    _enable_scim(db)
    cli = _client()
    # Provision a user, then give them a local password so we can prove login is
    # blocked purely by the active flag (not by missing credentials).
    r = _provision(cli, username="leaver@acme.test", email="leaver@acme.test")
    uid = int(r.json()["id"])
    user = db.get(m.User, uid)
    user.password_hash = hash_password("hunter2")
    user.auth_provider = "local"
    db.commit()

    # Sanity: login works while active.
    login_ok = cli.post(
        "/login",
        data={"username": "leaver@acme.test", "password": "hunter2"},
        follow_redirects=False,
    )
    assert login_ok.status_code == 303  # redirect to "/" on success

    # IdP deprovisions: PATCH active=false.
    r = cli.patch(
        f"/scim/v2/Users/{uid}",
        headers={**AUTH, **SCIM_JSON},
        json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    assert r.status_code == 200
    assert r.json()["active"] is False
    db.refresh(user)
    assert user.active is False

    # Login is now rejected (re-renders the login page, no redirect).
    fresh = _client()
    login_blocked = fresh.post(
        "/login",
        data={"username": "leaver@acme.test", "password": "hunter2"},
        follow_redirects=False,
    )
    assert login_blocked.status_code == 200
    assert "Invalid credentials" in login_blocked.text


def test_patch_active_true_reactivates(db):
    _enable_scim(db)
    cli = _client()
    r = _provision(cli, username="rejoin@acme.test", email="rejoin@acme.test")
    uid = int(r.json()["id"])
    cli.patch(
        f"/scim/v2/Users/{uid}",
        headers={**AUTH, **SCIM_JSON},
        json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    r = cli.patch(
        f"/scim/v2/Users/{uid}",
        headers={**AUTH, **SCIM_JSON},
        json={"Operations": [{"op": "replace", "path": "active", "value": True}]},
    )
    assert r.status_code == 200
    assert r.json()["active"] is True
    db.refresh(db.get(m.User, uid))
    assert db.get(m.User, uid).active is True


def test_patch_lenient_top_level_active(db):
    """Connectors that send a bare {"active": false} body (no Operations)."""
    _enable_scim(db)
    cli = _client()
    r = _provision(cli, username="lenient@acme.test", email="lenient@acme.test")
    uid = int(r.json()["id"])
    r = cli.patch(
        f"/scim/v2/Users/{uid}", headers={**AUTH, **SCIM_JSON}, json={"active": False}
    )
    assert r.status_code == 200
    assert r.json()["active"] is False


def test_put_full_replace_can_deactivate(db):
    _enable_scim(db)
    cli = _client()
    r = _provision(cli, username="putuser@acme.test", email="putuser@acme.test")
    uid = int(r.json()["id"])
    r = cli.put(
        f"/scim/v2/Users/{uid}",
        headers={**AUTH, **SCIM_JSON},
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "putuser@acme.test",
            "active": False,
        },
    )
    assert r.status_code == 200
    assert r.json()["active"] is False
    assert db.get(m.User, uid).active is False


def test_delete_soft_deactivates(db):
    _enable_scim(db)
    cli = _client()
    r = _provision(cli, username="del@acme.test", email="del@acme.test")
    uid = int(r.json()["id"])
    r = cli.delete(f"/scim/v2/Users/{uid}", headers=AUTH)
    assert r.status_code == 204
    user = db.get(m.User, uid)
    assert user is not None  # soft delete: row preserved
    assert user.active is False
    # Idempotent: a second delete is still 204.
    assert cli.delete(f"/scim/v2/Users/{uid}", headers=AUTH).status_code == 204


# --------------------------------------------------------------------------- #
# Auth + enablement
# --------------------------------------------------------------------------- #
def test_bad_token_401(db):
    _enable_scim(db)
    cli = _client()
    r = cli.get("/scim/v2/Users", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    # Missing header entirely.
    assert cli.get("/scim/v2/Users").status_code == 401


def test_disabled_feature_404(db):
    # SCIM not enabled -> whole surface 404s even with a (would-be) valid token.
    runtime.save_settings(
        db, {"scim.bearer_token_hash": SCIM_TOKEN}, sections={"SCIM provisioning"}
    )
    db.commit()
    cli = _client()
    assert cli.get("/scim/v2/Users", headers=AUTH).status_code == 404


# --------------------------------------------------------------------------- #
# Last-admin guard
# --------------------------------------------------------------------------- #
def test_last_admin_cannot_be_deactivated_via_patch(db):
    _enable_scim(db)
    admin = m.User(username="root", password_hash=hash_password("x"),
                   role=m.UserRole.admin, active=True)
    db.add(admin)
    db.commit()
    aid = admin.id
    cli = _client()
    r = cli.patch(
        f"/scim/v2/Users/{aid}",
        headers={**AUTH, **SCIM_JSON},
        json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    assert r.status_code == 409
    db.refresh(admin)
    assert admin.active is True  # still active -- guard held


def test_last_admin_cannot_be_deleted(db):
    _enable_scim(db)
    admin = m.User(username="root2", password_hash=hash_password("x"),
                   role=m.UserRole.admin, active=True)
    db.add(admin)
    db.commit()
    aid = admin.id
    cli = _client()
    assert cli.delete(f"/scim/v2/Users/{aid}", headers=AUTH).status_code == 409
    db.refresh(admin)
    assert admin.active is True


def test_non_last_admin_can_be_deactivated(db):
    _enable_scim(db)
    a1 = m.User(username="admin1", password_hash=hash_password("x"),
                role=m.UserRole.admin, active=True)
    a2 = m.User(username="admin2", password_hash=hash_password("x"),
                role=m.UserRole.admin, active=True)
    db.add_all([a1, a2])
    db.commit()
    cli = _client()
    r = cli.patch(
        f"/scim/v2/Users/{a2.id}",
        headers={**AUTH, **SCIM_JSON},
        json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    assert r.status_code == 200
    db.refresh(a2)
    assert a2.active is False


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def test_scim_actions_recorded_in_audit_log(db):
    _enable_scim(db)
    cli = _client()
    r = _provision(cli, username="audited@acme.test", email="audited@acme.test")
    uid = int(r.json()["id"])
    cli.patch(
        f"/scim/v2/Users/{uid}",
        headers={**AUTH, **SCIM_JSON},
        json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    actions = [
        row.action
        for row in db.scalars(
            select(m.AuditLog).order_by(m.AuditLog.id)
        )
    ]
    assert "scim.user.provision" in actions
    assert "scim.user.patch" in actions
