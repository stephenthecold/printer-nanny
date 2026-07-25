"""The /manage/people operator surface.

Route-level counterpart to test_printer_assignments.py: that file attacks the
service invariants directly, this one attacks them through HTTP, because a
route that forgets to call the service is exactly how a checked invariant stops
being checked.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import services
from central.main import app
from central.security import hash_password


def _user(db, username: str, role: m.UserRole) -> m.User:
    u = m.User(username=username, password_hash=hash_password("pw12345678"), role=role)
    db.add(u)
    db.commit()
    return u


def _login(username: str) -> TestClient:
    cli = TestClient(app)
    cli.post("/login", data={"username": username, "password": "pw12345678"},
             follow_redirects=False)
    return cli


def _client_with_printer(db, name: str, ip: str):
    c = m.Client(name=name)
    db.add(c)
    db.flush()
    s = m.Site(client_id=c.id, name=f"{name} HQ")
    db.add(s)
    db.flush()
    p = m.Printer(client_id=c.id, site_id=s.id, ip=ip, display_name=f"{name} MFP",
                  discovery_state=m.DiscoveryState.approved)
    db.add(p)
    db.commit()
    return c, p


# --------------------------- access control --------------------------------- #

def test_anonymous_is_redirected_to_login(db):
    assert TestClient(app).get("/manage/people",
                               follow_redirects=False).status_code == 303


def test_readonly_users_get_nothing_here(db):
    _user(db, "viewer", m.UserRole.client_readonly)
    _client_with_printer(db, "Acme", "10.0.0.1")
    for path, data in [
        ("/manage/people/create", {"client_id": 1, "display_name": "X"}),
        ("/manage/people/assign", {"printer_id": 1, "end_user_id": "1"}),
        ("/manage/people/groups/create", {"client_id": 1, "name": "G"}),
    ]:
        r = _login("viewer").post(path, data=data, follow_redirects=False)
        assert r.status_code == 303 and "/login" in r.headers.get("location", "")
    assert db.query(m.EndUser).count() == 0
    assert db.query(m.PrinterAssignment).count() == 0


def test_a_tech_can_manage_people(db):
    _user(db, "tech", m.UserRole.tech)
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    r = _login("tech").post("/manage/people/create",
                            data={"client_id": c.id, "display_name": "Jo"},
                            follow_redirects=False)
    assert r.status_code == 303
    assert db.query(m.EndUser).count() == 1


# --------------------------- tenancy through HTTP --------------------------- #

def test_the_route_refuses_a_cross_tenant_assignment(db):
    """The service raises; the route must catch it and write nothing."""
    _user(db, "admin", m.UserRole.admin)
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    _globex, globex_printer = _client_with_printer(db, "Globex", "10.0.1.1")
    person = m.EndUser(client_id=acme.id, email="a@acme.test")
    db.add(person)
    db.commit()

    r = _login("admin").post(
        "/manage/people/assign",
        data={"printer_id": globex_printer.id, "end_user_id": str(person.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert db.query(m.PrinterAssignment).count() == 0


def test_a_refused_cross_tenant_assignment_is_audited(db):
    """Not a form typo -- somebody submitted another customer's printer id.
    It belongs in the trail as a refusal."""
    _user(db, "admin", m.UserRole.admin)
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    _g, globex_printer = _client_with_printer(db, "Globex", "10.0.1.1")
    person = m.EndUser(client_id=acme.id, email="a@acme.test")
    db.add(person)
    db.commit()

    _login("admin").post("/manage/people/assign",
                         data={"printer_id": globex_printer.id,
                               "end_user_id": str(person.id)},
                         follow_redirects=False)
    actions = [a.action for a in db.scalars(select(m.AuditLog))]
    assert "printer_assignment.refused" in actions


def test_the_page_shows_only_the_selected_clients_staff(db):
    _user(db, "admin", m.UserRole.admin)
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    globex, _ = _client_with_printer(db, "Globex", "10.0.1.1")
    db.add(m.EndUser(client_id=acme.id, display_name="AcmePerson"))
    db.add(m.EndUser(client_id=globex.id, display_name="GlobexPerson"))
    db.commit()

    body = _login("admin").get(f"/manage/people?client_id={acme.id}").text
    assert "AcmePerson" in body
    assert "GlobexPerson" not in body


def test_a_bad_client_id_falls_back_rather_than_500ing(db):
    _user(db, "admin", m.UserRole.admin)
    _client_with_printer(db, "Acme", "10.0.0.1")
    cli = _login("admin")
    for raw in ("999999", "not-a-number", ""):
        assert cli.get(f"/manage/people?client_id={raw}").status_code == 200


# --------------------------- assignment behaviour --------------------------- #

def test_assigning_to_a_person_then_reading_it_back(db):
    _user(db, "admin", m.UserRole.admin)
    acme, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    person = m.EndUser(client_id=acme.id, display_name="Jo")
    db.add(person)
    db.commit()

    cli = _login("admin")
    cli.post("/manage/people/assign",
             data={"printer_id": printer.id, "end_user_id": str(person.id),
                   "is_default": "1"},
             follow_redirects=False)
    row = db.scalar(select(m.PrinterAssignment))
    assert row is not None and row.end_user_id == person.id and row.is_default
    body = cli.get(f"/manage/people?client_id={acme.id}").text
    assert "Acme MFP" in body and "default" in body


def test_assigning_to_both_a_person_and_a_group_is_refused(db):
    _user(db, "admin", m.UserRole.admin)
    acme, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    person = m.EndUser(client_id=acme.id, display_name="Jo")
    group = m.EndUserGroup(client_id=acme.id, name="Accounting")
    db.add_all([person, group])
    db.commit()

    _login("admin").post("/manage/people/assign",
                         data={"printer_id": printer.id,
                               "end_user_id": str(person.id),
                               "group_id": str(group.id)},
                         follow_redirects=False)
    assert db.query(m.PrinterAssignment).count() == 0


def test_assigning_to_neither_is_refused(db):
    _user(db, "admin", m.UserRole.admin)
    _acme, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    _login("admin").post("/manage/people/assign",
                         data={"printer_id": printer.id},
                         follow_redirects=False)
    assert db.query(m.PrinterAssignment).count() == 0


def test_unassigning_removes_the_row(db):
    _user(db, "admin", m.UserRole.admin)
    acme, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    person = m.EndUser(client_id=acme.id, display_name="Jo")
    db.add(person)
    db.flush()
    row = services.assign_printer(db, printer=printer, end_user=person)
    db.commit()
    rid = row.id

    _login("admin").post("/manage/people/unassign",
                         data={"assignment_id": rid}, follow_redirects=False)
    # The route commits on its own session; expire ours or we read the row back
    # out of a stale identity map and "prove" a delete that did not happen.
    db.expire_all()
    assert db.get(m.PrinterAssignment, rid) is None


def test_only_approved_printers_are_offered(db):
    """A pending device has not been accepted into the fleet; deploying a queue
    for it would hand staff hardware nobody has confirmed is theirs."""
    _user(db, "admin", m.UserRole.admin)
    acme, approved = _client_with_printer(db, "Acme", "10.0.0.1")
    pending = m.Printer(client_id=acme.id, site_id=approved.site_id, ip="10.0.0.99",
                        display_name="PendingBox",
                        discovery_state=m.DiscoveryState.pending)
    db.add(pending)
    db.add(m.EndUser(client_id=acme.id, display_name="Jo"))
    db.commit()

    body = _login("admin").get(f"/manage/people?client_id={acme.id}").text
    assert "Acme MFP" in body
    assert "PendingBox" not in body


# --------------------------- lifecycle -------------------------------------- #

def test_deactivating_keeps_the_row_and_drops_the_printers(db):
    _user(db, "admin", m.UserRole.admin)
    acme, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    person = m.EndUser(client_id=acme.id, display_name="Jo")
    db.add(person)
    db.flush()
    services.assign_printer(db, printer=printer, end_user=person)
    db.commit()

    _login("admin").post(f"/manage/people/{person.id}/active",
                         data={}, follow_redirects=False)
    db.expire_all()
    person = db.get(m.EndUser, person.id)
    assert person is not None and person.active is False
    assert db.query(m.PrinterAssignment).count() == 1
    assert services.effective_printers_for(db, person) == []


def test_reactivating_restores_them(db):
    _user(db, "admin", m.UserRole.admin)
    acme, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    person = m.EndUser(client_id=acme.id, display_name="Jo", active=False)
    db.add(person)
    db.flush()
    services.assign_printer(db, printer=printer, end_user=person)
    db.commit()

    _login("admin").post(f"/manage/people/{person.id}/active",
                         data={"active": "1"}, follow_redirects=False)
    db.expire_all()
    person = db.get(m.EndUser, person.id)
    assert person.active is True
    assert len(services.effective_printers_for(db, person)) == 1


def test_a_duplicate_email_is_rejected_with_a_message_not_a_500(db):
    _user(db, "admin", m.UserRole.admin)
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    cli = _login("admin")
    for _ in range(2):
        r = cli.post("/manage/people/create",
                     data={"client_id": acme.id, "email": "dupe@acme.test"},
                     follow_redirects=False)
        assert r.status_code == 303
    assert db.query(m.EndUser).count() == 1


def test_a_person_needs_at_least_one_identifier(db):
    _user(db, "admin", m.UserRole.admin)
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    _login("admin").post("/manage/people/create",
                         data={"client_id": acme.id, "email": "", "upn": "",
                               "display_name": "   "},
                         follow_redirects=False)
    assert db.query(m.EndUser).count() == 0


# --------------------------- groups ----------------------------------------- #

def test_group_membership_round_trips_through_the_form(db):
    _user(db, "admin", m.UserRole.admin)
    acme, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    a = m.EndUser(client_id=acme.id, display_name="A")
    b = m.EndUser(client_id=acme.id, display_name="B")
    group = m.EndUserGroup(client_id=acme.id, name="Accounting")
    db.add_all([a, b, group])
    db.flush()
    services.assign_printer(db, printer=printer, group=group)
    db.commit()

    cli = _login("admin")
    # dict-of-list, NOT a list of (key, value) tuples: this httpx version does
    # not form-encode the tuple form at all, so every field silently goes
    # missing and a handler whose params all have defaults still returns 303.
    cli.post(f"/manage/people/groups/{group.id}/members",
             data={"member_ids": [str(a.id)]}, follow_redirects=False)
    db.expire_all()
    assert len(services.effective_printers_for(db, db.get(m.EndUser, a.id))) == 1
    assert services.effective_printers_for(db, db.get(m.EndUser, b.id)) == []


def test_unticking_everyone_empties_the_group(db):
    """An empty checkbox list legitimately means "nobody" here -- this handler's
    contract is "set membership to the submitted set", so it must not be
    mistaken for a field the form failed to render."""
    _user(db, "admin", m.UserRole.admin)
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    person = m.EndUser(client_id=acme.id, display_name="A")
    group = m.EndUserGroup(client_id=acme.id, name="Accounting")
    db.add_all([person, group])
    db.flush()
    services.sync_group_members(db, group=group, end_users=[person])
    db.commit()
    assert db.query(m.EndUserGroupMember).count() == 1

    _login("admin").post(f"/manage/people/groups/{group.id}/members",
                         data={}, follow_redirects=False)
    assert db.query(m.EndUserGroupMember).count() == 0


def test_a_duplicate_group_name_is_rejected(db):
    _user(db, "admin", m.UserRole.admin)
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    cli = _login("admin")
    for _ in range(2):
        cli.post("/manage/people/groups/create",
                 data={"client_id": acme.id, "name": "Accounting"},
                 follow_redirects=False)
    assert db.query(m.EndUserGroup).count() == 1


def test_group_membership_ignores_another_tenants_person(db):
    """The id comes from a form. Submitting an outsider's id must not add them,
    and must not 500 -- it is rejected before anything is written."""
    _user(db, "admin", m.UserRole.admin)
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    globex, _ = _client_with_printer(db, "Globex", "10.0.1.1")
    outsider = m.EndUser(client_id=globex.id, display_name="Outsider")
    group = m.EndUserGroup(client_id=acme.id, name="Accounting")
    db.add_all([outsider, group])
    db.commit()

    r = _login("admin").post(f"/manage/people/groups/{group.id}/members",
                             data={"member_ids": [str(outsider.id)]},
                             follow_redirects=False)
    assert r.status_code == 303
    assert db.query(m.EndUserGroupMember).count() == 0


# --------------------------- rendering -------------------------------------- #

def test_hostile_names_are_escaped(db):
    """Staff names may arrive from a directory sync later; treat them as hostile
    now so the escaping is already proven when they do."""
    _user(db, "admin", m.UserRole.admin)
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    db.add(m.EndUser(client_id=acme.id,
                     display_name="<script>alert(1)</script>"))
    db.add(m.EndUserGroup(client_id=acme.id, name="<img src=x onerror=alert(2)>"))
    db.commit()

    body = _login("admin").get(f"/manage/people?client_id={acme.id}").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
    assert "<img src=x onerror=alert(2)>" not in body


def test_the_page_links_to_the_operator_users_page_to_keep_them_distinct(db):
    _user(db, "admin", m.UserRole.admin)
    _client_with_printer(db, "Acme", "10.0.0.1")
    body = _login("admin").get("/manage/people").text
    assert "/manage/users" in body


def test_the_page_survives_having_no_clients(db):
    _user(db, "admin", m.UserRole.admin)
    assert _login("admin").get("/manage/people").status_code == 200
