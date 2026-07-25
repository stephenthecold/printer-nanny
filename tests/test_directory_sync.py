"""Directory sync engine.

Tested with hand-built snapshots and no network at all -- which is the reason
providers return a plain DirectorySnapshot instead of writing to the database.
No CI runner has an Entra tenant, so if the rules lived in the providers they
would be untestable, and these are exactly the rules that destroy customer data
when they are wrong.
"""

from __future__ import annotations

import pytest

from central import models as m
from central import services
from central.directory.base import DirectoryGroup, DirectorySnapshot, DirectoryUser
from central.directory.sync import sync_snapshot

ENTRA = m.DirectorySource.entra
GOOGLE = m.DirectorySource.google


def _client(db, name="Acme"):
    c = m.Client(name=name)
    db.add(c)
    db.flush()
    s = m.Site(client_id=c.id, name=f"{name} HQ")
    db.add(s)
    db.flush()
    c._site = s
    return c


def _printer(db, client, ip="10.0.0.5"):
    p = m.Printer(client_id=client.id, site_id=client._site.id, ip=ip,
                  discovery_state=m.DiscoveryState.approved)
    db.add(p)
    db.flush()
    return p


def _people(db, client, source=ENTRA):
    return {
        u.directory_id: u
        for u in db.query(m.EndUser).filter_by(client_id=client.id,
                                               directory_source=source).all()
    }


def _snap(users=(), groups=(), complete=True):
    return DirectorySnapshot(users=list(users), groups=list(groups),
                             complete=complete)


# --------------------------- creation / update ------------------------------ #

def test_a_first_sync_creates_people(db):
    c = _client(db)
    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap([
        DirectoryUser("oid-1", email="a@acme.test", display_name="Ann"),
        DirectoryUser("oid-2", email="b@acme.test", display_name="Bob"),
    ]))
    assert counts["created"] == 2
    rows = _people(db, c)
    assert set(rows) == {"oid-1", "oid-2"}
    assert rows["oid-1"].display_name == "Ann"
    assert rows["oid-1"].directory_source is ENTRA


def test_syncing_twice_changes_nothing(db):
    c = _client(db)
    snap = _snap([DirectoryUser("oid-1", email="a@acme.test", display_name="Ann")])
    sync_snapshot(db, client=c, source=ENTRA, snapshot=snap)
    second = sync_snapshot(db, client=c, source=ENTRA, snapshot=snap)
    assert second["created"] == 0 and second["updated"] == 0
    assert second["deactivated"] == 0
    assert db.query(m.EndUser).count() == 1


def test_a_renamed_person_keeps_their_row_and_their_printers(db):
    """The whole reason identity is the immutable object id. Matching on email
    would deactivate this person as a leaver and create a stranger, stranding
    the assignment on the dead row."""
    c = _client(db)
    printer = _printer(db, c)
    sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap([
        DirectoryUser("oid-1", email="ann.smith@acme.test", display_name="Ann Smith"),
    ]))
    person = _people(db, c)["oid-1"]
    services.assign_printer(db, printer=printer, end_user=person, is_default=True)
    original_id = person.id

    # Married: new surname, new address, same objectId.
    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap([
        DirectoryUser("oid-1", email="ann.jones@acme.test", display_name="Ann Jones"),
    ]))
    assert counts["created"] == 0 and counts["deactivated"] == 0
    assert counts["updated"] == 1
    same = _people(db, c)["oid-1"]
    assert same.id == original_id
    assert same.email == "ann.jones@acme.test"
    assert len(services.effective_printers_for(db, same)) == 1


# --------------------------- deactivation ----------------------------------- #

def test_absence_deactivates_but_never_deletes(db):
    c = _client(db)
    printer = _printer(db, c)
    sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap([
        DirectoryUser("oid-1", email="a@acme.test"),
        DirectoryUser("oid-2", email="b@acme.test"),
    ]))
    services.assign_printer(db, printer=printer,
                            end_user=_people(db, c)["oid-2"])

    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap([
        DirectoryUser("oid-1", email="a@acme.test"),
    ]))
    assert counts["deactivated"] == 1
    rows = _people(db, c)
    assert rows["oid-2"].active is False
    # The row and its assignment survive for history; the access does not.
    assert db.query(m.PrinterAssignment).count() == 1
    assert services.effective_printers_for(db, rows["oid-2"]) == []


