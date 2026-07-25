"""Dashboard: end users, groups, and printer assignment.

Lives at ``/manage/people`` rather than ``/manage/users`` because
``/manage/users`` already means something else here -- dashboard *operators*.
Keeping the two words distinct in the URL is the cheapest way to keep them
distinct in an operator's head: **Users** administer the system, **People**
work at the customer and print things.

Every route is manager-gated and every mutation is audited. Tenancy is never
enforced in this module: it is enforced once, in ``services.assign_printer`` /
``services.sync_group_members``, so that a route cannot forget. What this module
does own is refusing to *display* one client's data under another's selection,
which is why every query here filters on the resolved client id rather than
trusting an id from the form.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import services
from central.audit import record
from central.db import get_db
from central.dashboard.manage import (
    _flash,
    _manager,
    _pop_flash,
    _redirect,
    _tpl,
)

router = APIRouter(prefix="/manage", tags=["manage"])


def _resolve_client(db: Session, raw: Optional[str]) -> Optional[m.Client]:
    """Resolve the selected client, falling back to the first one.

    Returns None only when there are no clients at all. A bad or non-numeric id
    resolves to the fallback rather than 500ing -- this value arrives from a
    query string and a stale bookmark is not an error worth a stack trace.
    """
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    if not clients:
        return None
    if raw:
        try:
            wanted = int(raw)
        except (TypeError, ValueError):
            return clients[0]
        for c in clients:
            if c.id == wanted:
                return c
    return clients[0]


@router.get("/people", response_class=HTMLResponse)
def people_home(
    request: Request,
    client_id: str = "",
    db: Session = Depends(get_db),
):
    """End users and groups for one client, with their effective printers.

    Scoped to a single client on purpose. An MSP's combined staff list across
    every customer is not a thing anyone needs to look at, and rendering it
    would make a cross-tenant mistake invisible in a wall of names.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    client = _resolve_client(db, client_id)
    if client is None:
        return _tpl(request, "people.html", db, clients=[], client=None,
                    people=[], groups=[], printers=[], resolved={},
                    memberships={}, flash=_pop_flash(request))

    people = list(db.scalars(
        select(m.EndUser)
        .where(m.EndUser.client_id == client.id)
        .order_by(m.EndUser.active.desc(), m.EndUser.display_name, m.EndUser.email)
    ))
    groups = list(db.scalars(
        select(m.EndUserGroup)
        .where(m.EndUserGroup.client_id == client.id)
        .order_by(m.EndUserGroup.name)
    ))
    # Only approved printers can be handed to staff. A pending-discovery device
    # has not been accepted into the fleet yet; assigning one would deploy a
    # queue for hardware an operator has not confirmed is theirs.
    printers = list(db.scalars(
        select(m.Printer)
        .where(
            m.Printer.client_id == client.id,
            m.Printer.discovery_state == m.DiscoveryState.approved,
        )
        .order_by(m.Printer.display_name, m.Printer.ip)
    ))

    resolved = {p.id: services.effective_printers_for(db, p) for p in people}
    memberships = {
        g.id: set(db.scalars(
            select(m.EndUserGroupMember.end_user_id).where(
                m.EndUserGroupMember.group_id == g.id
            )
        ).all())
        for g in groups
    }
    group_assignments = {
        g.id: list(db.scalars(
            select(m.PrinterAssignment).where(m.PrinterAssignment.group_id == g.id)
        ))
        for g in groups
    }

    return _tpl(
        request, "people.html", db,
        clients=clients, client=client, people=people, groups=groups,
        printers=printers, resolved=resolved, memberships=memberships,
        group_assignments=group_assignments, flash=_pop_flash(request),
    )


@router.post("/people/create")
def people_create(
    request: Request,
    client_id: int = Form(...),
    email: str = Form(""),
    display_name: str = Form(""),
    upn: str = Form(""),
    db: Session = Depends(get_db),
):
    """Add one person by hand.

    Manual entry is a first-class path, not a stopgap for the directory sync
    that lands later: contractors, shared-workstation logins and the customer
    who will never connect an IdP all live here.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client is None:
        _flash(request, "Client not found.")
        return _redirect("/manage/people")

    email = email.strip() or None
    display_name = display_name.strip() or None
    upn = upn.strip() or None
    if not (email or upn or display_name):
        _flash(request, "Give the person at least a name, email, or username.")
        return _redirect(f"/manage/people?client_id={client.id}")

    if email and db.scalar(
        select(m.EndUser).where(
            m.EndUser.client_id == client.id, m.EndUser.email == email
        )
    ):
        _flash(request, f"{email} is already on this client's staff list.")
        return _redirect(f"/manage/people?client_id={client.id}")

    person = m.EndUser(
        client_id=client.id, email=email, display_name=display_name, upn=upn,
        directory_source=m.DirectorySource.manual,
    )
    db.add(person)
    db.flush()
    record(db, request, user, "end_user.create",
           target=f"end_user:{person.id} (client:{client.id})",
           detail=f"manual: {display_name or email or upn}")
    db.commit()
    _flash(request, f"Added {display_name or email or upn}.")
    return _redirect(f"/manage/people?client_id={client.id}")


@router.post("/people/{person_id}/active")
def people_set_active(
    person_id: int,
    request: Request,
    active: str = Form(""),
    db: Session = Depends(get_db),
):
    """Activate or deactivate. Never delete.

    Deactivation is the deprovisioning gesture: the person's assignments stay on
    file (so "who had that printer?" survives them leaving) while
    ``effective_printers_for`` immediately resolves them to nothing.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    person = db.get(m.EndUser, person_id)
    if person is None:
        _flash(request, "Person not found.")
        return _redirect("/manage/people")

    person.active = bool(active.strip())
    record(db, request, user,
           "end_user.activate" if person.active else "end_user.deactivate",
           target=f"end_user:{person.id} (client:{person.client_id})",
           detail=person.display_name or person.email or person.upn or "")
    db.commit()
    return _redirect(f"/manage/people?client_id={person.client_id}")


