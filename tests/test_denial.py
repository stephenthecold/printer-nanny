""""You may not do this" must not be answered with the sign-in form.

``_manager`` and ``_admin`` return None for two entirely different situations --
nobody is signed in, and somebody IS signed in who may not do this -- and all 93
guard call sites collapsed them into ``_redirect("/login")``.

The result was a loop with no exit and no explanation: a technician follows a
link the application itself rendered, is shown the sign-in page, reads it as an
expired session, signs in again, succeeds, lands back where they started, and
follows the same link to the same page. Nothing ever says the answer is no.

It also loses work -- a POST answered with the login form has discarded the
submission with no indication of whether it applied -- and it lies to machines:
a 303 to /login tells a script, a monitor and the browser's history that the
request was fine.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central.main import app
from central.security import hash_password

#: Routes whose guard is ``_admin``: a tech is authenticated but not permitted.
ADMIN_ONLY = ["/manage/users", "/manage/billing", "/manage/events"]

#: Routes whose guard is ``_manager``: a client_readonly user is not permitted.
MANAGER_ONLY = ["/manage/agents", "/manage/people", "/manage/machines"]


def _signed_in(db, role: m.UserRole, username: str, client_id=None) -> TestClient:
    db.add(m.User(username=username, password_hash=hash_password("pw12345678"),
                  role=role, client_id=client_id))
    db.commit()
    http = TestClient(app)
    resp = http.post("/login", data={"username": username, "password": "pw12345678"},
                     follow_redirects=False)
    assert resp.status_code == 303
    return http


# --------------------------------------------------------------------------- #
# Anonymous keeps the redirect
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ADMIN_ONLY + MANAGER_ONLY)
def test_an_anonymous_visitor_is_still_sent_to_sign_in(path):
    """There IS a real action to take, and the login page is where it is taken."""
    http = TestClient(app)
    resp = http.get(path, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# --------------------------------------------------------------------------- #
# Authenticated-but-unauthorised gets a 403
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ADMIN_ONLY)
def test_a_tech_is_refused_not_asked_to_sign_in_again(db, path):
    http = _signed_in(db, m.UserRole.tech, "tech")
    resp = http.get(path, follow_redirects=False)
    assert resp.status_code == 403, (
        f"{path} answered {resp.status_code} -- a signed-in user was not told no"
    )
    assert "Not permitted" in resp.text


@pytest.mark.parametrize("path", MANAGER_ONLY)
def test_a_readonly_customer_is_refused(db, path):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    http = _signed_in(db, m.UserRole.client_readonly, "cust", client_id=client.id)
    resp = http.get(path, follow_redirects=False)
    assert resp.status_code == 403
    assert "Not permitted" in resp.text


def test_the_refusal_page_is_not_a_dead_end(db):
    """A refusal whose only exit is the back button strands the operator exactly
    the way /manage/people used to."""
    http = _signed_in(db, m.UserRole.tech, "tech")
    body = http.get("/manage/users", follow_redirects=False).text
    assert 'href="/"' in body, "no way off the refusal page"
    # The nav is what makes it navigable; base.html gates all of it on `user`.
    assert "/manage/agents" in body, "the refusal page rendered without navigation"


def test_the_refusal_names_who_you_are_signed_in_as(db):
    """Because the failure mode being fixed is an operator who cannot tell
    whether they are signed in at all."""
    http = _signed_in(db, m.UserRole.tech, "tech")
    body = http.get("/manage/users", follow_redirects=False).text
    assert "tech" in body


def test_a_refused_post_does_not_look_like_a_login_prompt(db):
    """A POST answered with the sign-in form has thrown the submission away and
    says nothing about whether it applied."""
    http = _signed_in(db, m.UserRole.tech, "tech")
    resp = http.post("/manage/users", data={"username": "x", "password": "y",
                                            "role": "admin"}, follow_redirects=False)
    assert resp.status_code == 403
    assert db.scalar(m.User.__table__.select().where(m.User.username == "x")) is None


# --------------------------------------------------------------------------- #
# The distinction is structural, not per-route
# --------------------------------------------------------------------------- #
def test_no_role_guard_answers_with_a_redirect():
    """The idiom must not creep back, in either of its two forms.

    Both were in the tree: ``_redirect("/login")``, which reads as an expired
    session, and ``_redirect("/login" if _user(...) is None else "/")``, which
    bounces a signed-in operator to the dashboard with no explanation at all.
    The second is better and still not an answer, so the rule is simply that a
    role guard does not reply with a redirect -- it replies with ``_deny``,
    which decides between the two cases in one place.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "central" / "dashboard"
    guard_call = re.compile(r"=\s*_(manager|admin)\(")
    inline_guard = re.compile(r"if\s+_(manager|admin)\(.*is None:")
    named_guard = re.compile(r"if\s+(\w+)\s+is None:")
    offenders = []
    for path in sorted(root.glob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "return _redirect(" not in line or i == 0:
                continue
            prev = lines[i - 1]
            # Only the redirect that IS the guard's body counts. A "not found,
            # go back to the list" redirect a couple of lines later is a
            # different thing entirely, and an earlier version of this test
            # flagged fifteen of them.
            if inline_guard.search(prev):
                offenders.append(f"{path.name}:{i + 1}: {line.strip()}")
                continue
            named = named_guard.search(prev)
            if named and i >= 2:
                assigned = lines[i - 2]
                if guard_call.search(assigned) and named.group(1) in assigned:
                    offenders.append(f"{path.name}:{i + 1}: {line.strip()}")
    assert not offenders, (
        "role-guard refusals still answering with a redirect: %s" % offenders
    )
