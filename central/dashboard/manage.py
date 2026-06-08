"""Management UI: create/edit/delete clients, sites, printers, and enroll agents.

Plain server-rendered forms (POST + redirect) -- robust and JS-free. Viewing and
creating require any logged-in user; deletes require admin.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central.dashboard import _keystore
from central.db import get_db
from central.runtime import app_branding
from central.security import generate_api_key, hash_api_key, hash_password


def _split_tags(raw: str) -> Optional[list[str]]:
    """Parse a comma-separated tag input into a clean list (None if empty)."""
    tags = [t.strip() for t in (raw or "").split(",") if t.strip()]
    return tags or None

router = APIRouter(prefix="/manage", tags=["manage"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_MANAGER_ROLES = {m.UserRole.admin, m.UserRole.tech}


def _user(request: Request, db: Session) -> Optional[m.User]:
    uid = request.session.get("user_id")
    return db.get(m.User, uid) if uid else None


def _manager(request: Request, db: Session) -> Optional[m.User]:
    """Management is for admin/tech only -- client_readonly users get nothing here."""
    user = _user(request, db)
    return user if (user is not None and user.role in _MANAGER_ROLES) else None


def _admin(request: Request, db: Session) -> Optional[m.User]:
    """Admin-only routes (user management, white-label settings) use this."""
    user = _user(request, db)
    return user if (user is not None and user.role == m.UserRole.admin) else None


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _pop_flash(request: Request) -> Optional[str]:
    return request.session.pop("flash", None)


def _tpl(request: Request, template: str, db: Session, **ctx) -> HTMLResponse:
    """Local render helper that always injects ``app.*`` branding into context.

    Keeps every manage template (nav, login, footer) in sync with the operator's
    Settings -> Branding values without each callsite having to remember.
    """
    ctx.setdefault("app", app_branding(db))
    return _templates.TemplateResponse(request, template, ctx)


# --------------------------------------------------------------------------- #
# Clients & sites
# --------------------------------------------------------------------------- #
@router.get("", response_class=HTMLResponse)
def manage_home(request: Request, db: Session = Depends(get_db)):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    # All clients are exposed in a top-level "Add site" form, so we always need
    # the full list -- even when the operator just wants to click into a client.
    return _tpl(
        request, "manage_clients.html", db,
        user=user, clients=clients, flash=_pop_flash(request),
    )


@router.post("/clients")
def create_client(
    request: Request, name: str = Form(...), notes: str = Form(""), db: Session = Depends(get_db)
):
    if _manager(request, db) is None:
        return _redirect("/login")
    if name.strip():
        db.add(m.Client(name=name.strip(), notes=notes.strip() or None))
        db.commit()
        _flash(request, f"Client '{name}' added.")
    return _redirect("/manage")


@router.get("/clients/{client_id}", response_class=HTMLResponse)
def client_manage(client_id: int, request: Request, db: Session = Depends(get_db)):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client is None:
        return _redirect("/manage")
    printers = list(
        db.scalars(select(m.Printer).where(m.Printer.client_id == client_id).order_by(m.Printer.ip))
    )
    return _tpl(
        request, "client_manage.html", db,
        user=user, client=client, sites=client.sites,
        printers=printers, flash=_pop_flash(request),
    )


@router.post("/clients/{client_id}")
def update_client(
    client_id: int, request: Request,
    name: str = Form(...), notes: str = Form(""), db: Session = Depends(get_db),
):
    if _manager(request, db) is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client:
        client.name = name.strip() or client.name
        client.notes = notes.strip() or None
        db.commit()
        _flash(request, "Client updated.")
    return _redirect(f"/manage/clients/{client_id}")


@router.post("/clients/{client_id}/delete")
def delete_client(client_id: int, request: Request, db: Session = Depends(get_db)):
    user = _manager(request, db)
    if user is None or user.role != m.UserRole.admin:
        _flash(request, "Only admins can delete clients.")
        return _redirect(f"/manage/clients/{client_id}")
    client = db.get(m.Client, client_id)
    if client:
        db.delete(client)
        db.commit()
        _flash(request, "Client deleted.")
    return _redirect("/manage")


@router.post("/sites")
def create_site(
    request: Request, client_id: int = Form(...), name: str = Form(...),
    address: str = Form(""), contact: str = Form(""), db: Session = Depends(get_db),
):
    if _manager(request, db) is None:
        return _redirect("/login")
    if name.strip():
        db.add(m.Site(
            client_id=client_id, name=name.strip(),
            address=address.strip() or None, contact=contact.strip() or None,
        ))
        db.commit()
        _flash(request, f"Site '{name}' added.")
    return _redirect(f"/manage/clients/{client_id}")


@router.post("/sites/{site_id}/delete")
def delete_site(site_id: int, request: Request, db: Session = Depends(get_db)):
    user = _manager(request, db)
    site = db.get(m.Site, site_id)
    if user is None or site is None:
        return _redirect("/manage")
    client_id = site.client_id
    if user.role != m.UserRole.admin:
        _flash(request, "Only admins can delete sites.")
    else:
        db.delete(site)
        db.commit()
        _flash(request, "Site deleted.")
    return _redirect(f"/manage/clients/{client_id}")


# --------------------------------------------------------------------------- #
# Printers (manual add / edit)
# --------------------------------------------------------------------------- #
@router.get("/printers/new", response_class=HTMLResponse)
def printer_new(
    request: Request, client_id: int, site_id: Optional[int] = None, db: Session = Depends(get_db)
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client is None:
        return _redirect("/manage")
    return _tpl(
        request, "printer_form.html", db,
        user=user, client=client, sites=client.sites,
        printer=None, selected_site_id=site_id,
    )


@router.get("/printers/{printer_id}/edit", response_class=HTMLResponse)
def printer_edit(
    printer_id: int, request: Request,
    from_approvals: int = 0,
    db: Session = Depends(get_db),
):
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer is None:
        return _redirect("/manage")
    client = db.get(m.Client, printer.client_id)
    return _tpl(
        request, "printer_form.html", db,
        user=user, client=client, sites=client.sites,
        printer=printer, selected_site_id=printer.site_id,
        from_approvals=bool(from_approvals),
    )


@router.post("/printers")
def printer_create(
    request: Request,
    client_id: int = Form(...), site_id: int = Form(...), ip: str = Form(...),
    hostname: str = Form(""), brand: str = Form(""), model: str = Form(""),
    serial: str = Form(""), location: str = Form(""),
    snmp_version: str = Form("2c"), snmp_community: str = Form("public"),
    asset_tag: str = Form(""), tags: str = Form(""), notes: str = Form(""),
    db: Session = Depends(get_db),
):
    if _manager(request, db) is None:
        return _redirect("/login")
    printer = m.Printer(
        client_id=client_id, site_id=site_id, ip=ip.strip(),
        hostname=hostname.strip() or None, brand=brand.strip() or None,
        model=model.strip() or None, serial=serial.strip() or None,
        location=location.strip() or None, snmp_version=snmp_version,
        snmp_community=snmp_community.strip() or "public",
        asset_tag=asset_tag.strip() or None,
        tags=_split_tags(tags),
        notes=notes.strip() or None,
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.commit()
    _flash(request, f"Printer {ip} added.")
    return _redirect(f"/manage/clients/{client_id}")


@router.post("/printers/{printer_id}")
def printer_update(
    printer_id: int, request: Request,
    site_id: int = Form(...), ip: str = Form(...), hostname: str = Form(""),
    brand: str = Form(""), model: str = Form(""), serial: str = Form(""),
    location: str = Form(""), snmp_version: str = Form("2c"),
    snmp_community: str = Form("public"),
    asset_tag: str = Form(""), tags: str = Form(""), notes: str = Form(""),
    approve: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save printer edits. If ``approve=1`` and the printer is pending, also approve it."""
    if _manager(request, db) is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer:
        printer.site_id = site_id
        printer.ip = ip.strip()
        printer.hostname = hostname.strip() or None
        printer.brand = brand.strip() or None
        printer.model = model.strip() or None
        printer.serial = serial.strip() or None
        printer.location = location.strip() or None
        printer.snmp_version = snmp_version
        printer.snmp_community = snmp_community.strip() or "public"
        printer.asset_tag = asset_tag.strip() or None
        printer.tags = _split_tags(tags)
        printer.notes = notes.strip() or None
        approved_now = False
        if approve and printer.discovery_state != m.DiscoveryState.approved:
            printer.discovery_state = m.DiscoveryState.approved
            approved_now = True
        db.commit()
        _flash(request, "Printer approved." if approved_now else "Printer updated.")
        # After approval (typically reached from /approvals), bounce back there
        # so the operator can keep working through the queue.
        if approved_now:
            return _redirect("/approvals")
        return _redirect(f"/manage/clients/{printer.client_id}")
    return _redirect("/manage")


