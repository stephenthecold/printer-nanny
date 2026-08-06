"""Two defects a UI audit found, and the guards that keep them fixed.

Both are the same shape: a screen that renders successfully while being wrong,
so nothing in the suite noticed. Neither is visible to a test that only asserts
a 200.

1. ``/manage/people`` rendered with NO navigation at all. ``_tpl`` reads
   ``ctx.get("user")`` but never injects it, and ``base.html`` wraps the nav, the
   skip link, the account chip and Logout in ``{% if user %}`` -- so the two
   ``_tpl`` calls in ``people.py`` that omitted ``user=`` produced a page with no
   way out and no way to sign out.

2. The printer edit form could not express SNMP **v3**, so for a v3 row neither
   option was ``selected``, the browser posted the first one, and the route
   assigned it unconditionally. Editing an asset tag silently rewrote the
   device's SNMP version to v2c -- changing how the agent polls it and flipping
   it from encrypted to cleartext on the security posture report.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central.main import app
from central.security import hash_password


def _client_with_login(db, role: m.UserRole, username: str) -> TestClient:
    db.add(m.User(username=username, password_hash=hash_password("pw12345678"), role=role))
    db.commit()
    http = TestClient(app)
    resp = http.post(
        "/login",
        data={"username": username, "password": "pw12345678"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text[:300]
    return http


def _nav_links(html: str) -> list:
    """Hrefs inside the primary nav. The nav is what `{% if user %}` gates."""
    nav = re.search(r"<nav\b.*?</nav>", html, re.S | re.I)
    if not nav:
        return []
    return re.findall(r'href="([^"]+)"', nav.group(0))


# --------------------------------------------------------------------------- #
# 1. The nav-less page
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", [m.UserRole.admin, m.UserRole.tech])
def test_people_page_renders_its_navigation(db, role):
    """The regression. Rendering 200 is not the property under test -- it did
    that before. The property is that the operator can leave the page."""
    http = _client_with_login(db, role, f"op-{role.value}")
    resp = http.get("/manage/people")
    assert resp.status_code == 200, resp.text[:300]
    links = _nav_links(resp.text)
    assert links, "/manage/people rendered with no navigation at all"
    # Sign-out is the specific thing whose absence stranded the operator.
    assert any("logout" in href.lower() for href in links), links


def test_people_page_nav_matches_another_managed_page(db):
    """A weaker page-specific nav would pass the test above and still be wrong."""
    http = _client_with_login(db, m.UserRole.admin, "admin")
    people = set(_nav_links(http.get("/manage/people").text))
    manage = set(_nav_links(http.get("/manage").text))
    assert people == manage, sorted(manage ^ people)


# --------------------------------------------------------------------------- #
# 2. Nav and authorization must agree
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/manage/people", "/manage/machines", "/manage/definitions"])
def test_a_tech_is_linked_to_every_page_a_tech_may_open(db, path):
    """These three routes gate on ``_manager`` (admin OR tech) while the nav
    gated on admin, so a tech could reach them only by typing the URL -- and the
    nav then highlighted *Manage*, misreporting where they were."""
    http = _client_with_login(db, m.UserRole.tech, "tech")
    assert http.get(path).status_code == 200, f"{path} refused a tech"
    assert path in _nav_links(http.get("/manage").text), (
        f"{path} is reachable by a tech but not linked for one"
    )


def test_a_readonly_customer_is_linked_to_none_of_them(db):
    """The other direction: the nav must not offer what the route refuses."""
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    db.add(m.User(username="cust", password_hash=hash_password("pw12345678"),
                  role=m.UserRole.client_readonly, client_id=client.id))
    db.commit()
    http = TestClient(app)
    http.post("/login", data={"username": "cust", "password": "pw12345678"},
              follow_redirects=False)
    links = _nav_links(http.get("/portal").text)
    for path in ("/manage/people", "/manage/machines", "/manage/definitions"):
        assert path not in links, f"{path} offered to a client_readonly user"


# --------------------------------------------------------------------------- #
# 3. The silent SNMP downgrade
# --------------------------------------------------------------------------- #
def _v3_printer(db):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.5",
        snmp_version="3", snmp_community="public",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.commit()
    return printer, site


def test_the_edit_form_can_express_v3(db):
    """A <select> that cannot express the row's value makes the browser post the
    first option. Listing v3 is what stops the downgrade at source."""
    printer, _ = _v3_printer(db)
    http = _client_with_login(db, m.UserRole.admin, "admin")
    body = http.get(f"/manage/printers/{printer.id}/edit").text
    select = re.search(r'<select[^>]*name="snmp_version".*?</select>', body, re.S)
    assert select, "no snmp_version select on the edit form"
    assert 'value="3"' in select.group(0), "v3 is not offered"
    # And the stored value must come back pre-selected, or the post still loses it.
    assert re.search(r'value="3"[^>]*selected', select.group(0)), (
        "v3 is offered but not selected for a v3 printer"
    )


def test_editing_an_unrelated_field_keeps_v3(db):
    """The regression, end to end: change the asset tag, keep the SNMP version."""
    printer, site = _v3_printer(db)
    http = _client_with_login(db, m.UserRole.admin, "admin")
    resp = http.post(
        f"/manage/printers/{printer.id}",
        data={
            "site_id": str(site.id), "ip": "10.0.0.5", "asset_tag": "ASSET-42",
            "snmp_version": "3", "snmp_community": "public",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text[:300]
    db.expire_all()
    refreshed = db.get(m.Printer, printer.id)
    assert refreshed.asset_tag == "ASSET-42"
    assert refreshed.snmp_version == "3", "editing an unrelated field downgraded SNMP v3"


def test_an_unrecognised_snmp_version_is_refused(db):
    """It decides how we talk to the device and how the posture report grades it,
    so a value we do not understand must not overwrite one we do."""
    printer, site = _v3_printer(db)
    http = _client_with_login(db, m.UserRole.admin, "admin")
    resp = http.post(
        f"/manage/printers/{printer.id}",
        data={
            "site_id": str(site.id), "ip": "10.0.0.5",
            "snmp_version": "9z", "snmp_community": "public",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(m.Printer, printer.id).snmp_version == "3"


def test_the_refusal_redirect_goes_somewhere_that_answers_a_get(db):
    """``/manage/printers/{id}`` is POST-only, so the old refusal target replied
    405 and threw the form away."""
    printer, site = _v3_printer(db)
    http = _client_with_login(db, m.UserRole.admin, "admin")
    resp = http.post(
        f"/manage/printers/{printer.id}",
        data={"site_id": str(site.id), "ip": "10.0.0.5", "snmp_version": "9z"},
        follow_redirects=False,
    )
    target = resp.headers["location"]
    assert http.get(target).status_code == 200, f"{target} does not answer a GET"
