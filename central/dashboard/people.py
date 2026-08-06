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
from central.directory import registry
from central.directory.sync import run_connection
from central.secrets import encrypt_value
from central.db import get_db
from central.dashboard.manage import (
    _deny,
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
        return _deny(request, db)

    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    client = _resolve_client(db, client_id)
    if client is None:
        # ``user=`` is not optional. ``_tpl`` reads ctx.get("user") but never
        # injects it, and base.html wraps the whole nav -- plus the skip link,
        # the account chip and Logout -- in ``{% if user %}``. Omitting it here
        # rendered the flagship print-management page with ZERO navigation and
        # no way to sign out.
        return _tpl(request, "people.html", db, user=user, clients=[], client=None,
                    people=[], groups=[], printers=[], resolved={},
                    memberships={}, providers=[], flash=_pop_flash(request))

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

    connections = {
        c.provider: c
        for c in db.scalars(
            select(m.DirectoryConnection).where(
                m.DirectoryConnection.client_id == client.id
            )
        )
    }
    # Provider metadata drives the form, so adding a provider to the registry
    # renders its fields without touching this template.
    providers = [
        {
            "key": src.value,
            "label": {"entra": "Microsoft Entra ID",
                      "google": "Google Workspace",
                      "ad": "On-prem Active Directory"}[src.value],
            "fields": registry.CONFIG_FIELDS[src],
            "secret_label": registry.SECRET_LABEL[src],
            "conn": connections.get(src),
        }
        for src in (m.DirectorySource.entra, m.DirectorySource.google,
                    m.DirectorySource.ad)
    ]

    return _tpl(
        request, "people.html", db, user=user,
        clients=clients, client=client, people=people, groups=groups,
        printers=printers, resolved=resolved, memberships=memberships,
        group_assignments=group_assignments, providers=providers,
        flash=_pop_flash(request),
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
        return _deny(request, db)
    client = db.get(m.Client, client_id)
    if client is None:
        _flash(request, "Client not found.", level="error")
        return _redirect("/manage/people")

    email = email.strip() or None
    display_name = display_name.strip() or None
    upn = upn.strip() or None
    if not (email or upn or display_name):
        _flash(request, "Give the person at least a name, email, or username.", level="error")
        return _redirect(f"/manage/people?client_id={client.id}")

    if email and db.scalar(
        select(m.EndUser).where(
            m.EndUser.client_id == client.id, m.EndUser.email == email
        )
    ):
        _flash(request, f"{email} is already on this client's staff list.", level="error")
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
        return _deny(request, db)
    person = db.get(m.EndUser, person_id)
    if person is None:
        _flash(request, "Person not found.", level="error")
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
        return _deny(request, db)

    printer = db.get(m.Printer, printer_id)
    if printer is None:
        _flash(request, "Printer not found.", level="error")
        return _redirect("/manage/people")

    person = db.get(m.EndUser, int(end_user_id)) if end_user_id.strip() else None
    group = db.get(m.EndUserGroup, int(group_id)) if group_id.strip() else None
    back = f"/manage/people?client_id={printer.client_id}"

    if (person is None) == (group is None):
        _flash(request, "Pick exactly one person or one group.", level="error")
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
        _flash(request, "That printer and that person belong to different clients.",
               level="error")
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
        return _deny(request, db)
    row = db.get(m.PrinterAssignment, assignment_id)
    if row is None:
        _flash(request, "Assignment not found.", level="error")
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
        return _deny(request, db)
    client = db.get(m.Client, client_id)
    if client is None:
        _flash(request, "Client not found.", level="error")
        return _redirect("/manage/people")

    name = name.strip()
    if not name:
        _flash(request, "A group needs a name.", level="error")
        return _redirect(f"/manage/people?client_id={client.id}")
    if db.scalar(select(m.EndUserGroup).where(
        m.EndUserGroup.client_id == client.id, m.EndUserGroup.name == name
    )):
        _flash(request, f"'{name}' already exists for this client.", level="error")
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
        return _deny(request, db)
    group = db.get(m.EndUserGroup, group_id)
    if group is None:
        _flash(request, "Group not found.", level="error")
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
        _flash(request, "Those people belong to a different client.", level="error")
        return _redirect(f"/manage/people?client_id={group.client_id}")

    record(db, request, user, "end_user_group.members",
           target=f"group:{group.id} (client:{group.client_id})",
           detail=f"+{added} -{removed}")
    db.commit()
    _flash(request, f"{group.name}: {added} added, {removed} removed.")
    return _redirect(f"/manage/people?client_id={group.client_id}")


# --------------------------------------------------------------------------- #
# Directory connections
# --------------------------------------------------------------------------- #
@router.post("/people/directory/save")
def directory_save(
    request: Request,
    client_id: int = Form(...),
    provider: str = Form(...),
    secret: str = Form(""),
    enabled: str = Form(""),
    # Provider settings are declared explicitly and prefixed `cfg_`. The prefix
    # is not cosmetic: Entra's own config field is called `client_id`, which
    # would otherwise collide with this form's `client_id` (the Printer Nanny
    # client) and silently write a tenant's app id into the wrong parameter.
    cfg_tenant_id: str = Form(""),
    cfg_client_id: str = Form(""),
    cfg_admin_email: str = Form(""),
    cfg_customer_domain: str = Form(""),
    cfg_server: str = Form(""),
    cfg_base_dn: str = Form(""),
    cfg_bind_dn: str = Form(""),
    cfg_user_filter: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create or update one client's connection to one directory.

    The secret is **write-only**: it is encrypted on the way in and never read
    back into the form. An empty secret field therefore means "leave the stored
    one alone", not "clear it" -- otherwise every edit of an unrelated field
    (a corrected base DN, a filter tweak) would silently wipe the credential
    and the next sync would fail for a reason nobody would connect to the edit.
    """
    user = _manager(request, db)
    if user is None:
        return _deny(request, db)
    client = db.get(m.Client, client_id)
    if client is None:
        _flash(request, "Client not found.", level="error")
        return _redirect("/manage/people")
    back = f"/manage/people?client_id={client.id}"

    try:
        source = m.DirectorySource(provider)
    except ValueError:
        _flash(request, "Unknown directory provider.", level="error")
        return _redirect(back)
    if source is m.DirectorySource.manual:
        _flash(request, "'manual' is a provenance, not a directory you connect to.",
               level="error")
        return _redirect(back)

    submitted = {
        "tenant_id": cfg_tenant_id, "client_id": cfg_client_id,
        "admin_email": cfg_admin_email, "customer_domain": cfg_customer_domain,
        "server": cfg_server, "base_dn": cfg_base_dn,
        "bind_dn": cfg_bind_dn, "user_filter": cfg_user_filter,
    }
    # Only the selected provider's own fields are stored, so switching provider
    # cannot leave another one's settings loitering in the config blob.
    config = {
        field: (submitted.get(field) or "").strip()
        for field, _label, _help in registry.CONFIG_FIELDS.get(source, [])
    }

    conn = db.scalar(
        select(m.DirectoryConnection).where(
            m.DirectoryConnection.client_id == client.id,
            m.DirectoryConnection.provider == source,
        )
    )
    created = conn is None
    if conn is None:
        conn = m.DirectoryConnection(client_id=client.id, provider=source)
        db.add(conn)

    conn.config = config
    conn.enabled = bool(enabled.strip())
    secret = secret.strip()
    if secret:
        conn.secret = encrypt_value(secret)
    db.flush()

    # Key names only, never values -- the audit log is read by more people than
    # the settings page is.
    record(db, request, user,
           "directory.create" if created else "directory.update",
           target=f"directory:{conn.id} (client:{client.id}, {source.value})",
           detail=f"config keys: {','.join(sorted(k for k in config if config[k]))}"
                  f"{'; secret set' if secret else ''}")
    db.commit()
    _flash(request, f"Saved {source.value} connection.")
    return _redirect(back)


@router.post("/people/directory/{conn_id}/sync")
def directory_sync_now(
    conn_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Run one connection immediately.

    Synchronous on purpose: an operator who just pasted a client secret wants to
    know now whether it works, and a background job that reports "queued" is a
    worse answer than a slow one.
    """
    user = _manager(request, db)
    if user is None:
        return _deny(request, db)
    conn = db.get(m.DirectoryConnection, conn_id)
    if conn is None:
        _flash(request, "Connection not found.", level="error")
        return _redirect("/manage/people")

    result = run_connection(db, conn)
    record(db, request, user, "directory.sync",
           target=f"directory:{conn.id} (client:{conn.client_id}, "
                  f"{conn.provider.value})",
           detail=str(result)[:400])
    db.commit()

    if result.get("error"):
        _flash(request, f"Sync failed: {result['error']}", level="error")
    else:
        # Computed, not static: the caveats this message appends are conditional,
        # so a fixed level would either paint every clean sync amber or announce a
        # partial application in the colour that means "that worked". A partial
        # fetch applied NO deactivations, so a leaver is still active and still
        # holds their printers; conflicts are rows deliberately not adopted. The
        # connection row right above this box already renders both in amber
        # (people.html), and the flash must not contradict it. Same shape as the
        # maintenance-log flash in manage.py.
        _flash(request, (
            f"Synced: {result.get('created', 0)} added, "
            f"{result.get('updated', 0)} updated, "
            f"{result.get('deactivated', 0)} deactivated"
            + (f", {result['conflicts']} conflict(s)" if result.get("conflicts") else "")
            + ("" if result.get("complete", True)
               else " — partial fetch, no deactivations applied")
        ), level=("warning" if result.get("conflicts")
                  or not result.get("complete", True) else "success"))
    return _redirect(f"/manage/people?client_id={conn.client_id}")


@router.post("/people/directory/{conn_id}/delete")
def directory_delete(
    conn_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Remove a connection. The people it synced are left exactly as they are.

    Deleting the connection is an administrative act about credentials; it is
    not a statement that the customer's staff have left. Cascading to the
    end_users would deprovision a whole company because somebody rotated a
    secret badly and removed the connection to start over.
    """
    user = _manager(request, db)
    if user is None:
        return _deny(request, db)
    conn = db.get(m.DirectoryConnection, conn_id)
    if conn is None:
        _flash(request, "Connection not found.", level="error")
        return _redirect("/manage/people")

    client_id, provider = conn.client_id, conn.provider.value
    record(db, request, user, "directory.delete",
           target=f"directory:{conn.id} (client:{client_id}, {provider})",
           detail="synced people left in place")
    db.delete(conn)
    db.commit()
    _flash(request, f"Removed the {provider} connection. Synced people kept.")
    return _redirect(f"/manage/people?client_id={client_id}")