@router.post("/printers/{printer_id}/delete")
def printer_delete(printer_id: int, request: Request, db: Session = Depends(get_db)):
    user = _manager(request, db)
    printer = db.get(m.Printer, printer_id)
    if user is None or printer is None:
        return _redirect("/manage")
    client_id = printer.client_id
    db.delete(printer)
    db.commit()
    _flash(request, "Printer deleted.")
    return _redirect(f"/manage/clients/{client_id}")


@router.post("/printers/{printer_id}/ignore")
def printer_ignore(printer_id: int, request: Request, db: Session = Depends(get_db)):
    """Move a printer back to the ignored state so the agent stops polling it."""
    if _manager(request, db) is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer:
        printer.discovery_state = m.DiscoveryState.ignored
        db.commit()
        _flash(request, f"Stopped monitoring printer {printer.ip}.")
        return _redirect(f"/manage/clients/{printer.client_id}")
    return _redirect("/manage")


@router.post("/printers/{printer_id}/approve")
def printer_approve(printer_id: int, request: Request, db: Session = Depends(get_db)):
    """One-click approve from the detail page (no field edits)."""
    if _manager(request, db) is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer:
        printer.discovery_state = m.DiscoveryState.approved
        db.commit()
        _flash(request, f"Printer {printer.ip} approved.")
        return _redirect(f"/printers/{printer.id}")
    return _redirect("/manage")


