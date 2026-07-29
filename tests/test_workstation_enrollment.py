"""Workstation enrollment: the API a print client speaks, and the page that
manages it.

The API half is where the security properties live, so most of this file is
about what enrollment must NOT let a caller do -- choose a tenant, read another
client's fleet, distinguish an unknown key from a revoked one, or keep working
after being retired.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import services
from central.main import app
from central.security import (
    generate_enroll_key,
    hash_api_key,
    hash_enroll_key,
    hash_password,
)


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


def _enroll_key(db, client: m.Client, label: str = "MSI") -> str:
    key = generate_enroll_key()
    db.add(m.WorkstationEnrollKey(
        client_id=client.id, key_hash=hash_enroll_key(key), label=label))
    db.commit()
    return key


def _enroll(cli: TestClient, key: str, uid: str, name: str = "PC"):
    return cli.post("/api/v1/workstations/enroll",
                    json={"enroll_key": key, "machine_uid": uid, "name": name})


# ----------------------------- enrollment ---------------------------------- #


def test_enrolling_mints_a_machine_and_its_own_key(db):
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)

    r = _enroll(TestClient(app), key, "GUID-A", "DESK-01")
    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True
    assert body["client_id"] == c.id
    assert body["api_key"]

    machine = db.get(m.Machine, body["machine_id"])
    db.refresh(machine)
    assert machine.name == "DESK-01"
    # Stored as a hash, never the key itself.
    assert machine.api_key_hash == hash_api_key(body["api_key"])
    assert body["api_key"] not in (machine.api_key_hash or "")


def test_the_caller_cannot_choose_its_tenant(db):
    """client_id comes from the key, never the request.

    A bearer credential whose holder can name a tenant lands them inside another
    customer's fleet -- the same rule as AgentClaimToken.site_id.
    """
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    globex, _ = _client_with_printer(db, "Globex", "10.9.9.1")
    key = _enroll_key(db, acme)

    r = TestClient(app).post("/api/v1/workstations/enroll", json={
        "enroll_key": key, "machine_uid": "GUID-A", "name": "PC",
        # Hostile extras -- these must be ignored, not honoured.
        "client_id": globex.id, "clientId": globex.id,
    })
    assert r.status_code == 200
    assert r.json()["client_id"] == acme.id


def test_unknown_and_revoked_keys_are_indistinguishable(db):
    """Different answers would let a holder of one key probe for others."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    row = db.scalar(select(m.WorkstationEnrollKey))
    row.revoked_at = datetime.now(timezone.utc)
    db.commit()

    cli = TestClient(app)
    revoked = _enroll(cli, key, "GUID-A")
    unknown = _enroll(cli, "pnw_nonexistent", "GUID-B")
    assert revoked.status_code == unknown.status_code == 401
    assert revoked.json() == unknown.json()


