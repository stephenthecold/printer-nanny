"""Management UI: create/edit/delete clients, sites, printers, and enroll agents.

Plain server-rendered forms (POST + redirect) — robust and JS-free. Viewing and
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
from central.db import get_db
from central.security import generate_api_key, hash_api_key

router = APIRouter(prefix="/manage", tags=["manage"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _user(request: Request, db: Session) -> Optional[m.User]:
    uid = request.session.get("user_id")
    return db.get(m.User, uid) if uid else None


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _pop_flash(request: Request) -> Optional[str]:
    return request.session.pop("flash", None)


# --------------------------------------------------------------------------- #
# Clients & sites
# --------------------------------------------------------------------------- #
@router.get("", response_class=HTMLResponse)
def manage_home(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _redirect("/login")
    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    return _templates.TemplateResponse(
        request, "manage_clients.html",
        {"user": user, "clients": clients, "flash": _pop_flash(request)},
    )


@router.post("/clients")
def create_client(
    request: Request, name: str = Form(...), notes: str = Form(""), db: Session = Depends(get_db)
):
    if _user(request, db) is None:
        return _redirect("/login")
    if name.strip():
        db.add(m.Client(name=name.strip(), notes=notes.strip() or None))
        db.commit()
        _flash(request, f"Client '{name}' added.")
    return _redirect("/manage")


@router.get("/clients/{client_id}", response_class=HTMLResponse)
def client_manage(client_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client is None:
        return _redirect("/manage")
    printers = list(
        db.scalars(select(m.Printer).where(m.Printer.client_id == client_id).order_by(m.Printer.ip))
    )
    return _templates.TemplateResponse(
        request, "client_manage.html",
        {"user": user, "client": client, "sites": client.sites,
         "printers": printers, "flash": _pop_flash(request)},
    )


@router.post("/clients/{client_id}")
def update_client(
    client_id: int, request: Request,
    name: str = Form(...), notes: str = Form(""), db: Session = Depends(get_db),
):
    if _user(request, db) is None:
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
    user = _user(request, db)
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
    if _user(request, db) is None:
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
    user = _user(request, db)
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
    user = _user(request, db)
    if user is None:
        return _redirect("/login")
    client = db.get(m.Client, client_id)
    if client is None:
        return _redirect("/manage")
    return _templates.TemplateResponse(
        request, "printer_form.html",
        {"user": user, "client": client, "sites": client.sites,
         "printer": None, "selected_site_id": site_id},
    )


@router.get("/printers/{printer_id}/edit", response_class=HTMLResponse)
def printer_edit(printer_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _redirect("/login")
    printer = db.get(m.Printer, printer_id)
    if printer is None:
        return _redirect("/manage")
    client = db.get(m.Client, printer.client_id)
    return _templates.TemplateResponse(
        request, "printer_form.html",
        {"user": user, "client": client, "sites": client.sites,
         "printer": printer, "selected_site_id": printer.site_id},
    )


@router.post("/printers")
def printer_create(
    request: Request,
    client_id: int = Form(...), site_id: int = Form(...), ip: str = Form(...),
    hostname: str = Form(""), brand: str = Form(""), model: str = Form(""),
    serial: str = Form(""), location: str = Form(""),
    snmp_version: str = Form("2c"), snmp_community: str = Form("public"),
    db: Session = Depends(get_db),
):
    if _user(request, db) is None:
        return _redirect("/login")
    printer = m.Printer(
        client_id=client_id, site_id=site_id, ip=ip.strip(),
        hostname=hostname.strip() or None, brand=brand.strip() or None,
        model=model.strip() or None, serial=serial.strip() or None,
        location=location.strip() or None, snmp_version=snmp_version,
        snmp_community=snmp_community.strip() or "public",
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
    snmp_community: str = Form("public"), db: Session = Depends(get_db),
):
    if _user(request, db) is None:
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
        db.commit()
        _flash(request, "Printer updated.")
        return _redirect(f"/manage/clients/{printer.client_id}")
    return _redirect("/manage")


@router.post("/printers/{printer_id}/delete")
def printer_delete(printer_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    printer = db.get(m.Printer, printer_id)
    if user is None or printer is None:
        return _redirect("/manage")
    client_id = printer.client_id
    db.delete(printer)
    db.commit()
    _flash(request, "Printer deleted.")
    return _redirect(f"/manage/clients/{client_id}")


# --------------------------------------------------------------------------- #
# Agents & subnets (enrollment)
# --------------------------------------------------------------------------- #
@router.get("/agents", response_class=HTMLResponse)
def agents_home(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _redirect("/login")
    agents = list(db.scalars(select(m.Agent).order_by(m.Agent.id)))
    sites = list(db.scalars(select(m.Site).order_by(m.Site.name)))
    return _templates.TemplateResponse(
        request, "agents.html",
        {"user": user, "agents": agents, "sites": sites,
         "new_key": request.session.pop("new_agent_key", None),
         "flash": _pop_flash(request)},
    )


@router.post("/agents")
def agent_create(
    request: Request, site_id: int = Form(...), name: str = Form(...),
    db: Session = Depends(get_db),
):
    if _user(request, db) is None:
        return _redirect("/login")
    api_key = generate_api_key()
    agent = m.Agent(site_id=site_id, name=name.strip() or "agent", api_key_hash=hash_api_key(api_key))
    db.add(agent)
    db.commit()
    # Surface the plaintext key exactly once (it's only stored hashed).
    request.session["new_agent_key"] = {"id": agent.id, "name": agent.name, "key": api_key}
    return _redirect("/manage/agents")


@router.post("/agents/{agent_id}/delete")
def agent_delete(agent_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
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
    db: Session = Depends(get_db),
):
    if _user(request, db) is None:
        return _redirect("/login")
    agent = db.get(m.Agent, agent_id)
    if agent and cidr.strip():
        db.add(m.Subnet(
            site_id=agent.site_id, agent_id=agent.id, cidr=cidr.strip(),
            snmp_community=snmp_community.strip() or "public", snmp_version=snmp_version,
        ))
        db.commit()
        _flash(request, f"Subnet {cidr} assigned.")
    return _redirect("/manage/agents")


@router.post("/subnets/{subnet_id}/delete")
def subnet_delete(subnet_id: int, request: Request, db: Session = Depends(get_db)):
    if _user(request, db) is None:
        return _redirect("/login")
    subnet = db.get(m.Subnet, subnet_id)
    if subnet:
        db.delete(subnet)
        db.commit()
        _flash(request, "Subnet removed.")
    return _redirect("/manage/agents")
