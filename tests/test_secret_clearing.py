"""A stored secret can be removed, and the field stops lying about what it holds.

Two halves of one defect. ``masked_for_form`` swapped a stored credential for
``SECRET_PLACEHOLDER`` and the template rendered that as the input's ``value``,
so:

* every configured secret looked like an eight-character password;
* the "unchanged" placeholder could never display, because an input holding text
  does not show one -- so nothing on the page said whether a credential was
  stored at all;
* and since blank ALSO means "keep" (which is what makes the form safe to submit
  without retyping every credential on it), there was no gesture in the product
  that removed one. Disabling a channel meant editing the database.

The rule now: the box renders EMPTY with a placeholder that says whether
something is held, blank still means "keep", and a per-secret checkbox is the
one way to remove it -- audited like any other settings change, and refused if
it arrives together with a replacement value, because that is two contradictory
instructions and one of them is a mistake.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import runtime
from central.main import app
from central.security import hash_password

SECRET = "smtp.password"
GROUP = "notifications"
SECTIONS = {"Email (SMTP)"}


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


def _clear(key: str) -> str:
    return runtime.secret_clear_field(key)


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
def test_the_clear_checkbox_removes_a_stored_secret(db):
    runtime.save_settings(db, {SECRET: "s3cret"})
    runtime.save_settings(db, {SECRET: "", _clear(SECRET): "1"}, sections=SECTIONS)
    assert runtime.load_settings(db)[SECRET] == ""


def test_blank_alone_still_keeps_it(db):
    """The half that must not change: an empty box is how an operator saves the
    rest of the form without retyping the credential."""
    runtime.save_settings(db, {SECRET: "s3cret"})
    runtime.save_settings(db, {SECRET: ""}, sections=SECTIONS)
    assert runtime.load_settings(db)[SECRET] == "s3cret"


def test_the_placeholder_still_keeps_it(db):
    runtime.save_settings(db, {SECRET: "s3cret"})
    runtime.save_settings(db, {SECRET: runtime.SECRET_PLACEHOLDER}, sections=SECTIONS)
    assert runtime.load_settings(db)[SECRET] == "s3cret"


def test_an_unticked_box_is_not_a_clear(db):
    """An unchecked checkbox posts nothing at all; an empty value posted by
    anything else must read the same way -- keep."""
    runtime.save_settings(db, {SECRET: "s3cret"})
    runtime.save_settings(db, {SECRET: "", _clear(SECRET): ""}, sections=SECTIONS)
    assert runtime.load_settings(db)[SECRET] == "s3cret"


def test_a_cleared_secret_does_not_fall_back_to_its_env_default(db, monkeypatch):
    """The reason clearing writes an EMPTY row rather than deleting one: several
    secret specs are seeded from the environment, so a deleted row would hand
    back the very credential the operator just removed."""
    spec = runtime.SPEC_BY_KEY[SECRET]
    monkeypatch.setattr(spec, "default", "from-the-environment")
    runtime.save_settings(db, {SECRET: "s3cret"})
    runtime.save_settings(db, {SECRET: "", _clear(SECRET): "1"}, sections=SECTIONS)
    assert runtime.load_settings(db)[SECRET] == ""


def test_clearing_is_scoped_to_the_posted_group(db):
    """A submission speaks only for the tab it rendered -- the same rule that
    stops an absent checkbox elsewhere reading as 'unchecked'."""
    runtime.save_settings(db, {"freescout.api_key": "abc123"})
    runtime.save_settings(
        db, {_clear("freescout.api_key"): "1"}, sections=SECTIONS,
    )
    assert runtime.load_settings(db)["freescout.api_key"] == "abc123"


def test_a_partial_update_never_clears(db):
    """sections=None is a partial write (logo upload, OAuth refresh, seed); none
    of those carry a clear box, and one arriving there would be a stray key."""
    runtime.save_settings(db, {SECRET: "s3cret"})
    runtime.save_settings(db, {"app.logo_url": "/branding/logo"})
    assert runtime.load_settings(db)[SECRET] == "s3cret"


def test_the_scim_token_can_be_cleared_too(db):
    """It is a str spec holding a hash, so it is not 'secret'-typed -- but it is
    masked like one, and leaving SCIM authenticating forever is the same trap."""
    runtime.set_scim_token(db, "an-idp-token")
    assert runtime.scim_token_matches(db, "an-idp-token") is True
    runtime.save_settings(
        db,
        {runtime.SCIM_TOKEN_HASH_KEY: "", _clear(runtime.SCIM_TOKEN_HASH_KEY): "1"},
        sections={"SCIM provisioning"},
    )
    assert runtime.scim_token_matches(db, "an-idp-token") is False


def test_secrets_to_clear_reads_the_same_rule_it_writes(db):
    form = {_clear(SECRET): "1", _clear("slack.webhook_url"): ""}
    assert runtime.secrets_to_clear(form) == {SECRET}


def test_a_clear_marker_is_not_mistaken_for_a_secret_value():
    """It rides in the repopulation payload; it is a marker, not a credential."""
    safe = runtime.without_secret_values({SECRET: "s3cret", _clear(SECRET): "1"})
    assert safe == {_clear(SECRET): "1"}


# --------------------------------------------------------------------------- #
# Replace-and-clear is two instructions, so it is refused
# --------------------------------------------------------------------------- #
def test_replacing_and_clearing_at_once_is_refused(db):
    errors, _cleared = runtime.check_settings(
        {SECRET: "a-new-password", _clear(SECRET): "1"}, sections=SECTIONS,
    )
    assert SECRET in errors


def test_a_clear_with_an_empty_field_is_not_a_conflict(db):
    errors, _cleared = runtime.check_settings(
        {SECRET: "", _clear(SECRET): "1"}, sections=SECTIONS,
    )
    assert errors == {}


def test_the_conflicting_save_writes_nothing(admin, db):
    runtime.save_settings(db, {SECRET: "s3cret"})
    resp = admin.post("/settings", data={
        "_group": GROUP, SECRET: "a-new-password", _clear(SECRET): "1",
    }, follow_redirects=True)
    assert resp.status_code == 200
    db.expire_all()
    assert runtime.load_settings(db)[SECRET] == "s3cret"
    assert "a-new-password" not in resp.text, "the refused secret was echoed back"


def test_a_refused_conflict_does_not_come_back_armed_to_destroy(admin, db):
    """Both halves of the contradiction drop together, or only the sharp one survives.

    The typed replacement CANNOT be carried back -- it is a secret, and
    repopulation rides in the session cookie. So re-ticking the clear box from
    ``flash.fields`` preserved exactly the destructive half of a submission the
    operator had just been told to resolve: the field came back empty with
    "clear" still armed, and a reflexive second Save removed the credential they
    were trying to REPLACE.
    """
    runtime.save_settings(db, {SECRET: "s3cret"})
    resp = admin.post("/settings", data={
        "_group": GROUP, SECRET: "a-new-password", _clear(SECRET): "1",
    }, follow_redirects=True)
    box = re.search(
        r'<input type="checkbox" name="%s"[^>]*>' % re.escape(_clear(SECRET)), resp.text
    )
    assert box, "the clear box vanished from the refused form"
    assert "checked" not in box.group(0), (
        "the refusal came back with 'clear' still ticked, so pressing Save again "
        "would destroy the credential the operator meant to replace"
    )
    # And the safe direction really is safe: saving that form as it stands is a
    # no-op rather than a deletion.
    admin.post("/settings", data={"_group": GROUP, SECRET: ""}, follow_redirects=True)
    db.expire_all()
    assert runtime.load_settings(db)[SECRET] == "s3cret"


def test_the_conflict_message_does_not_promise_to_keep_what_it_discarded(db):
    """The wording is load-bearing, because following it is what the operator does.

    "Untick 'clear' to store what you typed" describes an outcome the code
    cannot produce: the typed value is stripped by ``without_secret_values``, so
    unticking and saving keeps the OLD secret and reports success -- a silent
    failed rotation, on a value nobody can verify by looking at it.
    """
    errors, _cleared = runtime.check_settings(
        {SECRET: "a-new-password", _clear(SECRET): "1"}, sections=SECTIONS,
    )
    msg = errors[SECRET]
    assert "store what you typed" not in msg, "the refusal promised to keep a discarded value"
    assert "Retype" in msg, "the refusal does not say the typed value has to be entered again"


# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #
def test_the_keep_sentinel_never_reaches_the_page(admin, db):
    runtime.save_settings(
        db, {SECRET: "zzq-smtp-pw", "freescout.api_key": "zzq-freescout-key"},
    )
    body = admin.get(f"/settings?group={GROUP}").text
    assert runtime.SECRET_PLACEHOLDER not in body, (
        "the placeholder rendered as the field's value, which is what made a "
        "stored secret look like an eight-character password"
    )
    assert "zzq-smtp-pw" not in body and "zzq-freescout-key" not in body


def test_a_stored_secret_says_it_is_unchanged_and_offers_a_clear(admin, db):
    runtime.save_settings(db, {SECRET: "s3cret"})
    body = admin.get(f"/settings?group={GROUP}").text
    assert "unchanged" in body, "nothing on the page said a credential was held"
    assert f'name="{_clear(SECRET)}"' in body, "no way to remove it"


def test_an_unset_secret_says_so_and_offers_nothing_to_clear(admin, db):
    body = admin.get(f"/settings?group={GROUP}").text
    assert f'name="{_clear(SECRET)}"' not in body, (
        "a clear box for an unset credential is a control that can do nothing"
    )


def test_clearing_through_the_form_is_audited_and_reported(admin, db):
    runtime.save_settings(db, {SECRET: "zzq-smtp-pw"})
    resp = admin.post("/settings", data={
        "_group": GROUP, SECRET: "", _clear(SECRET): "1",
    }, follow_redirects=True)
    assert resp.status_code == 200
    db.expire_all()
    assert runtime.load_settings(db)[SECRET] == ""

    rows = [a for a in db.scalars(select(m.AuditLog))
            if a.action == "settings.update"]
    assert rows, "removing a credential was not recorded"
    blob = " ".join(str(r.detail) for r in rows)
    assert SECRET in blob, "the audit row does not name what changed"
    assert "zzq-smtp-pw" not in blob, "the audit row carried the secret itself"
    # The operator cannot check this by looking at the value, and it cannot be
    # undone from this page, so the save says which credential it destroyed.
    assert SECRET in resp.text
