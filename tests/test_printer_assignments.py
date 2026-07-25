"""End users, groups, and printer assignment.

The invariant under test throughout is tenancy: this is a multi-tenant product,
and an operator who can attach customer A's printer to customer B's staff does
not merely create a wrong row -- they leak the existence, name and location of
one customer's hardware into another customer's UI. No CHECK constraint can
state that rule (it spans three tables), so it lives in the service layer and is
attacked directly here.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from central import models as m
from central import services


def _client(db, name: str) -> m.Client:
    c = m.Client(name=name)
    db.add(c)
    db.flush()
    site = m.Site(client_id=c.id, name=f"{name} HQ")
    db.add(site)
    db.flush()
    c._site = site  # test convenience only
    return c


def _printer(db, client: m.Client, ip: str, name: str = "P") -> m.Printer:
    p = m.Printer(client_id=client.id, site_id=client._site.id, ip=ip, display_name=name)
    db.add(p)
    db.flush()
    return p


def _person(db, client: m.Client, email: str) -> m.EndUser:
    u = m.EndUser(client_id=client.id, email=email, display_name=email.split("@")[0])
    db.add(u)
    db.flush()
    return u


def _group(db, client: m.Client, name: str) -> m.EndUserGroup:
    g = m.EndUserGroup(client_id=client.id, name=name)
    db.add(g)
    db.flush()
    return g


# --------------------------- tenancy --------------------------------------- #

def test_cannot_assign_another_tenants_printer_to_a_user(db):
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    printer = _printer(db, globex, "10.0.0.5")
    person = _person(db, acme, "a@acme.test")
    with pytest.raises(services.TenancyError):
        services.assign_printer(db, printer=printer, end_user=person)


def test_cannot_assign_another_tenants_printer_to_a_group(db):
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    printer = _printer(db, globex, "10.0.0.5")
    group = _group(db, acme, "Accounting")
    with pytest.raises(services.TenancyError):
        services.assign_printer(db, printer=printer, group=group)


def test_tenancy_error_is_its_own_type(db):
    """Callers must be able to tell a form mistake from a cross-tenant reach.
    The first is a validation message; the second is a security event."""
    assert issubclass(services.TenancyError, ValueError)
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    with pytest.raises(services.TenancyError):
        services.assign_printer(
            db, printer=_printer(db, globex, "10.0.0.9"),
            end_user=_person(db, acme, "x@acme.test"),
        )


def test_group_membership_is_tenant_scoped(db):
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    group = _group(db, acme, "Accounting")
    outsider = _person(db, globex, "b@globex.test")
    with pytest.raises(services.TenancyError):
        services.sync_group_members(db, group=group, end_users=[outsider])


def test_a_rejected_membership_sync_adds_nobody(db):
    """Tenancy is checked for the whole batch before anything is written --
    a partial application would leave a cross-tenant group half-built."""
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    group = _group(db, acme, "Accounting")
    insider = _person(db, acme, "ok@acme.test")
    outsider = _person(db, globex, "bad@globex.test")
    with pytest.raises(services.TenancyError):
        services.sync_group_members(db, group=group, end_users=[insider, outsider])
    db.rollback()
    assert db.query(m.EndUserGroupMember).count() == 0


# --------------------------- shape constraints ------------------------------ #

def test_exactly_one_target_is_required(db):
    acme = _client(db, "Acme")
    printer = _printer(db, acme, "10.0.0.5")
    person = _person(db, acme, "a@acme.test")
    group = _group(db, acme, "Accounting")
    with pytest.raises(ValueError):
        services.assign_printer(db, printer=printer)
    with pytest.raises(ValueError):
        services.assign_printer(db, printer=printer, end_user=person, group=group)


def test_the_database_also_refuses_a_two_target_row(db):
    """The service guard is not the only line of defence -- a direct insert,
    a future route, or a bulk import must hit the CHECK too."""
    acme = _client(db, "Acme")
    printer = _printer(db, acme, "10.0.0.5")
    person = _person(db, acme, "a@acme.test")
    group = _group(db, acme, "Accounting")
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(text(
            "INSERT INTO printer_assignments "
            "(printer_id, end_user_id, group_id, is_default, created_at) "
            "VALUES (:p, :u, :g, 0, CURRENT_TIMESTAMP)"
        ), {"p": printer.id, "u": person.id, "g": group.id})
        db.flush()
    db.rollback()


def test_the_database_also_refuses_a_targetless_row(db):
    acme = _client(db, "Acme")
    printer = _printer(db, acme, "10.0.0.5")
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(text(
            "INSERT INTO printer_assignments "
            "(printer_id, end_user_id, group_id, is_default, created_at) "
            "VALUES (:p, NULL, NULL, 0, CURRENT_TIMESTAMP)"
        ), {"p": printer.id})
        db.flush()
    db.rollback()


def test_reassigning_the_same_pair_is_idempotent(db):
    """Ticking the box twice is a normal operator gesture, not a 500."""
    acme = _client(db, "Acme")
    printer = _printer(db, acme, "10.0.0.5")
    person = _person(db, acme, "a@acme.test")
    first = services.assign_printer(db, printer=printer, end_user=person)
    second = services.assign_printer(db, printer=printer, end_user=person, is_default=True)
    assert first.id == second.id
    assert second.is_default is True
    assert db.query(m.PrinterAssignment).count() == 1


def test_two_customers_can_both_have_a_jsmith(db):
    """The reason end users are not rows in `users`: that table's username is
    globally unique, and this is the first thing that would break."""
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    _person(db, acme, "jsmith@acme.test")
    _person(db, globex, "jsmith@globex.test")
    # Same local identity, same address even, different tenant.
    a = m.EndUser(client_id=acme.id, email="shared@example.test")
    b = m.EndUser(client_id=globex.id, email="shared@example.test")
    db.add_all([a, b])
    db.flush()
    assert a.id != b.id


def test_one_email_cannot_repeat_within_a_tenant(db):
    acme = _client(db, "Acme")
    _person(db, acme, "dupe@acme.test")
    db.add(m.EndUser(client_id=acme.id, email="dupe@acme.test"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


# --------------------------- resolution ------------------------------------- #

def test_direct_and_group_assignments_merge(db):
    acme = _client(db, "Acme")
    direct_p = _printer(db, acme, "10.0.0.5", "Direct")
    group_p = _printer(db, acme, "10.0.0.6", "Group")
    person = _person(db, acme, "a@acme.test")
    group = _group(db, acme, "Accounting")
    services.sync_group_members(db, group=group, end_users=[person])
    services.assign_printer(db, printer=direct_p, end_user=person)
    services.assign_printer(db, printer=group_p, group=group)

    got = services.effective_printers_for(db, person)
    assert [p.display_name for p, _, _ in got] == ["Direct", "Group"]
    assert [via for _, _, via in got] == [None, "Accounting"]


def test_a_printer_assigned_both_ways_appears_once_and_reads_as_direct(db):
    acme = _client(db, "Acme")
    printer = _printer(db, acme, "10.0.0.5", "Shared")
    person = _person(db, acme, "a@acme.test")
    group = _group(db, acme, "Accounting")
    services.sync_group_members(db, group=group, end_users=[person])
    services.assign_printer(db, printer=printer, group=group)
    services.assign_printer(db, printer=printer, end_user=person)

    got = services.effective_printers_for(db, person)
    assert len(got) == 1
    # Direct wins: somebody singled this person out, which is more specific
    # information than "they are in Accounting".
    assert got[0][2] is None


def test_a_direct_default_beats_a_group_default(db):
    acme = _client(db, "Acme")
    group_p = _printer(db, acme, "10.0.0.5", "GroupDefault")
    direct_p = _printer(db, acme, "10.0.0.6", "DirectDefault")
    person = _person(db, acme, "a@acme.test")
    group = _group(db, acme, "Accounting")
    services.sync_group_members(db, group=group, end_users=[person])
    services.assign_printer(db, printer=group_p, group=group, is_default=True)
    services.assign_printer(db, printer=direct_p, end_user=person, is_default=True)

    defaults = [p.display_name for p, is_def, _ in services.effective_printers_for(db, person)
                if is_def]
    assert defaults == ["DirectDefault"]


def test_competing_group_defaults_resolve_deterministically(db):
    """Three groups can each claim the default. Last-writer-wins would make the
    workstation's default depend on dict ordering; lowest printer id does not."""
    acme = _client(db, "Acme")
    p1 = _printer(db, acme, "10.0.0.5", "First")
    p2 = _printer(db, acme, "10.0.0.6", "Second")
    person = _person(db, acme, "a@acme.test")
    g1, g2 = _group(db, acme, "A"), _group(db, acme, "B")
    services.sync_group_members(db, group=g1, end_users=[person])
    services.sync_group_members(db, group=g2, end_users=[person])
    services.assign_printer(db, printer=p2, group=g2, is_default=True)
    services.assign_printer(db, printer=p1, group=g1, is_default=True)

    for _ in range(3):
        db.expire_all()
        defaults = [p.display_name for p, is_def, _ in
                    services.effective_printers_for(db, person) if is_def]
        assert defaults == ["First"]


