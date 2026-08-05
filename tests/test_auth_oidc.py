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


def test_match_existing_sso_user_by_email(db):
    db.add(m.User(username="jane", email="jane@acme.test", auth_provider="oidc",
                  role=m.UserRole.admin))
    db.commit()
    user = _match_or_provision(db, "jane@acme.test", {}, {"auto_provision": False})
    assert user is not None
    assert user.username == "jane"


def test_match_links_email_to_username_only_user(db):
    # SCIM provisioned it with username == email and no email column set. This is
    # the ordinary Entra/Okta shape -- SCIM provisions, OIDC authenticates -- so
    # it must keep working.
    db.add(m.User(username="bob@acme.test", auth_provider="scim"))
    db.commit()
    user = _match_or_provision(db, "bob@acme.test", {}, {"auto_provision": False})
    assert user is not None
    assert user.email == "bob@acme.test"  # back-filled


# --------------------------------------------------------------------------- #
# An SSO login must not adopt a password account
# --------------------------------------------------------------------------- #
# This was the shortest path to admin in the product. Matching was by email OR
# username with no check of what kind of account it found, and it ran BEFORE the
# auto_provision gate -- so turning auto-provisioning off did not close it
# either. Guest and self-service sign-up are ordinary IdP features, so "an
# address the IdP will issue" is not a secret.
#
# The two tests above previously asserted exactly this behaviour, with local
# password accounts, which is why it survived: the vulnerability was the
# documented design.
def test_sso_does_not_adopt_a_local_password_account(db):
    db.add(m.User(username="owner", email="owner@msp.test",
                  password_hash=hash_password("x"), role=m.UserRole.admin,
                  auth_provider="local"))
    db.commit()
    assert _match_or_provision(
        db, "owner@msp.test", {}, {"auto_provision": False}
    ) is None


def test_sso_does_not_adopt_a_local_account_via_its_username(db):
    """The username arm is the same reach -- a bootstrap admin is often named
    for an operator's address."""
    db.add(m.User(username="ops@msp.test", password_hash=hash_password("x"),
                  role=m.UserRole.admin, auth_provider="local"))
    db.commit()
    assert _match_or_provision(
        db, "ops@msp.test", {}, {"auto_provision": True}
    ) is None


def test_an_operator_can_opt_into_linking_local_accounts(db):
    """Migrating known users to SSO is legitimate -- it is just a decision an
    operator makes, not one an unauthenticated caller makes for them."""
    db.add(m.User(username="owner", email="owner@msp.test",
                  password_hash=hash_password("x"), auth_provider="local"))
    db.commit()
    user = _match_or_provision(
        db, "owner@msp.test", {},
        {"auto_provision": False, "link_local_accounts": True},
    )
    assert user is not None and user.username == "owner"


def test_an_unverified_email_claim_is_refused(db):
    """An IdP saying the address is unverified is telling us the user typed it,
    and this function turns text into 'which account is this'."""
    db.add(m.User(username="jane", email="jane@acme.test", auth_provider="oidc"))
    db.commit()
    assert _match_or_provision(
        db, "jane@acme.test", {"email_verified": False}, {"auto_provision": True}
    ) is None
    # A MISSING claim stays tolerated -- plenty of IdPs omit it for managed users.
    assert _match_or_provision(
        db, "jane@acme.test", {}, {"auto_provision": False}
    ) is not None


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