@router.post("/printers/{printer_id}/poll")
def printer_poll_now(printer_id: int, request: Request, db: Session = Depends(get_db)):
    """Enqueue an immediate poll for one printer on its owning agent.

    The agent picks the command up on its next heartbeat and polls just this IP,
    so an operator can refresh a single device without waiting for the normal
    cycle. Falls back to any agent at the printer's site if no discoverer is set.
    """
    if _manager(request, db) is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer is None:
        return _redirect("/manage")

    agent_id = printer.discovered_by_agent_id
    if agent_id is None:
        agent = db.scalar(select(m.Agent).where(m.Agent.site_id == printer.site_id))
        agent_id = agent.id if agent else None
    if agent_id is None:
        _flash(request, "No agent is assigned to this site -- cannot poll.")
        return _redirect(f"/printers/{printer.id}")

    db.add(m.Command(
        agent_id=agent_id,
        type=m.CommandType.poll_printer,
        payload={"printer_id": printer.id, "ip": printer.ip},
    ))
    db.commit()
    _flash(request, "Poll queued. The agent will refresh this printer on its next heartbeat.")
    return _redirect(f"/printers/{printer.id}")


# --------------------------------------------------------------------------- #
# Agents & subnets (enrollment)
# --------------------------------------------------------------------------- #
@router.get("/agents", response_class=HTMLResponse)
def agents_home(request: Request, db: Session = Depends(get_db)):
    from central.runtime import load_settings

    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    agents = list(db.scalars(select(m.Agent).order_by(m.Agent.id)))
    sites = list(db.scalars(select(m.Site).order_by(m.Site.name)))
    rt = load_settings(db)
    # Prefer the operator-pinned public URL so the agent install command always
    # uses the public HTTPS hostname even if this request hit the API directly
    # via an internal address. Falls back to the request URL otherwise.
    public_url = (rt.get("app.public_url") or str(request.base_url)).rstrip("/")
    return _tpl(
        request, "agents.html", db,
        user=user, agents=agents, sites=sites,
        new_key=_keystore.pop(request.session.pop("new_agent_key_token", None)),
        central_url=public_url,
        pip_source=rt["agent.pip_source"],
        docker_image=rt["agent.docker_image"],
        flash=_pop_flash(request),
    )