def test_exactly_one_default_ever_comes_back(db):
    acme = _client(db, "Acme")
    person = _person(db, acme, "a@acme.test")
    group = _group(db, acme, "All")
    services.sync_group_members(db, group=group, end_users=[person])
    for i in range(4):
        services.assign_printer(
            db, printer=_printer(db, acme, f"10.0.0.{i}", f"P{i}"),
            group=group, is_default=True,
        )
    got = services.effective_printers_for(db, person)
    assert len(got) == 4
    assert sum(1 for _, is_def, _ in got if is_def) == 1


def test_a_deactivated_person_keeps_their_rows_but_loses_their_printers(db):
    """Deprovisioning must take effect immediately at the resolution layer --
    the rows survive for audit, the access does not."""
    acme = _client(db, "Acme")
    printer = _printer(db, acme, "10.0.0.5")
    person = _person(db, acme, "a@acme.test")
    services.assign_printer(db, printer=printer, end_user=person)
    assert len(services.effective_printers_for(db, person)) == 1

    person.active = False
    db.flush()
    assert services.effective_printers_for(db, person) == []
    assert db.query(m.PrinterAssignment).count() == 1


def test_someone_with_nothing_assigned_resolves_empty(db):
    acme = _client(db, "Acme")
    assert services.effective_printers_for(db, _person(db, acme, "a@acme.test")) == []


