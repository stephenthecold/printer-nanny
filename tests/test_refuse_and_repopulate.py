"""Refusing bad input instead of substituting a default -- and handing it back.

Several handlers used to swallow an unparseable value and report success:

* ``runtime._coerce`` returned ``spec.default``, so a typo in the low-supply
  threshold moved it for the whole fleet under a green "Settings saved."
* ``schedule_update`` / ``schedule_create`` did ``except ValueError: pass``,
  keeping the old value. The life-threshold branch went further and cleared
  ``component_type`` too -- a field the operator had not touched.
* ``_coerce_role`` fell back to ``tech``: an unrecognised role silently produced
  an operator account with fleet-wide visibility across every tenant.
* ``subnet_add`` fell back to the agent's own site, filing a subnet -- and the
  SNMP credentials on it -- under a tenant nobody chose.

The rule now, and the two halves are equally important:

**Blank restores the default, and says so.** Clearing a numeric field is an
intelligible instruction and is honoured; refusing it would make resetting a
setting impossible without knowing the default. What changed is that the
operator is TOLD which settings moved and to what.

**A non-blank unparseable value is refused**, nothing is written, and the form
comes back filled in with what they typed. Refusing input the operator then has
to retype is how a validation rule gets worked around rather than obeyed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central import runtime
from central.dashboard import flash as fl
from central.main import app
from central.security import hash_password


class _Req:
    def __init__(self):
        self.session = {}


@pytest.fixture()
def admin(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("pw12345678"),
                  role=m.UserRole.admin))
    db.commit()
    http = TestClient(app)
    resp = http.post("/login", data={"username": "admin", "password": "pw12345678"},
                     follow_redirects=False)
    assert resp.status_code == 303
    return http


# --------------------------------------------------------------------------- #
# The channel carries the submission back
# --------------------------------------------------------------------------- #
def test_a_refusal_carries_fields_and_errors():
    req = _Req()
    fl.refuse(req, "Nothing saved.", fields={"a": "typo"}, errors={"a": "not a number"})
    got = fl.pop(req)
    assert got["level"] == "error"
    assert got["fields"] == {"a": "typo"}
    assert got["errors"] == {"a": "not a number"}


def test_fields_and_errors_are_always_dicts():
    """So a template can do flash.fields.get(...) without guarding."""
    req = _Req()
    fl.flash(req, "plain")
    got = fl.pop(req)
    assert got["fields"] == {} and got["errors"] == {}


def test_a_legacy_string_still_shapes_correctly():
    req = _Req()
    req.session[fl.FLASH_KEY] = "from an older build"
    got = fl.pop(req)
    assert got["fields"] == {} and got["errors"] == {}


# --------------------------------------------------------------------------- #
# Settings: blank restores, unparseable refuses
# --------------------------------------------------------------------------- #
def _numeric_spec():
    return next(s for s in runtime.SPECS if s.type in runtime.NUMERIC_TYPES)


def test_blank_is_reported_as_a_restore_not_an_error():
    spec = _numeric_spec()
    errors, cleared = runtime.check_settings({spec.key: ""})
    assert errors == {}
    assert cleared == {spec.key: spec.default}


def test_an_unparseable_value_is_an_error_naming_the_default():
    spec = _numeric_spec()
    errors, cleared = runtime.check_settings({spec.key: "thirty days"})
    assert spec.key in errors
    assert str(spec.default) in errors[spec.key], "the message must say how to reset"
    assert cleared == {}


def test_a_good_value_is_neither():
    spec = _numeric_spec()
    errors, cleared = runtime.check_settings({spec.key: "7"})
    assert errors == {} and cleared == {}


def test_secrets_are_stripped_before_a_form_is_handed_back():
    """Repopulation rides in the session cookie. The SMTP password does not
    belong there, in a log, or echoed into HTML."""
    secret_keys = [s.key for s in runtime.SPECS if s.type == "secret"]
    assert secret_keys, "no secret specs -- this test would pass vacuously"
    form = {k: "hunter2" for k in secret_keys}
    form["app.name"] = "Acme"
    safe = runtime.without_secret_values(form)
    assert safe == {"app.name": "Acme"}
    assert "hunter2" not in str(safe)


def test_the_read_path_stays_forgiving(db):
    """_coerce must NOT raise: an install already holding a bad value has to keep
    rendering. The refusal belongs on the save path, where a human is present."""
    spec = _numeric_spec()
    db.add(m.AppSetting(key=spec.key, value="not a number"))
    db.commit()
    values = runtime.load_settings(db)
    assert values[spec.key] == spec.default


def test_saving_a_bad_setting_writes_nothing_and_hands_it_back(admin, db):
    spec = _numeric_spec()
    before = runtime.load_settings(db)[spec.key]
    group = next(g for g, (_l, secs) in runtime.SETTINGS_GROUPS.items()
                 if spec.section in secs)
    resp = admin.post("/settings", data={"_group": group, spec.key: "twenty"},
                      follow_redirects=True)
    assert resp.status_code == 200
    db.expire_all()
    assert runtime.load_settings(db)[spec.key] == before, "a refused save still wrote"
    assert "twenty" in resp.text, "the rejected value was not handed back"


# --------------------------------------------------------------------------- #
# Roles: never fall back to a privileged default
# --------------------------------------------------------------------------- #
def test_an_unknown_role_is_refused_not_silently_made_tech(admin, db):
    from central.dashboard.manage import _coerce_role

    assert _coerce_role("operator") is None
    assert _coerce_role("tech") == m.UserRole.tech

    resp = admin.post("/manage/users", data={
        "username": "newbie", "password": "pw12345678", "role": "superuser",
    }, follow_redirects=True)
    assert resp.status_code == 200
    created = db.scalar(
        m.User.__table__.select().where(m.User.username == "newbie")
    )
    assert created is None, "an unrecognised role created an account anyway"


# --------------------------------------------------------------------------- #
# Maintenance schedules
# --------------------------------------------------------------------------- #
def _schedule(db):
    sched = m.MaintenanceSchedule(name="Fuser", interval_days=90, page_threshold=50000)
    db.add(sched)
    db.commit()
    return sched


def test_an_unparseable_interval_changes_nothing(admin, db):
    """The update form's numeric inputs are HIDDEN fields echoing stored values,
    so an operator cannot type into them and there is nothing to repopulate --
    this path is only reachable by a crafted POST. What matters here is that it
    writes nothing and says so, rather than keeping the old value under a green
    "Schedule updated."
    """
    sched = _schedule(db)
    resp = admin.post(f"/manage/maintenance/schedules/{sched.id}", data={
        "name": "Fuser", "interval_days": "90 days", "page_threshold": "50000",
    }, follow_redirects=True)
    assert resp.status_code == 200
    db.expire_all()
    refreshed = db.get(m.MaintenanceSchedule, sched.id)
    assert refreshed.interval_days == 90, "the old value was silently kept and reported OK"
    assert "was not changed" in resp.text, "the refusal was not reported"


def test_a_refused_create_hands_the_form_back(admin, db):
    """The create form is where an operator actually types these, so this is
    where repopulation earns its keep."""
    resp = admin.post("/manage/maintenance/schedules", data={
        "name": "Fuser kit", "interval_days": "90 days", "page_threshold": "50000",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert db.scalar(
        m.MaintenanceSchedule.__table__.select()
        .where(m.MaintenanceSchedule.name == "Fuser kit")
    ) is None, "a refused create still inserted a row"
    assert "90 days" in resp.text, "the rejected value was not handed back"
    assert "Fuser kit" in resp.text, "the fields that WERE valid were not preserved"


def test_a_bad_life_threshold_does_not_clear_the_component_type(admin, db):
    """The sharp one: an unparseable life threshold used to null component_type
    as well -- discarding a setting the operator never touched."""
    sched = m.MaintenanceSchedule(name="Belt", component_type="belt", life_threshold=20.0)
    db.add(sched)
    db.commit()
    resp = admin.post(f"/manage/maintenance/schedules/{sched.id}", data={
        "name": "Belt", "component_type": "belt", "life_threshold": "twenty percent",
    }, follow_redirects=True)
    assert resp.status_code == 200
    db.expire_all()
    refreshed = db.get(m.MaintenanceSchedule, sched.id)
    assert refreshed.component_type == "belt"
    assert refreshed.life_threshold == 20.0


def test_a_blank_interval_still_clears_it(admin, db):
    """Blank remains an instruction, not an error."""
    sched = _schedule(db)
    resp = admin.post(f"/manage/maintenance/schedules/{sched.id}", data={
        "name": "Fuser", "interval_days": "", "page_threshold": "50000",
    }, follow_redirects=True)
    assert resp.status_code == 200
    db.expire_all()
    assert db.get(m.MaintenanceSchedule, sched.id).interval_days is None
