"""Onboarding defaults: a new client arrives already monitored.

The rule that matters is that defaults are a starting point, not a policy:
applied once at creation, never re-applied, never overwriting what an operator
tuned afterwards, and always visible in the audit trail rather than appearing
unexplained in someone's alert rules.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import runtime
from central import services
from central.main import app
from central.security import hash_password


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


def _submit(cli, **overrides):
    data = {
        "client_name": "Acme Ltd",
        "site_name": "Head office",
        "cidr": "",
        "ttl_minutes": 1440,
    }
    data.update(overrides)
    return cli.post("/manage/onboard", data=data, follow_redirects=False)


# ---------- parsing ----------

def test_quiet_hours_parsing_handles_the_wrapping_case(db):
    assert services._parse_quiet_hours("18:00-07:00") == (1080, 420)
    assert services._parse_quiet_hours("09:30-17:45") == (570, 1065)


def test_malformed_quiet_hours_degrade_rather_than_raise(db):
    for bad in ("", "garbage", "25:00-07:00", "18:00", "18:61-07:00", None):
        assert services._parse_quiet_hours(bad) is None


def test_weekday_parsing_ignores_junk_and_defaults_to_every_day(db):
    assert services._parse_weekdays("0,1,4") == [0, 1, 4]
    assert services._parse_weekdays("0,0,1") == [0, 1]
    assert services._parse_weekdays("9,x,-1") == [0, 1, 2, 3, 4, 5, 6]
    assert services._parse_weekdays("") == [0, 1, 2, 3, 4, 5, 6]


# ---------- application ----------

def test_defaults_create_a_client_scoped_rule_and_window(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _submit(_login("admin"))
    db.expire_all()

    client = db.scalar(select(m.Client).where(m.Client.name == "Acme Ltd"))
    rule = db.scalar(select(m.AlertRule).where(m.AlertRule.scope_id == client.id))
    assert rule is not None
    assert rule.scope == m.AlertScope.client
    assert rule.condition_type == m.AlertConditionType.supply_below
    assert rule.threshold == 15

    window = db.scalar(
        select(m.SuppressionWindow).where(m.SuppressionWindow.scope_id == client.id)
    )
    assert window is not None
    assert window.kind == m.SuppressionKind.quiet_hours
    assert window.action == m.SuppressionAction.defer
    assert (window.start_minute, window.end_minute) == (1080, 420)


def test_defaults_can_be_turned_off(db):
    _mk_user(db, "admin", m.UserRole.admin)
    # Bools follow checkbox semantics: an unchecked box posts nothing, so
    # clearing one means naming the section and omitting the key. Passing
    # {key: False} would read as "checkbox present" and set it True.
    runtime.save_settings(db, {}, sections={"Onboarding defaults"})
    db.commit()
    _submit(_login("admin"))
    db.expire_all()
    assert not list(db.scalars(select(m.AlertRule)))
    assert not list(db.scalars(select(m.SuppressionWindow)))


def test_a_zero_threshold_skips_only_the_rule(db):
    _mk_user(db, "admin", m.UserRole.admin)
    runtime.save_settings(db, {"onboarding.low_supply_pct": 0})
    db.commit()
    _submit(_login("admin"))
    db.expire_all()
    assert not list(db.scalars(select(m.AlertRule)))
    assert list(db.scalars(select(m.SuppressionWindow)))


def test_blank_quiet_hours_skips_only_the_window(db):
    _mk_user(db, "admin", m.UserRole.admin)
    runtime.save_settings(db, {"onboarding.quiet_hours": ""})
    db.commit()
    _submit(_login("admin"))
    db.expire_all()
    assert list(db.scalars(select(m.AlertRule)))
    assert not list(db.scalars(select(m.SuppressionWindow)))


def test_a_malformed_setting_does_not_block_onboarding(db):
    """The customer still gets created; they just don't get that one default."""
    _mk_user(db, "admin", m.UserRole.admin)
    runtime.save_settings(db, {"onboarding.quiet_hours": "not-a-window"})
    db.commit()
    _submit(_login("admin"))
    db.expire_all()
    assert db.scalar(select(m.Client).where(m.Client.name == "Acme Ltd")) is not None
    assert not list(db.scalars(select(m.SuppressionWindow)))


def test_defaults_are_not_reapplied_to_an_existing_client(db):
    _mk_user(db, "admin", m.UserRole.admin)
    cli = _login("admin")
    _submit(cli)
    _submit(cli, site_name="Second site")
    db.expire_all()
    assert len(list(db.scalars(select(m.AlertRule)))) == 1
    assert len(list(db.scalars(select(m.SuppressionWindow)))) == 1


def test_a_tuned_threshold_survives_a_second_site(db):
    """The failure this guards: onboarding site 2 quietly resetting site 1's tuning."""
    _mk_user(db, "admin", m.UserRole.admin)
    cli = _login("admin")
    _submit(cli)
    db.expire_all()
    rule = db.scalar(select(m.AlertRule))
    rule.threshold = 5.0
    db.commit()

    _submit(cli, site_name="Second site")
    db.expire_all()
    assert db.scalar(select(m.AlertRule)).threshold == 5.0


def test_what_was_created_is_audited(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _submit(_login("admin"))
    rows = list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "client.defaults_applied")
    ))
    assert len(rows) == 1
    assert "low-supply rule at 15%" in rows[0].detail
    assert "quiet hours 18:00-07:00" in rows[0].detail


def test_the_window_is_scoped_to_the_client_not_global(db):
    """A global window would silence every customer, not just the new one."""
    _mk_user(db, "admin", m.UserRole.admin)
    _submit(_login("admin"))
    db.expire_all()
    window = db.scalar(select(m.SuppressionWindow))
    assert window.scope == m.AlertScope.client
    assert window.scope_id is not None