def test_a_disabled_account_is_deactivated_even_though_it_is_present(db):
    c = _client(db)
    sync_snapshot(db, client=c, source=ENTRA,
                  snapshot=_snap([DirectoryUser("oid-1", email="a@acme.test")]))
    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap([
        DirectoryUser("oid-1", email="a@acme.test", active=False),
    ]))
    assert counts["deactivated"] == 1
    assert _people(db, c)["oid-1"].active is False


def test_a_returning_employee_comes_back_to_the_same_row(db):
    c = _client(db)
    printer = _printer(db, c)
    sync_snapshot(db, client=c, source=ENTRA,
                  snapshot=_snap([DirectoryUser("oid-1", email="a@acme.test")]))
    person = _people(db, c)["oid-1"]
    services.assign_printer(db, printer=printer, end_user=person)
    original_id = person.id

    sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap([]))
    assert _people(db, c)["oid-1"].active is False

    counts = sync_snapshot(db, client=c, source=ENTRA,
                           snapshot=_snap([DirectoryUser("oid-1", email="a@acme.test")]))
    assert counts["reactivated"] == 1
    back = _people(db, c)["oid-1"]
    assert back.id == original_id and back.active is True
    # Their printer is theirs again, without an operator re-assigning it.
    assert len(services.effective_printers_for(db, back)) == 1


def test_an_incomplete_snapshot_deactivates_nobody(db):
    """A paging failure must not read as mass resignation."""
    c = _client(db)
    sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap([
        DirectoryUser("oid-1", email="a@acme.test"),
        DirectoryUser("oid-2", email="b@acme.test"),
        DirectoryUser("oid-3", email="c@acme.test"),
    ]))
    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap(
        [DirectoryUser("oid-1", email="a@acme.test")], complete=False,
    ))
    assert counts["deactivated"] == 0
    assert all(u.active for u in _people(db, c).values())


def test_an_incomplete_snapshot_still_creates_and_updates(db):
    c = _client(db)
    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap(
        [DirectoryUser("oid-9", email="new@acme.test")], complete=False,
    ))
    assert counts["created"] == 1


# --------------------------- manual rows are sacred ------------------------- #

def test_sync_never_touches_a_manual_person(db):
    """Operators hand-create contractors and shared accounts. A sync that
    deactivates them makes the manual path untrustworthy."""
    c = _client(db)
    manual = m.EndUser(client_id=c.id, display_name="Contractor",
                       email="temp@acme.test",
                       directory_source=m.DirectorySource.manual)
    db.add(manual)
    db.flush()

    sync_snapshot(db, client=c, source=ENTRA,
                  snapshot=_snap([DirectoryUser("oid-1", email="a@acme.test")]))
    db.refresh(manual)
    assert manual.active is True
    assert manual.directory_source is m.DirectorySource.manual


def test_another_providers_people_are_left_alone(db):
    """Two directories on one client must not deactivate each other's users."""
    c = _client(db)
    sync_snapshot(db, client=c, source=GOOGLE,
                  snapshot=_snap([DirectoryUser("g-1", email="g@acme.test")]))
    sync_snapshot(db, client=c, source=ENTRA,
                  snapshot=_snap([DirectoryUser("e-1", email="e@acme.test")]))
    assert _people(db, c, GOOGLE)["g-1"].active is True
    assert _people(db, c, ENTRA)["e-1"].active is True


def test_an_email_owned_by_a_manual_row_is_reported_not_hijacked(db):
    """Adopting would transfer operator-owned data to the directory, and on a
    bad match (shared mailbox, alias) merges two people irreversibly. Skipping
    is recoverable, so it is reported instead."""
    c = _client(db)
    manual = m.EndUser(client_id=c.id, display_name="Shared Reception",
                       email="reception@acme.test",
                       directory_source=m.DirectorySource.manual)
    db.add(manual)
    db.flush()
    manual_id = manual.id

    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap([
        DirectoryUser("oid-1", email="reception@acme.test", display_name="Reception"),
    ]))
    assert counts["conflicts"] == 1
    assert counts["created"] == 0
    assert "reception@acme.test" in counts["conflict_detail"][0]
    db.refresh(manual)
    assert manual.id == manual_id
    assert manual.directory_source is m.DirectorySource.manual
    assert manual.display_name == "Shared Reception"


