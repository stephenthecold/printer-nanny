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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from central import models as m
from central.audit import record
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
    from central import __version__ as _central_version

    ctx.setdefault("app", app_branding(db))
    ctx.setdefault("central_version", _central_version)
    # Conditional Approvals nav: link only renders when something is pending.
    if "nav_pending" not in ctx:
        ctx["nav_pending"] = db.scalar(
            select(func.count())
            .select_from(m.Printer)
            .where(m.Printer.discovery_state == m.DiscoveryState.pending)
        ) or 0
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
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    if name.strip():
        db.add(m.Client(name=name.strip(), notes=notes.strip() or None))
        record(db, request, actor, "client.create", target=f"client:{name.strip()}")
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
        record(db, request, _manager(request, db), "client.update",
               target=f"client:{client.id} {client.name}")
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
        record(db, request, user, "client.delete",
               target=f"client:{client.id} {client.name}")
        db.delete(client)
        db.commit()
        _flash(request, "Client deleted.")
    return _redirect("/manage")


@router.post("/sites")
def create_site(
    request: Request, client_id: int = Form(...), name: str = Form(...),
    address: str = Form(""), contact: str = Form(""), db: Session = Depends(get_db),
):
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    if name.strip():
        db.add(m.Site(
            client_id=client_id, name=name.strip(),
            address=address.strip() or None, contact=contact.strip() or None,
        ))
        record(db, request, actor, "site.create",
               target=f"site:{name.strip()} (client:{client_id})")
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
        record(db, request, user, "site.delete",
               target=f"site:{site.id} {site.name}")
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
    display_name: str = Form(""),
    db: Session = Depends(get_db),
):
    if _manager(request, db) is None:
        return _redirect("/login")
    printer = m.Printer(
        client_id=client_id, site_id=site_id, ip=ip.strip(),
        display_name=display_name.strip() or None,
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
    record(db, request, _manager(request, db), "printer.create",
           target=f"printer:{printer.ip} (client:{client_id})")
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
    display_name: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save printer edits. If ``approve=1`` and the printer is pending, also approve it."""
    if _manager(request, db) is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer:
        printer.site_id = site_id
        printer.ip = ip.strip()
        printer.display_name = display_name.strip() or None
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
        record(db, request, _manager(request, db),
               "printer.approve" if approved_now else "printer.update",
               target=f"printer:{printer.id} {printer.ip}")
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
    record(db, request, user, "printer.delete",
           target=f"printer:{printer.id} {printer.ip}")
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
        record(db, request, _manager(request, db), "printer.ignore",
               target=f"printer:{printer.id} {printer.ip}")
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
        record(db, request, _manager(request, db), "printer.approve",
               target=f"printer:{printer.id} {printer.ip}")
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
    record(db, request, _manager(request, db), "printer.poll_now",
           target=f"printer:{printer.id} {printer.ip}")
    db.commit()
    _flash(request, "Poll queued. The agent will refresh this printer on its next heartbeat.")
    return _redirect(f"/printers/{printer.id}")


# --------------------------------------------------------------------------- #
# Agents & subnets (enrollment)
# --------------------------------------------------------------------------- #
@router.get("/agents", response_class=HTMLResponse)
def agents_home(request: Request, db: Session = Depends(get_db)):
    from central import __version__ as central_version
    from central.runtime import load_settings

    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    agents = list(db.scalars(select(m.Agent).order_by(m.Agent.id)))
    sites = list(db.scalars(
        select(m.Site).join(m.Client).order_by(m.Client.name, m.Site.name)
    ))
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    rt = load_settings(db)
    # Prefer the operator-pinned public URL so the agent install command always
    # uses the public HTTPS hostname even if this request hit the API directly
    # via an internal address. Falls back to the request URL otherwise.
    public_url = (rt.get("app.public_url") or str(request.base_url)).rstrip("/")
    # Group sites by client so the cross-site subnet picker can render an
    # optgroup-style picker -- making the multi-client agent pattern visible.
    sites_by_client: dict[int, list[m.Site]] = {}
    for site in sites:
        sites_by_client.setdefault(site.client_id, []).append(site)
    # Discovery status lives here now (the standalone Discovery page folded
    # in): per-site pending-approval counts so each subnet row can show how
    # many discovered devices are waiting.
    pending_by_site = {
        site_id: count
        for site_id, count in db.execute(
            select(m.Printer.site_id, func.count())
            .where(m.Printer.discovery_state == m.DiscoveryState.pending)
            .group_by(m.Printer.site_id)
        ).all()
    }
    return _tpl(
        request, "agents.html", db,
        user=user, agents=agents, sites=sites,
        clients=clients,
        sites_by_client=sites_by_client,
        pending_by_site=pending_by_site,
        new_key=_keystore.pop(request.session.pop("new_agent_key_token", None)),
        central_url=public_url,
        pip_source=rt["agent.pip_source"],
        docker_image=rt["agent.docker_image"],
        central_version=central_version,
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
    record(db, request, _manager(request, db), "agent.create",
           target=f"agent:{name.strip() or 'agent'} (site:{site_id})")
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
        record(db, request, _manager(request, db), "agent.rotate_key",
               target=f"agent:{agent.id} {agent.name}")
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
    record(db, request, _manager(request, db), "agent.update_queued",
           target=f"agent:{agent.id} {agent.name}", detail=pip_source)
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
    record(db, request, user, "agent.update_all",
           detail=f"{len(agents)} agent(s); source={pip_source}")
    db.commit()
    _flash(request, f"Update queued for {len(agents)} agent(s).")
    return _redirect("/manage/agents")


@router.post("/agents/{agent_id}/rescan")
def agent_rescan(agent_id: int, request: Request, db: Session = Depends(get_db)):
    """Queue a discovery rescan for this agent (picked up on next heartbeat).

    Mirror of POST /discovery/agents/{agent_id}/rescan but lives on /manage/agents
    so the operator doesn't have to leave the Agents page to trigger a sweep.
    """
    if _manager(request, db) is None:
        return _redirect("/login")
    agent = db.get(m.Agent, agent_id)
    if agent is None:
        _flash(request, "Agent not found.")
        return _redirect("/manage/agents")
    db.add(m.Command(agent_id=agent.id, type=m.CommandType.rescan, payload=None))
    record(db, request, _manager(request, db), "agent.rescan",
           target=f"agent:{agent.id} {agent.name}")
    db.commit()
    _flash(
        request,
        f"Rescan queued for '{agent.name}' (picks up on next heartbeat ~60s).",
    )
    return _redirect("/manage/agents")


@router.post("/agents/{agent_id}/poll-now")
def agent_poll_now(agent_id: int, request: Request, db: Session = Depends(get_db)):
    """Queue a full poll cycle for this agent (every approved printer it serves).

    Cuts the wait from the poll interval (default 5 min) down to the heartbeat
    interval (default 60s) so a tech can verify a fix landed without standing
    around.
    """
    if _manager(request, db) is None:
        return _redirect("/login")
    agent = db.get(m.Agent, agent_id)
    if agent is None:
        _flash(request, "Agent not found.")
        return _redirect("/manage/agents")
    db.add(m.Command(agent_id=agent.id, type=m.CommandType.poll_now, payload=None))
    record(db, request, _manager(request, db), "agent.poll_now",
           target=f"agent:{agent.id} {agent.name}")
    db.commit()
    _flash(
        request,
        f"Poll-now queued for '{agent.name}' (picks up on next heartbeat ~60s).",
    )
    return _redirect("/manage/agents")


@router.post("/agents/{agent_id}/delete")
def agent_delete(agent_id: int, request: Request, db: Session = Depends(get_db)):
    user = _manager(request, db)
    if user is None or user.role != m.UserRole.admin:
        _flash(request, "Only admins can delete agents.")
        return _redirect("/manage/agents")
    agent = db.get(m.Agent, agent_id)
    if agent:
        record(db, request, user, "agent.delete",
               target=f"agent:{agent.id} {agent.name}")
        db.delete(agent)
        db.commit()
        _flash(request, "Agent deleted.")
    return _redirect("/manage/agents")


def _build_v3_blob(
    *,
    user: str, security_level: str,
    auth_protocol: str, auth_password: str,
    priv_protocol: str, priv_password: str,
    context_name: str = "",
) -> Optional[dict]:
    """Build the snmp_v3 JSON blob from form fields. Returns None when no
    user was supplied (so toggling back to v1/v2c clears the blob)."""
    user = user.strip()
    if not user:
        return None
    blob = {
        "user": user,
        "security_level": security_level.strip() or "noAuthNoPriv",
    }
    from central.secrets import encrypt_value

    if auth_protocol.strip():
        blob["auth_protocol"] = auth_protocol.strip()
    if auth_password:
        # USM passwords are encrypted at rest; the agent-config endpoint
        # decrypts them on the way out to the (authenticated) agent.
        blob["auth_password"] = encrypt_value(auth_password)
    if priv_protocol.strip():
        blob["priv_protocol"] = priv_protocol.strip()
    if priv_password:
        blob["priv_password"] = encrypt_value(priv_password)
    if context_name.strip():
        blob["context_name"] = context_name.strip()
    return blob


@router.post("/agents/{agent_id}/subnets")
def subnet_add(
    agent_id: int, request: Request, cidr: str = Form(...),
    snmp_community: str = Form("public"), snmp_version: str = Form("2c"),
    bind_interface: str = Form(""),
    site_id: str = Form(""),
    snmp_v3_user: str = Form(""),
    snmp_v3_security_level: str = Form("noAuthNoPriv"),
    snmp_v3_auth_protocol: str = Form(""),
    snmp_v3_auth_password: str = Form(""),
    snmp_v3_priv_protocol: str = Form(""),
    snmp_v3_priv_password: str = Form(""),
    snmp_v3_context_name: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a subnet and bind it to this agent.

    By default the subnet lives at the agent's home site. Pass an explicit
    ``site_id`` to create the subnet at a DIFFERENT site -- the multi-client
    pattern where one HQ agent collects for several client sites whose
    tunnels are terminated locally.

    SNMPv3 fields are picked up when ``snmp_version="3"``; for v1/v2c they're
    ignored. The agent receives the v3 blob through the existing /config
    endpoint and uses it to build a USM auth context.
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
            snmp_v3=_build_v3_blob(
                user=snmp_v3_user,
                security_level=snmp_v3_security_level,
                auth_protocol=snmp_v3_auth_protocol,
                auth_password=snmp_v3_auth_password,
                priv_protocol=snmp_v3_priv_protocol,
                priv_password=snmp_v3_priv_password,
                context_name=snmp_v3_context_name,
            ),
        ))
        record(db, request, _manager(request, db), "subnet.create",
               target=f"subnet:{cidr.strip()} agent:{agent.id}",
               detail=f"snmp v{snmp_version}")
        db.commit()
        _flash(request, f"Subnet {cidr} assigned.")
    return _redirect("/manage/agents")


@router.post("/subnets/{subnet_id}/delete")
def subnet_delete(subnet_id: int, request: Request, db: Session = Depends(get_db)):
    if _manager(request, db) is None:
        return _redirect("/login")
    subnet = db.get(m.Subnet, subnet_id)
    if subnet:
        record(db, request, _manager(request, db), "subnet.delete",
               target=f"subnet:{subnet.id} {subnet.cidr}")
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
    snmp_v3_user: str = Form(""),
    snmp_v3_security_level: str = Form(""),
    snmp_v3_auth_protocol: str = Form(""),
    snmp_v3_auth_password: str = Form(""),
    snmp_v3_priv_protocol: str = Form(""),
    snmp_v3_priv_password: str = Form(""),
    snmp_v3_context_name: str = Form(""),
    snmp_v3_clear: str = Form(""),
    db: Session = Depends(get_db),
):
    """Edit a subnet's friendly label, SNMP creds, source-bind address, and
    optionally reassign it to a different agent (potentially in a different
    site -- one agent at HQ can collect for several client sites whose
    tunnels are terminated there).

    Empty fields are ignored so the inline label-edit form on the agents page
    doesn't accidentally wipe other settings when an operator just renames.
    ``snmp_v3_clear=1`` is the explicit "blow away v3 creds" signal -- without
    it, omitting v3 form fields keeps the existing creds.
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
        # SNMPv3: explicit clear vs. partial update. A partial v3 update where
        # only ``snmp_v3_user`` is present means the operator wants to rebuild
        # the blob from these form values. If no v3 fields at all are present
        # we leave the existing blob alone (so renaming a subnet doesn't blow
        # away creds).
        if snmp_v3_clear.strip():
            subnet.snmp_v3 = None
        elif snmp_v3_user.strip():
            subnet.snmp_v3 = _build_v3_blob(
                user=snmp_v3_user,
                security_level=snmp_v3_security_level or "noAuthNoPriv",
                auth_protocol=snmp_v3_auth_protocol,
                auth_password=snmp_v3_auth_password,
                priv_protocol=snmp_v3_priv_protocol,
                priv_password=snmp_v3_priv_password,
                context_name=snmp_v3_context_name,
            )
        # agent_id: optional reassignment. Accept any agent regardless of
        # site -- that's the whole point of the multi-client agent path.
        if agent_id.strip():
            try:
                new_agent = db.get(m.Agent, int(agent_id))
            except ValueError:
                new_agent = None
            if new_agent is not None:
                subnet.agent_id = new_agent.id
        record(db, request, _manager(request, db), "subnet.update",
               target=f"subnet:{subnet.id} {subnet.cidr}")
        db.commit()
        _flash(request, f"Subnet {subnet.cidr} updated.")
    return _redirect("/manage/agents")


# --------------------------------------------------------------------------- #
# Maintenance schedules (admin/tech) + service-log entries
# --------------------------------------------------------------------------- #
def _parse_date(raw: str):
    """YYYY-MM-DD -> tz-aware datetime, or None. The form's <input type='date'>
    posts in that shape; tolerated naive bare-date strings just as well."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    if not raw or not raw.strip():
        return None
    try:
        d = _dt.strptime(raw.strip(), "%Y-%m-%d")
        return d.replace(tzinfo=_tz.utc)
    except ValueError:
        return None


@router.get("/maintenance", response_class=HTMLResponse)
def maintenance_home(request: Request, db: Session = Depends(get_db)):
    """All schedules (per-printer or model-wide) + recent service-log entries.

    Drives the operator side of the worker's maintenance-due alert
    pipeline: rolling next_due forward (by logging a service entry, or
    editing inline) auto-resolves the corresponding alert on the next
    worker cycle.
    """
    user = _manager(request, db)
    if user is None:
        return _redirect("/login")
    schedules = list(
        db.scalars(
            select(m.MaintenanceSchedule).order_by(
                m.MaintenanceSchedule.next_due.asc().nulls_last(),
                m.MaintenanceSchedule.id.desc(),
            )
        )
    )
    printers = list(db.scalars(
        select(m.Printer)
        .where(m.Printer.discovery_state == m.DiscoveryState.approved)
        .order_by(m.Printer.client_id, m.Printer.site_id, m.Printer.ip)
    ))
    records = list(
        db.scalars(
            select(m.MaintenanceRecord)
            .order_by(m.MaintenanceRecord.performed_at.desc())
            .limit(50)
        )
    )
    printers_by_id = {p.id: p for p in db.scalars(select(m.Printer))}
    return _tpl(
        request, "maintenance.html", db,
        user=user, schedules=schedules, printers=printers,
        records=records, printers_by_id=printers_by_id,
        types=[t.value for t in m.MaintenanceType],
        flash=_pop_flash(request),
    )


@router.post("/maintenance/schedules")
def schedule_create(
    request: Request,
    name: str = Form(...),
    printer_id: str = Form(""),
    model: str = Form(""),
    interval_days: str = Form(""),
    page_threshold: str = Form(""),
    next_due: str = Form(""),
    db: Session = Depends(get_db),
):
    """Either printer_id or model identifies the scope; both empty -> a
    model-wide schedule that matches every printer with that model. interval
    OR page threshold drives the worker's due-check (the worker considers
    a schedule due when next_due <= now AND page_count >= threshold)."""
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    name = name.strip()
    if not name:
        _flash(request, "Schedule name is required.")
        return _redirect("/manage/maintenance")
    pid: Optional[int] = None
    if printer_id.strip():
        try:
            pid = int(printer_id)
        except ValueError:
            pid = None
    try:
        interval = int(interval_days) if interval_days.strip() else None
    except ValueError:
        interval = None
    try:
        threshold = int(page_threshold) if page_threshold.strip() else None
    except ValueError:
        threshold = None
    sched = m.MaintenanceSchedule(
        name=name, printer_id=pid,
        model=model.strip() or None,
        interval_days=interval, page_threshold=threshold,
        next_due=_parse_date(next_due),
    )
    db.add(sched)
    record(db, request, actor, "maintenance_schedule.create",
           target=f"sched:{name}",
           detail=f"printer:{pid or '-'} model:{model.strip() or '-'} "
                  f"every:{interval or '-'}d threshold:{threshold or '-'}")
    db.commit()
    _flash(request, f"Schedule '{name}' added.")
    return _redirect("/manage/maintenance")


@router.post("/maintenance/schedules/{sched_id}")
def schedule_update(
    sched_id: int, request: Request,
    name: str = Form(""),
    interval_days: str = Form(""),
    page_threshold: str = Form(""),
    next_due: str = Form(""),
    db: Session = Depends(get_db),
):
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    sched = db.get(m.MaintenanceSchedule, sched_id)
    if sched is None:
        return _redirect("/manage/maintenance")
    if name.strip():
        sched.name = name.strip()
    try:
        sched.interval_days = int(interval_days) if interval_days.strip() else None
    except ValueError:
        pass
    try:
        sched.page_threshold = int(page_threshold) if page_threshold.strip() else None
    except ValueError:
        pass
    parsed = _parse_date(next_due)
    if parsed is not None or next_due == "":
        sched.next_due = parsed
    record(db, request, actor, "maintenance_schedule.update",
           target=f"sched:{sched.id} {sched.name}")
    db.commit()
    _flash(request, f"Schedule '{sched.name}' updated.")
    return _redirect("/manage/maintenance")


@router.post("/maintenance/schedules/{sched_id}/delete")
def schedule_delete(sched_id: int, request: Request, db: Session = Depends(get_db)):
    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    sched = db.get(m.MaintenanceSchedule, sched_id)
    if sched is not None:
        record(db, request, actor, "maintenance_schedule.delete",
               target=f"sched:{sched.id} {sched.name}")
        db.delete(sched)
        db.commit()
        _flash(request, f"Schedule '{sched.name}' removed.")
    return _redirect("/manage/maintenance")


@router.post("/maintenance/schedules/{sched_id}/log")
def schedule_log_service(
    sched_id: int, request: Request,
    performed_by: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Operator clicked 'Mark serviced': record a MaintenanceRecord and roll
    next_due forward by interval_days (when set). The worker's reconcile pass
    will see next_due > now on the next cycle and auto-resolve the
    maintenance-due alert."""
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz

    actor = _manager(request, db)
    if actor is None:
        return _redirect("/login")
    sched = db.get(m.MaintenanceSchedule, sched_id)
    if sched is None:
        return _redirect("/manage/maintenance")
    now = _dt.now(_tz.utc)
    next_due = (
        now + _td(days=sched.interval_days) if sched.interval_days else None
    )
    rec = m.MaintenanceRecord(
        printer_id=sched.printer_id,
        type=m.MaintenanceType.scheduled,
        performed_by=performed_by.strip() or actor.username,
        performed_at=now,
        notes=(notes.strip() or sched.name) + f" (schedule #{sched.id})",
        next_due=next_due,
    )
    db.add(rec)
    if next_due is not None:
        sched.next_due = next_due
    record(db, request, actor, "maintenance.log",
           target=f"sched:{sched.id} {sched.name}",
           detail=f"by:{performed_by.strip() or actor.username}")
    db.commit()
    _flash(request, f"Service logged for '{sched.name}'.")
    return _redirect("/manage/maintenance")