# --------------------------- membership sync -------------------------------- #

def test_membership_sync_is_a_set_difference(db):
    """Not delete-all-then-insert: with a sync on a schedule, that would be a
    real, recurring window in which everybody briefly has no printers."""
    acme = _client(db, "Acme")
    group = _group(db, acme, "Accounting")
    a = _person(db, acme, "a@acme.test")
    b = _person(db, acme, "b@acme.test")
    c = _person(db, acme, "c@acme.test")

    added, removed = services.sync_group_members(db, group=group, end_users=[a, b])
    assert (added, removed) == (2, 0)

    # b stays -- its row must not be churned just because the batch was resent.
    b_row = db.query(m.EndUserGroupMember).filter_by(group_id=group.id,
                                                     end_user_id=b.id).one()
    added, removed = services.sync_group_members(db, group=group, end_users=[b, c])
    assert (added, removed) == (1, 1)
    assert db.query(m.EndUserGroupMember).filter_by(group_id=group.id,
                                                    end_user_id=b.id).one() is b_row
    members = {r.end_user_id for r in db.query(m.EndUserGroupMember)
               .filter_by(group_id=group.id).all()}
    assert members == {b.id, c.id}


def test_membership_sync_to_empty_removes_everyone(db):
    acme = _client(db, "Acme")
    group = _group(db, acme, "Accounting")
    services.sync_group_members(db, group=group,
                                end_users=[_person(db, acme, "a@acme.test")])
    added, removed = services.sync_group_members(db, group=group, end_users=[])
    assert (added, removed) == (0, 1)
    assert db.query(m.EndUserGroupMember).count() == 0


def test_removing_someone_from_a_group_revokes_the_inherited_printer(db):
    acme = _client(db, "Acme")
    printer = _printer(db, acme, "10.0.0.5")
    person = _person(db, acme, "a@acme.test")
    group = _group(db, acme, "Accounting")
    services.sync_group_members(db, group=group, end_users=[person])
    services.assign_printer(db, printer=printer, group=group)
    assert len(services.effective_printers_for(db, person)) == 1

    services.sync_group_members(db, group=group, end_users=[])
    assert services.effective_printers_for(db, person) == []