def test_one_conflict_does_not_abandon_the_rest_of_the_batch(db):
    c = _client(db)
    db.add(m.EndUser(client_id=c.id, email="clash@acme.test",
                     directory_source=m.DirectorySource.manual))
    db.flush()
    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap([
        DirectoryUser("oid-1", email="clash@acme.test"),
        DirectoryUser("oid-2", email="fine@acme.test"),
        DirectoryUser("oid-3", email="also-fine@acme.test"),
    ]))
    assert counts["conflicts"] == 1
    assert counts["created"] == 2


# --------------------------- tenancy ---------------------------------------- #

def test_a_sync_only_ever_writes_into_its_own_client(db):
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    sync_snapshot(db, client=acme, source=ENTRA,
                  snapshot=_snap([DirectoryUser("oid-1", email="a@acme.test")]))
    assert db.query(m.EndUser).filter_by(client_id=globex.id).count() == 0
    assert db.query(m.EndUser).filter_by(client_id=acme.id).count() == 1


def test_the_same_directory_id_in_two_tenants_is_two_people(db):
    """Object ids are unique per directory, not globally. Two customers can
    legitimately present colliding ids and must not be merged."""
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    for c in (acme, globex):
        sync_snapshot(db, client=c, source=ENTRA,
                      snapshot=_snap([DirectoryUser("shared-oid", email=None)]))
    rows = db.query(m.EndUser).filter_by(directory_id="shared-oid").all()
    assert len(rows) == 2
    assert {r.client_id for r in rows} == {acme.id, globex.id}


def test_manual_is_refused_as_a_sync_source(db):
    c = _client(db)
    with pytest.raises(ValueError):
        sync_snapshot(db, client=c, source=m.DirectorySource.manual,
                      snapshot=_snap([]))


# --------------------------- groups ----------------------------------------- #

def test_groups_and_membership_sync(db):
    c = _client(db)
    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap(
        users=[DirectoryUser("oid-1", email="a@acme.test"),
               DirectoryUser("oid-2", email="b@acme.test")],
        groups=[DirectoryGroup("g-1", "Accounting", ["oid-1", "oid-2"])],
    ))
    assert counts["groups_created"] == 1
    assert counts["members_added"] == 2
    group = db.query(m.EndUserGroup).filter_by(directory_id="g-1").one()
    assert group.name == "Accounting"
    assert db.query(m.EndUserGroupMember).filter_by(group_id=group.id).count() == 2


def test_group_membership_grants_the_groups_printer(db):
    c = _client(db)
    printer = _printer(db, c)
    sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap(
        users=[DirectoryUser("oid-1", email="a@acme.test")],
        groups=[DirectoryGroup("g-1", "Accounting", ["oid-1"])],
    ))
    group = db.query(m.EndUserGroup).filter_by(directory_id="g-1").one()
    services.assign_printer(db, printer=printer, group=group)
    person = _people(db, c)["oid-1"]
    got = services.effective_printers_for(db, person)
    assert len(got) == 1 and got[0][2] == "Accounting"


def test_leaving_a_group_revokes_the_inherited_printer(db):
    c = _client(db)
    printer = _printer(db, c)
    sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap(
        users=[DirectoryUser("oid-1"), DirectoryUser("oid-2")],
        groups=[DirectoryGroup("g-1", "Accounting", ["oid-1", "oid-2"])],
    ))
    group = db.query(m.EndUserGroup).filter_by(directory_id="g-1").one()
    services.assign_printer(db, printer=printer, group=group)

    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap(
        users=[DirectoryUser("oid-1"), DirectoryUser("oid-2")],
        groups=[DirectoryGroup("g-1", "Accounting", ["oid-1"])],
    ))
    assert counts["members_removed"] == 1
    assert services.effective_printers_for(db, _people(db, c)["oid-2"]) == []
    assert len(services.effective_printers_for(db, _people(db, c)["oid-1"])) == 1


