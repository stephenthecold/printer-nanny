"""Idempotent first-run bootstrap that the docker compose api service invokes."""

from __future__ import annotations

from central import models as m
from central import seed
from central.security import hash_password, verify_password


def test_seed_init_creates_admin_when_empty(db):
    seed.seed_init()
    users = {u.username: u for u in db.query(m.User).all()}
    assert set(users) == {"admin", "tech"}
    assert users["admin"].role == m.UserRole.admin
    assert verify_password("admin", users["admin"].password_hash)
    # And the global alert rules are in place (low supply / errors / agent offline).
    assert db.query(m.AlertRule).count() == 3


def test_seed_init_is_idempotent(db):
    seed.seed_init()
    seed.seed_init()  # second call must not duplicate rows
    assert db.query(m.User).count() == 2
    assert db.query(m.AlertRule).count() == 3


def test_seed_init_leaves_existing_users_alone(db):
    """If an operator has already changed the admin password, don't trample it."""
    db.add(m.User(
        username="admin", password_hash=hash_password("a-strong-password"),
        role=m.UserRole.admin,
    ))
    db.commit()
    seed.seed_init()
    admin = db.query(m.User).filter_by(username="admin").one()
    assert verify_password("a-strong-password", admin.password_hash)
    # And we didn't add tech or the default rules either — the DB is already in use.
    assert db.query(m.User).count() == 1
    assert db.query(m.AlertRule).count() == 0
