"""Management CRUD: clients, sites, subnets, agents, printers, maintenance, commands."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import schemas as s
from central.db import get_db
from central.deps import require_staff
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
def create_client(payload: s.ClientIn, db: Session = Depends(get_db)):
    client = m.Client(name=payload.name, notes=payload.notes)
    db.add(client)
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
def create_site(payload: s.SiteIn, db: Session = Depends(get_db)):
    _get_or_404(db, m.Client, payload.client_id)
    site = m.Site(**payload.model_dump())
    db.add(site)
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
def create_subnet(payload: s.SubnetIn, db: Session = Depends(get_db)):
    _get_or_404(db, m.Site, payload.site_id)
    subnet = m.Subnet(**payload.model_dump())
    db.add(subnet)
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
def create_agent(payload: s.AgentIn, db: Session = Depends(get_db)):
    """Create an agent and return its API key ONCE (only the hash is stored)."""
    _get_or_404(db, m.Site, payload.site_id)
    api_key = generate_api_key()
    agent = m.Agent(
        site_id=payload.site_id, name=payload.name, api_key_hash=hash_api_key(api_key)
    )
    db.add(agent)
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
def create_printer(payload: s.PrinterIn, db: Session = Depends(get_db)):
    _get_or_404(db, m.Client, payload.client_id)
    _get_or_404(db, m.Site, payload.site_id)
    printer = m.Printer(**payload.model_dump(), discovery_state=m.DiscoveryState.approved)
    db.add(printer)
    db.commit()
    db.refresh(printer)
    return printer


@router.post("/printers/{printer_id}/approve", response_model=s.PrinterOut)
def approve_printer(printer_id: int, db: Session = Depends(get_db)):
    printer = _get_or_404(db, m.Printer, printer_id)
    printer.discovery_state = m.DiscoveryState.approved
    db.commit()
    db.refresh(printer)
    return printer


@router.post("/printers/{printer_id}/ignore", response_model=s.PrinterOut)
def ignore_printer(printer_id: int, db: Session = Depends(get_db)):
    printer = _get_or_404(db, m.Printer, printer_id)
    printer.discovery_state = m.DiscoveryState.ignored
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
def enqueue_command(payload: s.CommandIn, db: Session = Depends(get_db)):
    _get_or_404(db, m.Agent, payload.agent_id)
    cmd = m.Command(agent_id=payload.agent_id, type=payload.type, payload=payload.payload)
    db.add(cmd)
    db.commit()
    db.refresh(cmd)
    return cmd