@router.post("/people/assign")
def people_assign(
    request: Request,
    printer_id: int = Form(...),
    end_user_id: str = Form(""),
    group_id: str = Form(""),
    is_default: str = Form(""),
    db: Session = Depends(get_db),
):
    """Assign a printer to one person or one group.

    The tenancy check lives in ``services.assign_printer``; a ``TenancyError``
    here means somebody submitted a printer id belonging to another customer,
    which is a security event rather than a typo -- so it is audited as a
    refusal and shown as a flat refusal, not as a form hint that would tell the
    submitter which ids do and do not exist.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")

    printer = db.get(m.Printer, printer_id)
    if printer is None:
        _flash(request, "Printer not found.")
        return _redirect("/manage/people")

    person = db.get(m.EndUser, int(end_user_id)) if end_user_id.strip() else None
    group = db.get(m.EndUserGroup, int(group_id)) if group_id.strip() else None
    back = f"/manage/people?client_id={printer.client_id}"

    if (person is None) == (group is None):
        _flash(request, "Pick exactly one person or one group.")
        return _redirect(back)

    try:
        services.assign_printer(
            db, printer=printer, end_user=person, group=group,
            is_default=bool(is_default.strip()), operator_id=user.id,
        )
    except services.TenancyError as exc:
        record(db, request, user, "printer_assignment.refused",
               target=f"printer:{printer.id}", detail=f"cross-tenant: {exc}")
        db.commit()
        _flash(request, "That printer and that person belong to different clients.")
        return _redirect(back)

    target = (f"end_user:{person.id}" if person else f"group:{group.id}")
    record(db, request, user, "printer_assignment.create",
           target=f"printer:{printer.id} -> {target}",
           detail=("default" if is_default.strip() else "assigned"))
    db.commit()
    _flash(request, "Assigned.")
    return _redirect(back)


@router.post("/people/unassign")
def people_unassign(
    request: Request,
    assignment_id: int = Form(...),
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    row = db.get(m.PrinterAssignment, assignment_id)
    if row is None:
        _flash(request, "Assignment not found.")
        return _redirect("/manage/people")

    printer = db.get(m.Printer, row.printer_id)
    client_id = printer.client_id if printer else ""
    target = (f"end_user:{row.end_user_id}" if row.end_user_id
              else f"group:{row.group_id}")
    record(db, request, user, "printer_assignment.delete",
           target=f"printer:{row.printer_id} -> {target}", detail="")
    db.delete(row)
    db.commit()
    _flash(request, "Removed.")
    return _redirect(f"/manage/people?client_id={client_id}")


@router.post("/people/groups/create")
def group_create(
    request: Request,
    client_id: int = Form(...),
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client is None:
        _flash(request, "Client not found.")
        return _redirect("/manage/people")

    name = name.strip()
    if not name:
        _flash(request, "A group needs a name.")
        return _redirect(f"/manage/people?client_id={client.id}")
    if db.scalar(select(m.EndUserGroup).where(
        m.EndUserGroup.client_id == client.id, m.EndUserGroup.name == name
    )):
        _flash(request, f"'{name}' already exists for this client.")
        return _redirect(f"/manage/people?client_id={client.id}")

    group = m.EndUserGroup(client_id=client.id, name=name,
                           directory_source=m.DirectorySource.manual)
    db.add(group)
    db.flush()
    record(db, request, user, "end_user_group.create",
           target=f"group:{group.id} (client:{client.id})", detail=name)
    db.commit()
    _flash(request, f"Created group '{name}'.")
    return _redirect(f"/manage/people?client_id={client.id}")


@router.post("/people/groups/{group_id}/members")
def group_members(
    group_id: int,
    request: Request,
    # typing.List to match suppression_create's `weekdays`, the dashboard's
    # other repeated form field. The PEP 585 builtin binds identically here --
    # this is consistency with the existing handler, not a workaround.
    member_ids: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Replace a group's membership with exactly the ticked people.

    A checkbox list, so an empty submission legitimately means "nobody" -- there
    is no presence-marker problem here because the form always posts the group
    id via the URL, and this handler's whole contract is "set membership to the
    submitted set". Unticking everyone must therefore empty the group, not be
    mistaken for a field that wasn't rendered.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    group = db.get(m.EndUserGroup, group_id)
    if group is None:
        _flash(request, "Group not found.")
        return _redirect("/manage/people")

    wanted = []
    for raw in member_ids:
        raw = (raw or "").strip()
        if not raw:
            continue
        person = db.get(m.EndUser, int(raw)) if raw.isdigit() else None
        if person is not None:
            wanted.append(person)

    try:
        added, removed = services.sync_group_members(
            db, group=group, end_users=wanted
        )
    except services.TenancyError as exc:
        record(db, request, user, "end_user_group.refused",
               target=f"group:{group.id}", detail=f"cross-tenant: {exc}")
        db.commit()
        _flash(request, "Those people belong to a different client.")
        return _redirect(f"/manage/people?client_id={group.client_id}")

    record(db, request, user, "end_user_group.members",
           target=f"group:{group.id} (client:{group.client_id})",
           detail=f"+{added} -{removed}")
    db.commit()
    _flash(request, f"{group.name}: {added} added, {removed} removed.")
    return _redirect(f"/manage/people?client_id={group.client_id}")