def test_reenrolling_rotates_the_key_and_does_not_duplicate(db):
    """A second row would be the same PC twice, one holding the assignments."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    cli = TestClient(app)

    first = _enroll(cli, key, "GUID-A").json()
    second = _enroll(cli, key, "GUID-A").json()

    assert second["created"] is False
    assert second["machine_id"] == first["machine_id"]
    assert second["api_key"] != first["api_key"]
    assert db.scalar(select(m.Machine).where(m.Machine.machine_uid == "GUID-A"))
    assert len(list(db.scalars(select(m.Machine).where(
        m.Machine.machine_uid == "GUID-A")))) == 1

    # The old credential must stop working, or rotation is decorative.
    r = cli.get(f"/api/v1/workstations/{first['machine_id']}/assignments",
                headers={"Authorization": f"Bearer {first['api_key']}"})
    assert r.status_code == 401


def test_two_clients_can_both_have_the_same_machine_uid(db):
    """Uniqueness is per tenant. A GUID collision across customers is not an
    error the second customer should experience."""
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    globex, _ = _client_with_printer(db, "Globex", "10.9.9.1")
    cli = TestClient(app)

    a = _enroll(cli, _enroll_key(db, acme), "SAME-UID").json()
    g = _enroll(cli, _enroll_key(db, globex), "SAME-UID").json()
    assert a["machine_id"] != g["machine_id"]
    assert a["client_id"] == acme.id and g["client_id"] == globex.id


# ----------------------------- assignments --------------------------------- #


def test_assignments_need_the_machines_own_key(db):
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    enrolled = _enroll(TestClient(app), _enroll_key(db, c), "GUID-A").json()
    mid = enrolled["machine_id"]

    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "nonsense"}):
        assert TestClient(app).get(
            f"/api/v1/workstations/{mid}/assignments", headers=headers
        ).status_code == 401


def test_the_enroll_key_is_not_a_read_credential(db):
    """The whole point of the split: losing the installer's key must not
    disclose a fleet."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    mid = _enroll(TestClient(app), key, "GUID-A").json()["machine_id"]

    r = TestClient(app).get(f"/api/v1/workstations/{mid}/assignments",
                            headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 401


def test_a_machine_cannot_read_another_machines_assignments(db):
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    cli = TestClient(app)
    a = _enroll(cli, key, "GUID-A").json()
    b = _enroll(cli, key, "GUID-B").json()

    r = cli.get(f"/api/v1/workstations/{a['machine_id']}/assignments",
                headers={"Authorization": f"Bearer {b['api_key']}"})
    assert r.status_code == 401


def test_login_screen_still_gets_the_machines_own_printers(db):
    """No signed-in user is not an error -- it is the shared-terminal case."""
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    enrolled = _enroll(TestClient(app), _enroll_key(db, c), "GUID-A").json()
    db.add(m.PrinterAssignment(printer_id=printer.id,
                               machine_id=enrolled["machine_id"], is_default=True))
    db.commit()

    r = TestClient(app).get(
        f"/api/v1/workstations/{enrolled['machine_id']}/assignments",
        headers={"Authorization": f"Bearer {enrolled['api_key']}"})
    body = r.json()
    assert body["resolved_for"] is None
    assert [p["printer_id"] for p in body["printers"]] == [printer.id]
    assert body["default_printer_id"] == printer.id


def test_a_users_own_default_beats_the_machines(db):
    """The agreed precedence, asserted through HTTP rather than the service."""
    c, machine_printer = _client_with_printer(db, "Acme", "10.0.0.1")
    own = m.Printer(client_id=c.id, site_id=machine_printer.site_id, ip="10.0.0.2",
                    display_name="Jo's", discovery_state=m.DiscoveryState.approved)
    person = m.EndUser(client_id=c.id, email="jo@acme.test", display_name="Jo")
    db.add_all([own, person])
    db.flush()
    enrolled = _enroll(TestClient(app), _enroll_key(db, c), "GUID-A").json()
    db.add_all([
        m.PrinterAssignment(printer_id=machine_printer.id,
                            machine_id=enrolled["machine_id"], is_default=True),
        m.PrinterAssignment(printer_id=own.id, end_user_id=person.id, is_default=True),
    ])
    db.commit()

    r = TestClient(app).get(
        f"/api/v1/workstations/{enrolled['machine_id']}/assignments",
        params={"user": "jo@acme.test"},
        headers={"Authorization": f"Bearer {enrolled['api_key']}"})
    body = r.json()
    assert body["resolved_for"] == "jo@acme.test"
    assert body["default_printer_id"] == own.id
    assert len(body["printers"]) == 2


def test_a_user_from_another_client_does_not_resolve(db):
    """Two customers each having a jsmith is normal; matching must be scoped."""
    acme, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    globex, other = _client_with_printer(db, "Globex", "10.9.9.1")
    stranger = m.EndUser(client_id=globex.id, email="jsmith@globex.test",
                         display_name="J Smith")
    db.add(stranger)
    db.flush()
    db.add(m.PrinterAssignment(printer_id=other.id, end_user_id=stranger.id,
                               is_default=True))
    enrolled = _enroll(TestClient(app), _enroll_key(db, acme), "GUID-A").json()
    db.add(m.PrinterAssignment(printer_id=printer.id,
                               machine_id=enrolled["machine_id"]))
    db.commit()

    r = TestClient(app).get(
        f"/api/v1/workstations/{enrolled['machine_id']}/assignments",
        params={"user": "jsmith@globex.test"},
        headers={"Authorization": f"Bearer {enrolled['api_key']}"})
    body = r.json()
    assert body["resolved_for"] is None, "another client's user must not resolve"
    assert [p["printer_id"] for p in body["printers"]] == [printer.id]


def test_retiring_a_machine_stops_it_on_its_next_request(db):
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    enrolled = _enroll(TestClient(app), _enroll_key(db, c), "GUID-A").json()
    machine = db.get(m.Machine, enrolled["machine_id"])
    machine.active = False
    db.commit()

    assert TestClient(app).get(
        f"/api/v1/workstations/{enrolled['machine_id']}/assignments",
        headers={"Authorization": f"Bearer {enrolled['api_key']}"}
    ).status_code == 401


def test_checkin_refreshes_liveness_and_name(db):
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    enrolled = _enroll(TestClient(app), _enroll_key(db, c), "GUID-A", "OLD-NAME").json()

    r = TestClient(app).post(
        f"/api/v1/workstations/{enrolled['machine_id']}/checkin",
        json={"name": "NEW-NAME"},
        headers={"Authorization": f"Bearer {enrolled['api_key']}"})
    assert r.status_code == 200

    machine = db.get(m.Machine, enrolled["machine_id"])
    db.refresh(machine)
    assert machine.name == "NEW-NAME"
    assert machine.last_seen_at is not None
    # The GUID identifies the machine; the rename must not have forked a row.
    assert machine.machine_uid == "GUID-A"


def test_a_hostile_machine_name_is_stored_as_text(db):
    """Names arrive from a device on a customer LAN and land in the dashboard."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    nasty = "<script>alert(1)</script>" + "A" * 400
    enrolled = _enroll(TestClient(app), _enroll_key(db, c), "GUID-A", nasty).json()

    machine = db.get(m.Machine, enrolled["machine_id"])
    db.refresh(machine)
    assert len(machine.name) <= 255, "length-capped so a device cannot push a novel"
    assert machine.name.startswith("<script>")  # stored raw, escaped at render


# ------------------------------ the UI ------------------------------------- #


def test_machines_page_needs_a_manager(db):
    _user(db, "readonly", m.UserRole.client_readonly)
    r = _login("readonly").get("/manage/machines", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert "/login" in r.headers.get("location", "")


def test_anonymous_cannot_reach_the_machines_page(db):
    r = TestClient(app).get("/manage/machines", follow_redirects=False)
    assert r.status_code in (302, 303, 307)


def test_minting_a_key_shows_it_once_and_stores_only_a_hash(db):
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    _user(db, "admin1", m.UserRole.admin)
    cli = _login("admin1")

    cli.post("/manage/machines/keys/create",
             data={"client_id": c.id, "label": "Acme MSI"}, follow_redirects=False)
    page = cli.get("/manage/machines").text
    row = db.scalar(select(m.WorkstationEnrollKey))
    assert row is not None and row.label == "Acme MSI"
    assert "pnw_" in page, "the key is shown once, right after minting"

    # ...and not on the next load.
    assert "pnw_" not in cli.get("/manage/machines").text

    # What is stored must not be the key that was displayed.
    import re
    shown = re.search(r"pnw_[A-Za-z0-9_\-]+", page).group(0)
    assert row.key_hash == hash_enroll_key(shown)
    assert row.key_hash != shown


def test_revoking_a_key_leaves_enrolled_machines_working(db):
    """The property that makes rotation routine rather than an outage."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    _user(db, "admin1", m.UserRole.admin)
    key = _enroll_key(db, c)
    enrolled = _enroll(TestClient(app), key, "GUID-A").json()

    row = db.scalar(select(m.WorkstationEnrollKey))
    _login("admin1").post(f"/manage/machines/keys/{row.id}/revoke",
                          follow_redirects=False)
    db.refresh(row)
    assert row.revoked_at is not None
    assert _enroll(TestClient(app), key, "GUID-NEW").status_code == 401
    assert TestClient(app).get(
        f"/api/v1/workstations/{enrolled['machine_id']}/assignments",
        headers={"Authorization": f"Bearer {enrolled['api_key']}"}
    ).status_code == 200


def test_assigning_across_tenants_is_refused_and_audited(db):
    """Same invariant as the People page, through the Machines routes."""
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    globex, other = _client_with_printer(db, "Globex", "10.9.9.1")
    _user(db, "admin1", m.UserRole.admin)
    enrolled = _enroll(TestClient(app), _enroll_key(db, acme), "GUID-A").json()

    _login("admin1").post("/manage/machines/assign", data={
        "machine_id": enrolled["machine_id"], "printer_id": other.id,
    }, follow_redirects=False)

    assert db.scalar(select(m.PrinterAssignment).where(
        m.PrinterAssignment.machine_id == enrolled["machine_id"])) is None
    refusal = db.scalar(select(m.AuditLog).where(
        m.AuditLog.action == "printer_assignment.refused"))
    assert refusal is not None


def test_the_shared_terminal_toggle_round_trips(db):
    """Posted as an explicit value, not read as checkbox presence -- an
    unchecked box posts nothing, and a handler that reads it directly cannot
    tell 'off' from 'this form didn't carry the field'."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    _user(db, "admin1", m.UserRole.admin)
    enrolled = _enroll(TestClient(app), _enroll_key(db, c), "GUID-A").json()
    mid = enrolled["machine_id"]
    cli = _login("admin1")

    cli.post(f"/manage/machines/{mid}/default-wins",
             data={"default_wins": "1"}, follow_redirects=False)
    machine = db.get(m.Machine, mid)
    db.refresh(machine)
    assert machine.default_wins is True

    cli.post(f"/manage/machines/{mid}/default-wins",
             data={"default_wins": "0"}, follow_redirects=False)
    db.refresh(machine)
    assert machine.default_wins is False


def test_a_hostile_machine_name_is_escaped_in_the_page(db):
    """SNMP/PJL/device strings land in the dashboard; this one arrives from the
    workstation itself and must render as text."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    _user(db, "admin1", m.UserRole.admin)
    _enroll(TestClient(app), _enroll_key(db, c), "GUID-A", "<script>alert(1)</script>")

    page = _login("admin1").get("/manage/machines").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_the_page_renders_machines_and_their_assignments(db):
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    _user(db, "admin1", m.UserRole.admin)
    enrolled = _enroll(TestClient(app), _enroll_key(db, c), "GUID-A", "DESK-01").json()
    db.add(m.PrinterAssignment(printer_id=printer.id,
                               machine_id=enrolled["machine_id"], is_default=True))
    db.commit()

    page = _login("admin1").get("/manage/machines").text
    assert "DESK-01" in page
    assert "Acme MFP" in page
    assert "Machines" in page


def test_services_refuse_a_cross_tenant_machine_assignment(db):
    """The service layer directly, since the route is only one caller."""
    acme, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    globex, other = _client_with_printer(db, "Globex", "10.9.9.1")
    machine = m.Machine(client_id=acme.id, machine_uid="GUID-A", name="PC")
    db.add(machine)
    db.commit()

    import pytest
    with pytest.raises(services.TenancyError):
        services.assign_printer(db, printer=other, machine=machine)


def test_a_signed_in_user_is_matched_by_upn(db):
    """Windows hands the service a UPN, not an email, and the two drift apart:
    a mailbox move changes the email while the UPN stays put."""
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    person = m.EndUser(client_id=c.id, email="new.address@acme.test",
                       upn="jo@acme.local", display_name="Jo")
    db.add(person)
    db.flush()
    db.add(m.PrinterAssignment(printer_id=printer.id, end_user_id=person.id,
                               is_default=True))
    enrolled = _enroll(TestClient(app), _enroll_key(db, c), "GUID-A").json()
    db.commit()

    r = TestClient(app).get(
        f"/api/v1/workstations/{enrolled['machine_id']}/assignments",
        params={"user": "jo@acme.local"},
        headers={"Authorization": f"Bearer {enrolled['api_key']}"})
    body = r.json()
    assert body["resolved_for"] == "jo@acme.local"
    assert body["default_printer_id"] == printer.id


def test_email_still_matches_when_no_upn_is_stored(db):
    """Tenants that never populated a UPN must keep working."""
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    person = m.EndUser(client_id=c.id, email="jo@acme.test", display_name="Jo")
    db.add(person)
    db.flush()
    db.add(m.PrinterAssignment(printer_id=printer.id, end_user_id=person.id))
    enrolled = _enroll(TestClient(app), _enroll_key(db, c), "GUID-A").json()
    db.commit()

    r = TestClient(app).get(
        f"/api/v1/workstations/{enrolled['machine_id']}/assignments",
        params={"user": "jo@acme.test"},
        headers={"Authorization": f"Bearer {enrolled['api_key']}"})
    assert r.json()["resolved_for"] == "jo@acme.test"


def test_upn_matching_is_still_tenant_scoped(db):
    """The scoping rule must not have been widened by adding a second column."""
    acme, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    globex, other = _client_with_printer(db, "Globex", "10.9.9.1")
    stranger = m.EndUser(client_id=globex.id, email="x@globex.test",
                         upn="jsmith@globex.local", display_name="J")
    db.add(stranger)
    db.flush()
    db.add(m.PrinterAssignment(printer_id=other.id, end_user_id=stranger.id))
    enrolled = _enroll(TestClient(app), _enroll_key(db, acme), "GUID-A").json()
    db.commit()

    r = TestClient(app).get(
        f"/api/v1/workstations/{enrolled['machine_id']}/assignments",
        params={"user": "jsmith@globex.local"},
        headers={"Authorization": f"Bearer {enrolled['api_key']}"})
    assert r.json()["resolved_for"] is None
    assert r.json()["printers"] == []


# ------------------------ re-imaged PCs keep their printers ----------------- #


def _seen(db, machine, minutes_ago: int) -> None:
    machine.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    db.commit()


def test_a_reimaged_pc_keeps_its_printers(db):
    """The whole point: ProgramData is wiped, a fresh GUID arrives, and the PC
    reclaims the record (and therefore the assignments) it had before."""
    c, printer = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    first = _enroll(TestClient(app), key, "GUID-BEFORE", "DESK-01").json()
    db.add(m.PrinterAssignment(printer_id=printer.id,
                               machine_id=first["machine_id"], is_default=True))
    db.commit()
    _seen(db, db.get(m.Machine, first["machine_id"]), minutes_ago=999)

    # Re-imaged: same computer name, brand-new GUID.
    second = _enroll(TestClient(app), key, "GUID-AFTER", "DESK-01").json()

    assert second["adopted"] is True
    assert second["created"] is False
    assert second["machine_id"] == first["machine_id"], "same record, so same printers"

    machine = db.get(m.Machine, first["machine_id"])
    db.refresh(machine)
    assert machine.machine_uid == "GUID-AFTER", "identity moved to the new install"

    # And it really can see them.
    body = TestClient(app).get(
        f"/api/v1/workstations/{second['machine_id']}/assignments",
        headers={"Authorization": f"Bearer {second['api_key']}"}).json()
    assert [p["printer_id"] for p in body["printers"]] == [printer.id]


def test_adoption_preserves_the_shared_terminal_flag(db):
    """Assignments hang off the row id, so does everything else an operator set.
    Preserving the row is what preserves the configuration."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    first = _enroll(TestClient(app), key, "GUID-BEFORE", "KIOSK-01").json()
    machine = db.get(m.Machine, first["machine_id"])
    machine.default_wins = True
    db.commit()
    _seen(db, machine, minutes_ago=999)

    _enroll(TestClient(app), key, "GUID-AFTER", "KIOSK-01")
    db.refresh(machine)
    assert machine.default_wins is True


def test_a_live_machine_is_never_adopted(db):
    """A recent check-in means the PC is ALIVE, so a name match is a collision,
    not a re-image. Adopting would rotate a working machine's credential out
    from under it and the two would fight over the row forever."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    first = _enroll(TestClient(app), key, "GUID-LIVE", "DESK-01").json()
    _seen(db, db.get(m.Machine, first["machine_id"]), minutes_ago=1)

    second = _enroll(TestClient(app), key, "GUID-OTHER", "DESK-01").json()
    assert second["adopted"] is False
    assert second["created"] is True
    assert second["machine_id"] != first["machine_id"]

    # The live machine's credential still works -- it was not stolen.
    assert TestClient(app).get(
        f"/api/v1/workstations/{first['machine_id']}/assignments",
        headers={"Authorization": f"Bearer {first['api_key']}"}
    ).status_code == 200


def test_an_ambiguous_name_is_never_adopted(db):
    """Two records sharing a name means adopting either is a coin flip that
    could hand one person's printers to another. A new machine is recoverable;
    a wrong merge is not."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    a = _enroll(TestClient(app), key, "GUID-A", "DESK-01").json()
    b = _enroll(TestClient(app), key, "GUID-B", "DESK-01").json()
    for mid in (a["machine_id"], b["machine_id"]):
        _seen(db, db.get(m.Machine, mid), minutes_ago=999)

    third = _enroll(TestClient(app), key, "GUID-C", "DESK-01").json()
    assert third["adopted"] is False
    assert third["created"] is True
    assert third["machine_id"] not in (a["machine_id"], b["machine_id"])


def test_adoption_never_crosses_a_tenant(db):
    """The name is matched inside the enrollment key's client only. Two
    customers each having a DESK-01 is completely normal."""
    acme, acme_printer = _client_with_printer(db, "Acme", "10.0.0.1")
    globex, globex_printer = _client_with_printer(db, "Globex", "10.9.9.1")
    globex_machine = _enroll(
        TestClient(app), _enroll_key(db, globex), "GUID-GLOBEX", "DESK-01").json()
    db.add(m.PrinterAssignment(printer_id=globex_printer.id,
                               machine_id=globex_machine["machine_id"]))
    db.commit()
    _seen(db, db.get(m.Machine, globex_machine["machine_id"]), minutes_ago=999)

    # An Acme PC with the same computer name must NOT inherit Globex's record.
    acme_machine = _enroll(
        TestClient(app), _enroll_key(db, acme), "GUID-ACME", "DESK-01").json()
    assert acme_machine["adopted"] is False
    assert acme_machine["machine_id"] != globex_machine["machine_id"]
    assert acme_machine["client_id"] == acme.id


def test_a_blank_name_never_adopts(db):
    """Machine-reported and optional; every unnamed record would otherwise be
    one collision."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    first = _enroll(TestClient(app), key, "GUID-A", "").json()
    _seen(db, db.get(m.Machine, first["machine_id"]), minutes_ago=999)

    second = _enroll(TestClient(app), key, "GUID-B", "").json()
    assert second["adopted"] is False
    assert second["machine_id"] != first["machine_id"]


def test_name_matching_is_case_insensitive(db):
    """Windows computer names are, and a PC that comes back as DESK-01 having
    been desk-01 is the same desk."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    first = _enroll(TestClient(app), key, "GUID-A", "desk-01").json()
    _seen(db, db.get(m.Machine, first["machine_id"]), minutes_ago=999)

    second = _enroll(TestClient(app), key, "GUID-B", "DESK-01").json()
    assert second["adopted"] is True
    assert second["machine_id"] == first["machine_id"]


def test_adoption_can_be_turned_off(db):
    """It changes what holding an enrollment key gets you, so it is a setting."""
    from central.runtime import save_settings

    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    first = _enroll(TestClient(app), key, "GUID-A", "DESK-01").json()
    _seen(db, db.get(m.Machine, first["machine_id"]), minutes_ago=999)

    # An unchecked box posts nothing, so "in sections but absent from the form"
    # is how False is recorded -- passing the key with a False VALUE would set it
    # True, since save_settings reads presence, not value.
    save_settings(db, {"workstation.adopt_stale_after_min": 60},
                  sections={"Workstations"})
    db.commit()
    from central.runtime import load_settings
    assert load_settings(db)["workstation.adopt_by_name"] is False

    second = _enroll(TestClient(app), key, "GUID-B", "DESK-01").json()
    assert second["adopted"] is False
    assert second["created"] is True


def test_adoption_is_audited_as_its_own_action(db):
    """One machine taking over another record's printers by name is a different
    event from a key rotation, and must be findable."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    first = _enroll(TestClient(app), key, "GUID-A", "DESK-01").json()
    _seen(db, db.get(m.Machine, first["machine_id"]), minutes_ago=999)
    _enroll(TestClient(app), key, "GUID-B", "DESK-01")

    row = db.scalar(select(m.AuditLog).where(m.AuditLog.action == "machine.adopt"))
    assert row is not None
    assert "DESK-01" in (row.detail or "")
    assert "GUID-B" in (row.detail or ""), "the new identity is what took over"


def test_a_retired_machine_that_is_reimaged_comes_back_active(db):
    """An operator retired the old PC; the replacement is demonstrably running."""
    c, _ = _client_with_printer(db, "Acme", "10.0.0.1")
    key = _enroll_key(db, c)
    first = _enroll(TestClient(app), key, "GUID-A", "DESK-01").json()
    machine = db.get(m.Machine, first["machine_id"])
    machine.active = False
    db.commit()
    _seen(db, machine, minutes_ago=999)

    second = _enroll(TestClient(app), key, "GUID-B", "DESK-01").json()
    db.refresh(machine)
    assert second["adopted"] is True
    assert machine.active is True