@router.post("/agents")
def agent_create(
    request: Request, site_id: int = Form(...), name: str = Form(...),
    db: Session = Depends(get_db),
):
    if _manager(request, db) is None:
        return _redirect("/login")
    api_key = generate_api_key()
    agent = m.Agent(site_id=site_id, name=name.strip() or "agent", api_key_hash=hash_api_key(api_key))
    db.add(agent)
    db.commit()
    # Surface the plaintext key exactly once. Keep it server-side (not in the
    # signed-but-readable session cookie); the session holds only a one-shot token.
    request.session["new_agent_key_token"] = _keystore.put(
        {"id": agent.id, "name": agent.name, "key": api_key}
    )
    return _redirect("/manage/agents")


@router.post("/agents/{agent_id}/rotate-key")
def agent_rotate_key(agent_id: int, request: Request, db: Session = Depends(get_db)):
    """Issue a fresh API key for an agent (e.g. if the original was lost)."""
    if _manager(request, db) is None:
        return _redirect("/login")
    agent = db.get(m.Agent, agent_id)
    if agent:
        api_key = generate_api_key()
        agent.api_key_hash = hash_api_key(api_key)
        db.commit()
        request.session["new_agent_key_token"] = _keystore.put(
            {"id": agent.id, "name": agent.name, "key": api_key}
        )
    return _redirect("/manage/agents")


@router.post("/agents/{agent_id}/update")
def agent_update_command(agent_id: int, request: Request, db: Session = Depends(get_db)):
    """Queue an update_agent command. The agent picks it up on its next
    heartbeat (~60s), pip-installs the configured agent.pip_source, then exits
    so the service manager restarts it against the new code.

    Operator-driven only: there's no automatic rolling-update story yet (the
    design doc lists "secure auto-update path eventually" -- this is the
    eventually). A confirmation dialog is in the template.
    """
    if _manager(request, db) is None:
        return _redirect("/login")
    agent = db.get(m.Agent, agent_id)
    if agent is None:
        _flash(request, "Agent not found.")
        return _redirect("/manage/agents")
    from central.runtime import load_settings
    rt = load_settings(db)
    pip_source = str(rt.get("agent.pip_source") or "").strip()
    if not pip_source or "your-org" in pip_source:
        _flash(
            request,
            "Set Settings -> Agent install -> Pip source to your real repo "
            "before pushing updates; the placeholder won't install.",
        )
        return _redirect("/manage/agents")
    db.add(m.Command(
        agent_id=agent.id,
        type=m.CommandType.update_agent,
        payload={"pip_source": pip_source},
    ))
    db.commit()
    _flash(request, f"Update queued for '{agent.name}' (picks up on next heartbeat).")
    return _redirect("/manage/agents")


@router.post("/agents/update-all")
def agents_update_all(request: Request, db: Session = Depends(get_db)):
    """Queue update_agent for every enrolled agent. Admin only -- one command
    per agent so a single failure doesn't cascade."""
    user = _manager(request, db)
    if user is None or user.role != m.UserRole.admin:
        _flash(request, "Only admins can mass-update agents.")
        return _redirect("/manage/agents")
    from central.runtime import load_settings
    rt = load_settings(db)
    pip_source = str(rt.get("agent.pip_source") or "").strip()
    if not pip_source or "your-org" in pip_source:
        _flash(
            request,
            "Set Settings -> Agent install -> Pip source to your real repo first.",
        )
        return _redirect("/manage/agents")
    agents = list(db.scalars(select(m.Agent)))
    for agent in agents:
        db.add(m.Command(
            agent_id=agent.id,
            type=m.CommandType.update_agent,
            payload={"pip_source": pip_source},
        ))
    db.commit()
    _flash(request, f"Update queued for {len(agents)} agent(s).")
    return _redirect("/manage/agents")


@router.post("/agents/{agent_id}/delete")
def agent_delete(agent_id: int, request: Request, db: Session = Depends(get_db)):
    user = _manager(request, db)
    if user is None or user.role != m.UserRole.admin:
        _flash(request, "Only admins can delete agents.")
        return _redirect("/manage/agents")
    agent = db.get(m.Agent, agent_id)
    if agent:
        db.delete(agent)
        db.commit()
        _flash(request, "Agent deleted.")
    return _redirect("/manage/agents")