@router.post("/maintenance/records/{rec_id}/delete")
def record_delete(rec_id: int, request: Request, db: Session = Depends(get_db)):
    actor = _manager(request, db)
    if actor is None or actor.role != m.UserRole.admin:
        _flash(request, "Only admins can remove service records.")
        return _redirect("/manage/maintenance")
    rec = db.get(m.MaintenanceRecord, rec_id)
    if rec is not None:
        record(db, request, actor, "maintenance.record_delete",
               target=f"record:{rec.id} printer:{rec.printer_id}")
        db.delete(rec)
        db.commit()
        _flash(request, "Service record removed.")
    return _redirect("/manage/maintenance")


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


# --------------------------------------------------------------------------- #
# Audit trail (admin only)
# --------------------------------------------------------------------------- #
@router.get("/audit", response_class=HTMLResponse)
def audit_home(request: Request, q: str = "", db: Session = Depends(get_db)):
    """Latest audit rows, newest first. ``?q=`` filters by substring across
    action / target / username -- enough for 'what did tech2 touch last week'
    without building a query designer."""
    admin = _admin(request, db)
    if admin is None:
        return _redirect("/login" if _user(request, db) is None else "/")
    stmt = select(m.AuditLog).order_by(m.AuditLog.ts.desc()).limit(200)
    if q.strip():
        needle = f"%{q.strip()}%"
        stmt = (
            select(m.AuditLog)
            .where(
                m.AuditLog.action.ilike(needle)
                | m.AuditLog.target.ilike(needle)
                | m.AuditLog.username.ilike(needle)
            )
            .order_by(m.AuditLog.ts.desc())
            .limit(200)
        )
    rows = list(db.scalars(stmt))
    return _tpl(
        request, "audit.html", db,
        user=admin, rows=rows, q=q.strip(),
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
    record(db, request, _admin(request, db), "user.create",
           target=f"user:{username}", detail=f"role={role_enum.value}")
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
    record(db, request, actor, "user.update",
           target=f"user:{target.username}", detail=f"role={new_role.value}")
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
    record(db, request, _admin(request, db), "user.reset_password",
           target=f"user:{target.username}")
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
    record(db, request, actor, "user.delete",
           target=f"user:{target.username}", detail=f"role={target.role.value}")
    db.delete(target)
    db.commit()
    _flash(request, f"User '{target.username}' deleted.")
    return _redirect("/manage/users")
