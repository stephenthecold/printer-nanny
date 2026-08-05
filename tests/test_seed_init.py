"""Idempotent first-run bootstrap that the docker compose api service invokes."""

from __future__ import annotations

from central import models as m
from central import seed
from central.security import hash_password, verify_password


def test_seed_init_creates_one_admin_with_a_generated_password(db):
    """One admin, a generated password, and deliberately no ``tech`` account.

    This test previously asserted ``{"admin", "tech"}`` and that the admin's
    password was literally ``admin``. Both were the defect, not the contract:
    a published credential recreated on every container start is the same hole
    twice, and ``tech``/``tech`` was the second instance of it. The bootstrap
    now generates the password and forces a change on first login, so asserting
    a known one here would re-pin exactly what was fixed.
    """
    seed.seed_init()
    users = {u.username: u for u in db.query(m.User).all()}
    assert set(users) == {"admin"}
    assert users["admin"].role == m.UserRole.admin
    assert not verify_password("admin", users["admin"].password_hash), (
        "the bootstrap admin must not have a guessable password"
    )
    assert users["admin"].must_change_password, (
        "a generated password is only safe if the operator is forced to replace it"
    )
    # And the global alert rules are in place. Assert the condition types rather
    # than a bare count, so adding a rule has to be a deliberate edit here.
    assert {r.condition_type for r in db.query(m.AlertRule)} == {
        m.AlertConditionType.supply_below,
        m.AlertConditionType.error_severity,
        m.AlertConditionType.offline_minutes,
        m.AlertConditionType.printer_offline,
    }


def test_seed_init_is_idempotent(db):
    seed.seed_init()
    seed.seed_init()  # second call must not duplicate rows
    assert db.query(m.User).count() == 1
    assert db.query(m.AlertRule).count() == 4


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
    # And we did not add the default rules either -- the DB is already in use.
    assert db.query(m.User).count() == 1
    assert db.query(m.AlertRule).count() == 0