@router.post("/agents/{agent_id}/subnets")
def subnet_add(
    agent_id: int, request: Request, cidr: str = Form(...),
    snmp_community: str = Form("public"), snmp_version: str = Form("2c"),
    bind_interface: str = Form(""),
    site_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a subnet and bind it to this agent.

    By default the subnet lives at the agent's home site. Pass an explicit
    ``site_id`` to create the subnet at a DIFFERENT site -- the multi-client
    pattern where one HQ agent collects for several client sites whose
    tunnels are terminated locally.
    """
    if _manager(request, db) is None:
        return _redirect("/login")
    agent = db.get(m.Agent, agent_id)
    if agent and cidr.strip():
        effective_site_id = agent.site_id
        if site_id.strip():
            try:
                effective_site_id = int(site_id)
            except ValueError:
                pass
        db.add(m.Subnet(
            site_id=effective_site_id, agent_id=agent.id, cidr=cidr.strip(),
            snmp_community=snmp_community.strip() or "public",
            snmp_version=snmp_version,
            bind_interface=bind_interface.strip() or None,
        ))
        db.commit()
        _flash(request, f"Subnet {cidr} assigned.")
    return _redirect("/manage/agents")


@router.post("/subnets/{subnet_id}/delete")
def subnet_delete(subnet_id: int, request: Request, db: Session = Depends(get_db)):
    if _manager(request, db) is None:
        return _redirect("/login")
    subnet = db.get(m.Subnet, subnet_id)
    if subnet:
        db.delete(subnet)
        db.commit()
        _flash(request, "Subnet removed.")
    return _redirect("/manage/agents")


@router.post("/subnets/{subnet_id}")
def subnet_update(
    subnet_id: int, request: Request,
    label: str = Form(""),
    snmp_community: str = Form(""),
    snmp_version: str = Form(""),
    bind_interface: str = Form(""),
    agent_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """Edit a subnet's friendly label, SNMP creds, source-bind address, and
    optionally reassign it to a different agent (potentially in a different
    site -- one agent at HQ can collect for several client sites whose
    tunnels are terminated there).

    Empty fields are ignored so the inline label-edit form on the agents page
    doesn't accidentally wipe other settings when an operator just renames.
    """
    if _manager(request, db) is None:
        return _redirect("/login")
    subnet = db.get(m.Subnet, subnet_id)
    if subnet:
        subnet.label = label.strip() or None
        if snmp_community.strip():
            subnet.snmp_community = snmp_community.strip()
        if snmp_version.strip():
            subnet.snmp_version = snmp_version.strip()
        # bind_interface: empty string clears it (one explicit interface ->
        # OS default route); operator can intentionally remove it.
        subnet.bind_interface = bind_interface.strip() or None
        # agent_id: optional reassignment. Accept any agent regardless of
        # site -- that's the whole point of the multi-client agent path.
        if agent_id.strip():
            try:
                new_agent = db.get(m.Agent, int(agent_id))
            except ValueError:
                new_agent = None
            if new_agent is not None:
                subnet.agent_id = new_agent.id
        db.commit()
        _flash(request, f"Subnet {subnet.cidr} updated.")
    return _redirect("/manage/agents")


# --------------------------------------------------------------------------- #
# Users (admin only)
# --------------------------------------------------------------------------- #
def _coerce_role(raw: str) -> m.UserRole:
    try:
        return m.UserRole(raw)
    except ValueError:
        return m.UserRole.tech


@router.get("/users", response_class=HTMLResponse)
def users_home(request: Request, db: Session = Depends(get_db)):
    if _admin(request, db) is None:
        return _redirect("/login" if _user(request, db) is None else "/")
    users = list(db.scalars(select(m.User).order_by(m.User.username)))
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    return _tpl(
        request, "manage_users.html", db,
        user=_admin(request, db), users=users, clients=clients,
        roles=[r.value for r in m.UserRole],
        flash=_pop_flash(request),
    )


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
def user_edit(user_id: int, request: Request, db: Session = Depends(get_db)):
    if _admin(request, db) is None:
        return _redirect("/login" if _user(request, db) is None else "/")
    target = db.get(m.User, user_id)
    if target is None:
        return _redirect("/manage/users")
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    return _tpl(
        request, "user_form.html", db,
        user=_admin(request, db), target=target, clients=clients,
        roles=[r.value for r in m.UserRole],
    )


@router.post("/users")
def user_create(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    role: str = Form("tech"),
    client_id: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    if _admin(request, db) is None:
        return _redirect("/login")
    username = username.strip()
    if not username:
        _flash(request, "Username is required.")
        return _redirect("/manage/users")
    if db.scalar(select(m.User).where(m.User.username == username)) is not None:
        _flash(request, f"Username '{username}' is already taken.")
        return _redirect("/manage/users")
    role_enum = _coerce_role(role)
    # client_readonly users MUST be pinned to a client -- otherwise they'd see
    # every client (defeating the role's purpose). Other roles ignore client_id.
    pinned_client_id: Optional[int] = None
    if client_id.strip():
        try:
            pinned_client_id = int(client_id)
        except ValueError:
            pinned_client_id = None
    if role_enum == m.UserRole.client_readonly and pinned_client_id is None:
        _flash(request, "client_readonly users must be assigned to a client.")
        return _redirect("/manage/users")
    new_user = m.User(
        username=username,
        email=email.strip() or None,
        password_hash=hash_password(password) if password else None,
        role=role_enum,
        client_id=pinned_client_id if role_enum == m.UserRole.client_readonly else None,
        auth_provider="local" if password else "oidc",
    )
    db.add(new_user)
    db.commit()
    _flash(request, f"User '{username}' created.")
    return _redirect("/manage/users")


@router.post("/users/{user_id}")
def user_update(
    user_id: int, request: Request,
    email: str = Form(""),
    role: str = Form("tech"),
    client_id: str = Form(""),
    db: Session = Depends(get_db),
):
    actor = _admin(request, db)
    if actor is None:
        return _redirect("/login")
    target = db.get(m.User, user_id)
    if target is None:
        return _redirect("/manage/users")
    new_role = _coerce_role(role)
    # Last-admin guard: refuse to demote the only remaining admin (lockout).
    if (target.role == m.UserRole.admin and new_role != m.UserRole.admin
            and db.query(m.User).filter_by(role=m.UserRole.admin).count() <= 1):
        _flash(request, "Refused: this is the only admin. Promote another user first.")
        return _redirect(f"/manage/users/{user_id}/edit")
    pinned_client_id: Optional[int] = None
    if client_id.strip():
        try:
            pinned_client_id = int(client_id)
        except ValueError:
            pinned_client_id = None
    if new_role == m.UserRole.client_readonly and pinned_client_id is None:
        _flash(request, "client_readonly users must be assigned to a client.")
        return _redirect(f"/manage/users/{user_id}/edit")
    target.email = email.strip() or None
    target.role = new_role
    target.client_id = pinned_client_id if new_role == m.UserRole.client_readonly else None
    db.commit()
    _flash(request, f"User '{target.username}' updated.")
    return _redirect("/manage/users")


@router.post("/users/{user_id}/reset-password")
def user_reset_password(
    user_id: int, request: Request,
    new_password: str = Form(...),
    db: Session = Depends(get_db),
):
    if _admin(request, db) is None:
        return _redirect("/login")
    target = db.get(m.User, user_id)
    if target is None:
        return _redirect("/manage/users")
    if len(new_password) < 8:
        _flash(request, "Password must be at least 8 characters.")
        return _redirect(f"/manage/users/{user_id}/edit")
    target.password_hash = hash_password(new_password)
    target.auth_provider = "local"  # they can now sign in locally
    db.commit()
    _flash(request, f"Password reset for '{target.username}'.")
    return _redirect("/manage/users")


@router.post("/users/{user_id}/delete")
def user_delete(user_id: int, request: Request, db: Session = Depends(get_db)):
    actor = _admin(request, db)
    if actor is None:
        return _redirect("/login")
    target = db.get(m.User, user_id)
    if target is None:
        return _redirect("/manage/users")
    if actor.id == target.id:
        _flash(request, "Refused: you can't delete the account you're logged in as.")
        return _redirect("/manage/users")
    if (target.role == m.UserRole.admin
            and db.query(m.User).filter_by(role=m.UserRole.admin).count() <= 1):
        _flash(request, "Refused: this is the only admin.")
        return _redirect("/manage/users")
    db.delete(target)
    db.commit()
    _flash(request, f"User '{target.username}' deleted.")
    return _redirect("/manage/users")
