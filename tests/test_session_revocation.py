"""Logging out has to actually log you out.

Sessions here are Starlette **signed cookies with no server-side store**. Nothing
can delete one: ``request.session.clear()`` sends a delete-cookie header, and the
signed value the browser already holds keeps verifying for the full ``max_age``
(12 hours). So every "revoke" action was advisory.

Measured before ``User.session_epoch`` existed, replaying one captured cookie in
a fresh client:

    | action                            | /manage/users after |
    |-----------------------------------|---------------------|
    | user clicked logout               | 200                 |
    | user changed their own password   | 200                 |
    | admin reset their password        | 200                 |
    | role demoted admin -> tech        | 303 (already safe)  |
    | account deactivated               | 303 (already safe)  |

Note which half was already safe. Role and `active` are re-read from the row on
every request, so those took effect immediately -- while the three *credential
rotation* actions did not. That is exactly backwards: those are the ones you
take when you believe a session is already in the wrong hands.

These tests capture a cookie, perform the action, and replay the cookie in a
SEPARATE client. Asserting through the original client would prove nothing --
that one is holding the fresh cookie the action just set.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from central import models as m
from central.main import app
from central.security import hash_password

PROTECTED = "/manage/users"


def _user(db, username="ops", password="correct-horse", role=m.UserRole.admin):
    user = m.User(username=username, password_hash=hash_password(password), role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(username="ops", password="correct-horse") -> TestClient:
    cli = TestClient(app)
    resp = cli.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert resp.status_code == 303, "fixture login failed; the rest proves nothing"
    return cli


def _replay(cli: TestClient) -> int:
    """Status a stolen copy of this client's cookie gets, in a fresh client."""
    thief = TestClient(app)
    for name, value in cli.cookies.items():
        thief.cookies.set(name, value)
    return thief.get(PROTECTED, follow_redirects=False).status_code


def test_a_captured_cookie_works_until_something_revokes_it(db):
    """The premise. If this fails, the other tests are measuring nothing."""
    _user(db)
    cli = _login()
    assert _replay(cli) == 200


def test_logging_out_revokes_the_cookie(db):
    _user(db)
    cli = _login()
    assert _replay(cli) == 200  # live before
    cli.get("/logout", follow_redirects=False)
    assert _replay(cli) == 303, "the cookie survived logout"


def test_changing_your_own_password_revokes_every_other_session(db):
    """The action you take when you think someone else has your password."""
    user = _user(db)
    stolen = _login()
    assert _replay(stolen) == 200

    owner = _login()
    resp = owner.post(
        "/account/password",
        data={
            "current_password": "correct-horse",
            "new_password": "battery-staple-9",
            "confirm_password": "battery-staple-9",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(user)

    assert _replay(stolen) == 303, "the old session outlived the password"
    # ...and the person who did the rotation is not logged out by their own act.
    assert owner.get(PROTECTED, follow_redirects=False).status_code == 200


def test_an_admin_reset_revokes_the_targets_sessions(db):
    target = _user(db, username="staff", password="old-password-1",
                   role=m.UserRole.tech)
    _user(db, username="boss", password="admin-password-1")
    stolen = _login("staff", "old-password-1")
    assert stolen.get("/", follow_redirects=False).status_code == 200

    boss = _login("boss", "admin-password-1")
    resp = boss.post(
        f"/manage/users/{target.id}/reset-password",
        data={"new_password": "brand-new-password-2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(target)
    assert target.must_change_password, (
        "an admin-chosen password reached its owner over chat or a ticket and "
        "stays readable there; it has to be rotated on first use"
    )
    assert stolen.get("/", follow_redirects=False).status_code == 303


def test_a_session_minted_before_the_column_existed_still_works(db):
    """Upgrade behaviour, and it is a deliberate choice.

    A cookie carrying no epoch is treated as current. The alternative logs out
    every operator on upgrade to fix a problem none of them has yet, and the
    first rotation after upgrade stamps it properly anyway.
    """
    from central.deps import SESSION_EPOCH_KEY, session_is_current

    user = _user(db)
    assert session_is_current({}, user) is True
    assert session_is_current({SESSION_EPOCH_KEY: 0}, user) is True
    user.session_epoch = 1
    assert session_is_current({SESSION_EPOCH_KEY: 0}, user) is False
