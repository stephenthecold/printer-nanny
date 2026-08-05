"""Management CRUD: clients, sites, subnets, agents, printers, maintenance, commands."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import schemas as s
from central.audit import record
from central.db import get_db
from central.deps import require_admin, require_staff
from central.runtime import load_settings
from central.security import generate_api_key, hash_api_key

# Management CRUD is operator-only. Before this gate the router merely required a
# logged-in user, so a client_readonly session could both read every tenant's
# clients/printers AND create/approve printers and enqueue agent commands.
router = APIRouter(prefix="/api/v1", tags=["management"], dependencies=[Depends(require_staff)])


def _get_or_404(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{model.__name__} {obj_id} not found")
    return obj


# --- Clients ---------------------------------------------------------------- #
@router.get("/clients", response_model=list[s.ClientOut])
def list_clients(db: Session = Depends(get_db)):
    return list(db.scalars(select(m.Client).order_by(m.Client.name)))


@router.post("/clients", response_model=s.ClientOut, status_code=201)
def create_client(
    payload: s.ClientIn, request: Request,
    db: Session = Depends(get_db), user: m.User = Depends(require_staff),
):
    client = m.Client(name=payload.name, notes=payload.notes)
    db.add(client)
    db.flush()
    record(db, request, user, "client.create", target=f"client:{client.id} {client.name}")
    db.commit()
    db.refresh(client)
    return client


# --- Sites ------------------------------------------------------------------ #
@router.get("/sites", response_model=list[s.SiteOut])
def list_sites(client_id: Optional[int] = None, db: Session = Depends(get_db)):
    stmt = select(m.Site)
    if client_id is not None:
        stmt = stmt.where(m.Site.client_id == client_id)
    return list(db.scalars(stmt.order_by(m.Site.name)))


@router.post("/sites", response_model=s.SiteOut, status_code=201)
def create_site(
    payload: s.SiteIn, request: Request,
    db: Session = Depends(get_db), user: m.User = Depends(require_staff),
):
    _get_or_404(db, m.Client, payload.client_id)
    site = m.Site(**payload.model_dump())
    db.add(site)
    db.flush()
    record(db, request, user, "site.create",
           target=f"site:{site.id} {site.name}", detail=f"client={site.client_id}")
    db.commit()
    db.refresh(site)
    return site


# --- Subnets ---------------------------------------------------------------- #
@router.get("/subnets", response_model=list[s.SubnetOut])
def list_subnets(site_id: Optional[int] = None, db: Session = Depends(get_db)):
    stmt = select(m.Subnet)
    if site_id is not None:
        stmt = stmt.where(m.Subnet.site_id == site_id)
    return list(db.scalars(stmt))


@router.post("/subnets", response_model=s.SubnetOut, status_code=201)
def create_subnet(
    payload: s.SubnetIn, request: Request,
    db: Session = Depends(get_db), user: m.User = Depends(require_staff),
):
    _get_or_404(db, m.Site, payload.site_id)
    subnet = m.Subnet(**payload.model_dump())
    db.add(subnet)
    db.flush()
    # The CIDR, never the community string: audit detail is rendered in the UI
    # and travels in diagnostics.
    record(db, request, user, "subnet.create",
           target=f"subnet:{subnet.id} {subnet.cidr}", detail=f"site={subnet.site_id}")
    db.commit()
    db.refresh(subnet)
    return subnet


# --- Agents ----------------------------------------------------------------- #
@router.get("/agents", response_model=list[s.AgentOut])
def list_agents(site_id: Optional[int] = None, db: Session = Depends(get_db)):
    stmt = select(m.Agent)
    if site_id is not None:
        stmt = stmt.where(m.Agent.site_id == site_id)
    return list(db.scalars(stmt))


@router.post("/agents", response_model=s.AgentCreated, status_code=201)
def create_agent(
    payload: s.AgentIn, request: Request,
    db: Session = Depends(get_db), user: m.User = Depends(require_staff),
):
    """Create an agent and return its API key ONCE (only the hash is stored)."""
    _get_or_404(db, m.Site, payload.site_id)
    api_key = generate_api_key()
    agent = m.Agent(
        site_id=payload.site_id, name=payload.name, api_key_hash=hash_api_key(api_key)
    )
    db.add(agent)
    db.flush()
    # This route mints a long-lived agent credential and returns it in plaintext,
    # and it recorded NOTHING -- while the dashboard route doing the same thing
    # records `agent.create`. A tech-role session could therefore mint a
    # credential for any tenant and leave no trail, which also made CLAUDE.md's
    # claim that agent CRUD is audited false for half the ways to reach it.
    # The key is never in the detail: only the hash is stored anywhere.
    record(db, request, user, "agent.create",
           target=f"agent:{agent.id} {agent.name}", detail=f"site={agent.site_id}")
    db.commit()
    db.refresh(agent)
    base = s.AgentOut.model_validate(agent)
    return s.AgentCreated(**base.model_dump(), api_key=api_key)


# --- Printers --------------------------------------------------------------- #
@router.get("/printers", response_model=list[s.PrinterOut])
def list_printers(
    client_id: Optional[int] = None,
    site_id: Optional[int] = None,
    discovery_state: Optional[m.DiscoveryState] = None,
    db: Session = Depends(get_db),
):
    stmt = select(m.Printer)
    if client_id is not None:
        stmt = stmt.where(m.Printer.client_id == client_id)
    if site_id is not None:
        stmt = stmt.where(m.Printer.site_id == site_id)
    if discovery_state is not None:
        stmt = stmt.where(m.Printer.discovery_state == discovery_state)
    return list(db.scalars(stmt.order_by(m.Printer.ip)))


@router.post("/printers", response_model=s.PrinterOut, status_code=201)
def create_printer(
    payload: s.PrinterIn, request: Request,
    db: Session = Depends(get_db), user: m.User = Depends(require_staff),
):
    _get_or_404(db, m.Client, payload.client_id)
    site = _get_or_404(db, m.Site, payload.site_id)
    # Same invariant the dashboard route enforces: site_id and client_id are two
    # separately-writable columns that the ingest path assumes agree, and ingest
    # keys on the SITE. A row whose site belongs to another client is handed to
    # that client's agent -- snmp_community included -- while billing still
    # charges the pages here.
    if site.client_id != payload.client_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"site {payload.site_id} does not belong to client {payload.client_id}",
        )
    printer = m.Printer(**payload.model_dump(), discovery_state=m.DiscoveryState.approved)
    db.add(printer)
    db.flush()
    record(db, request, user, "printer.create",
           target=f"printer:{printer.id} {printer.ip}",
           detail=f"client={printer.client_id}")
    db.commit()
    db.refresh(printer)
    return printer


@router.post("/printers/{printer_id}/approve", response_model=s.PrinterOut)
def approve_printer(
    printer_id: int, request: Request,
    db: Session = Depends(get_db), user: m.User = Depends(require_staff),
):
    printer = _get_or_404(db, m.Printer, printer_id)
    printer.discovery_state = m.DiscoveryState.approved
    record(db, request, user, "printer.approve", target=f"printer:{printer.id} {printer.ip}")
    db.commit()
    db.refresh(printer)
    return printer


@router.post("/printers/{printer_id}/ignore", response_model=s.PrinterOut)
def ignore_printer(
    printer_id: int, request: Request,
    db: Session = Depends(get_db), user: m.User = Depends(require_staff),
):
    printer = _get_or_404(db, m.Printer, printer_id)
    printer.discovery_state = m.DiscoveryState.ignored
    record(db, request, user, "printer.ignore", target=f"printer:{printer.id} {printer.ip}")
    db.commit()
    db.refresh(printer)
    return printer


# --- Maintenance ------------------------------------------------------------ #
@router.get("/printers/{printer_id}/maintenance", response_model=list[s.MaintenanceRecordOut])
def list_maintenance(printer_id: int, db: Session = Depends(get_db)):
    _get_or_404(db, m.Printer, printer_id)
    return list(
        db.scalars(
            select(m.MaintenanceRecord)
            .where(m.MaintenanceRecord.printer_id == printer_id)
            .order_by(m.MaintenanceRecord.performed_at.desc())
        )
    )


@router.post("/maintenance", response_model=s.MaintenanceRecordOut, status_code=201)
def add_maintenance(payload: s.MaintenanceRecordIn, db: Session = Depends(get_db)):
    _get_or_404(db, m.Printer, payload.printer_id)
    record = m.MaintenanceRecord(**payload.model_dump())
    db.add(record)
    # Logging service rolls any due schedule(s) for this printer forward, which
    # clears the maintenance-due alert: use the supplied next_due, else now+interval.
    now = datetime.now(timezone.utc)
    for sched in db.scalars(
        select(m.MaintenanceSchedule).where(m.MaintenanceSchedule.printer_id == payload.printer_id)
    ):
        if payload.next_due is not None:
            sched.next_due = payload.next_due
        elif sched.interval_days:
            sched.next_due = now + timedelta(days=sched.interval_days)
    db.commit()
    db.refresh(record)
    return record


# --- Commands (enqueue for an agent to pull) -------------------------------- #
@router.post("/commands", response_model=s.CommandOut, status_code=201)
def enqueue_command(
    payload: s.CommandIn,
    request: Request,
    db: Session = Depends(get_db),
    user: m.User = Depends(require_admin),
):
    """Queue a command for an agent to pull on its next heartbeat. ADMIN ONLY.

    Deliberately stricter than the rest of this (staff-wide) router: a command
    runs unattended inside a customer LAN as the agent's service account -- root
    under systemd, LocalSystem under NSSM -- and ``update_agent`` makes the agent
    pip-install, i.e. EXECUTE, whatever source it is handed. That is the same
    authority the dashboard reserves for admins on /manage/agents/update-outdated,
    so a tech-role session must not reach it through the JSON API either.

    The update source is never taken from the request: it is read from the
    admin-only ``agent.pip_source`` setting, exactly like the dashboard's
    /manage/agents/{id}/update. ``s.CommandIn`` refuses a caller-supplied
    pip_source (and any other unrecognised payload key) rather than stripping it.
    """
    agent = _get_or_404(db, m.Agent, payload.agent_id)
    body = dict(payload.payload or {})
    if payload.type == m.CommandType.update_agent:
        pip_source = str(load_settings(db).get("agent.pip_source") or "").strip()
        if not pip_source or "your-org" in pip_source:
            # 409, not 400: the request is fine, the server isn't configured yet.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Set Settings -> Agent install -> Pip source to your real repo "
                "before pushing updates; the placeholder won't install.",
            )
        body["pip_source"] = pip_source
    cmd = m.Command(agent_id=agent.id, type=payload.type, payload=body or None)
    db.add(cmd)
    # Enqueue is a security boundary (remote code execution on the agent host for
    # update_agent), so record who queued what alongside the dashboard's own
    # agent.update_queued rows. Nothing logged here is secret: the payload is at
    # most a printer IP/id plus the server-resolved repo URL.
    record(db, request, user, "command.enqueue",
           target=f"agent:{agent.id} {agent.name}",
           detail=f"type={payload.type.value} payload={body or {}}")
    db.commit()
    db.refresh(cmd)
    return cmd
