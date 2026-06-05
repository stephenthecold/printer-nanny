"""OIDC user matching / provisioning and the enabled check."""

from __future__ import annotations

from central import models as m
from central import runtime
from central.auth_oidc import _match_or_provision, oidc_enabled
from central.security import hash_password


def test_oidc_enabled_requires_issuer_and_client(db):
    assert oidc_enabled(db) is False
    runtime.save_settings(db, {"oidc.enabled": "on"})
    assert oidc_enabled(db) is False  # still missing issuer/client_id
    runtime.save_settings(db, {
        "oidc.enabled": "on", "oidc.issuer": "https://idp.test", "oidc.client_id": "abc"
    })
    assert oidc_enabled(db) is True


def test_match_existing_user_by_email(db):
    db.add(m.User(username="jane", email="jane@acme.test",
                  password_hash=hash_password("x"), role=m.UserRole.admin))
    db.commit()
    user = _match_or_provision(db, "jane@acme.test", {}, {"auto_provision": False})
    assert user is not None
    assert user.username == "jane"


def test_match_links_email_to_username_only_user(db):
    # Local user created with username == email but no email column set.
    db.add(m.User(username="bob@acme.test", password_hash=hash_password("x")))
    db.commit()
    user = _match_or_provision(db, "bob@acme.test", {}, {"auto_provision": False})
    assert user is not None
    assert user.email == "bob@acme.test"  # back-filled


def test_auto_provision_new_user(db):
    user = _match_or_provision(
        db, "new@acme.test", {}, {"auto_provision": True, "default_role": "tech"}
    )
    assert user is not None
    assert user.auth_provider == "oidc"
    assert user.password_hash is None
    assert user.role == m.UserRole.tech


def test_no_provision_when_disabled(db):
    assert _match_or_provision(db, "ghost@acme.test", {}, {"auto_provision": False}) is None