def test_a_renamed_group_keeps_its_assignments(db):
    c = _client(db)
    printer = _printer(db, c)
    sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap(
        users=[DirectoryUser("oid-1")],
        groups=[DirectoryGroup("g-1", "Accounting", ["oid-1"])],
    ))
    group = db.query(m.EndUserGroup).filter_by(directory_id="g-1").one()
    services.assign_printer(db, printer=printer, group=group)

    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap(
        users=[DirectoryUser("oid-1")],
        groups=[DirectoryGroup("g-1", "Finance", ["oid-1"])],
    ))
    assert counts["groups_updated"] == 1
    db.refresh(group)
    assert group.name == "Finance"
    assert len(services.effective_printers_for(db, _people(db, c)["oid-1"])) == 1


def test_a_deleted_group_is_emptied_not_deleted(db):
    """It may carry printer assignments an operator made; deleting it would
    discard them silently. An empty group grants nobody anything."""
    c = _client(db)
    printer = _printer(db, c)
    sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap(
        users=[DirectoryUser("oid-1")],
        groups=[DirectoryGroup("g-1", "Accounting", ["oid-1"])],
    ))
    group = db.query(m.EndUserGroup).filter_by(directory_id="g-1").one()
    services.assign_printer(db, printer=printer, group=group)

    sync_snapshot(db, client=c, source=ENTRA,
                  snapshot=_snap(users=[DirectoryUser("oid-1")], groups=[]))
    assert db.query(m.EndUserGroup).filter_by(directory_id="g-1").count() == 1
    assert db.query(m.EndUserGroupMember).count() == 0
    assert services.effective_printers_for(db, _people(db, c)["oid-1"]) == []


def test_an_unresolvable_group_member_is_skipped_not_fatal(db):
    """A filtered-out user or an unexpanded nested group must not drop everyone
    else in the group."""
    c = _client(db)
    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap(
        users=[DirectoryUser("oid-1")],
        groups=[DirectoryGroup("g-1", "Accounting", ["oid-1", "nested-group-id"])],
    ))
    assert counts["skipped_members"] == 1
    assert counts["members_added"] == 1


def test_a_group_name_owned_by_a_manual_group_is_not_hijacked(db):
    c = _client(db)
    db.add(m.EndUserGroup(client_id=c.id, name="Accounting",
                          directory_source=m.DirectorySource.manual))
    db.flush()
    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap(
        users=[], groups=[DirectoryGroup("g-1", "Accounting", [])],
    ))
    assert counts["conflicts"] == 1
    assert counts["groups_created"] == 0
    assert db.query(m.EndUserGroup).count() == 1


# --------------------------- malformed input -------------------------------- #

def test_a_user_with_no_directory_id_is_counted_not_crashed(db):
    c = _client(db)
    counts = sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap([
        DirectoryUser("", email="ghost@acme.test"),
        DirectoryUser("oid-2", email="real@acme.test"),
    ]))
    assert counts["conflicts"] == 1 and counts["created"] == 1


def test_whitespace_is_normalised(db):
    c = _client(db)
    sync_snapshot(db, client=c, source=ENTRA, snapshot=_snap([
        DirectoryUser("  oid-1  ", email="  a@acme.test  ", display_name="  Ann  "),
    ]))
    row = _people(db, c)["oid-1"]
    assert row.email == "a@acme.test" and row.display_name == "Ann"


def test_a_user_with_no_email_is_still_created(db):
    """On-prem AD accounts routinely have no mailbox."""
    c = _client(db)
    counts = sync_snapshot(db, client=c, source=m.DirectorySource.ad, snapshot=_snap([
        DirectoryUser("guid-1", upn="jsmith", display_name="J Smith"),
    ]))
    assert counts["created"] == 1
    row = db.query(m.EndUser).filter_by(directory_id="guid-1").one()
    assert row.email is None and row.upn == "jsmith"


def test_several_users_with_no_email_coexist(db):
    """UNIQUE(client_id, email) must not collapse them -- NULLs do not collide."""
    c = _client(db)
    counts = sync_snapshot(db, client=c, source=m.DirectorySource.ad, snapshot=_snap([
        DirectoryUser("guid-1", upn="a"),
        DirectoryUser("guid-2", upn="b"),
        DirectoryUser("guid-3", upn="c"),
    ]))
    assert counts["created"] == 3
