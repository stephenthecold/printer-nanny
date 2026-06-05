"""Agent → central ingest endpoints (push readings/discovery, pull commands)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import schemas as s
from central import services
from central.db import get_db
from central.deps import authenticated_agent, touch_heartbeat

router = APIRouter(prefix="/api/v1/agents/{agent_id}", tags=["ingest"])


@router.post("/heartbeat", response_model=s.AgentOut)
def heartbeat(
    payload: s.HeartbeatIn,
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    touch_heartbeat(agent, payload.version)
    db.commit()
    db.refresh(agent)
    return agent


@router.post("/readings")
def post_readings(
    batch: s.ReadingsBatchIn,
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    touch_heartbeat(agent)
    applied, skipped = 0, []
    for reading in batch.readings:
        printer = services.apply_reading(db, agent.site_id, reading)
        if printer is None:
            skipped.append(reading.ip)
        else:
            applied += 1
    db.commit()
    return {"applied": applied, "skipped_unknown": skipped}


@router.post("/discovered")
def post_discovered(
    batch: s.DiscoveredBatchIn,
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    touch_heartbeat(agent)
    created, known = 0, 0
    for device in batch.devices:
        _, was_created = services.record_discovered(db, agent, device)
        created += int(was_created)
        known += int(not was_created)
    db.commit()
    return {"new_pending": created, "already_known": known}


@router.get("/targets", response_model=list[s.PollTargetOut])
def get_targets(
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    """Approved printers at the agent's site, with SNMP params — the poll list."""
    touch_heartbeat(agent)
    targets = list(
        db.scalars(
            select(m.Printer).where(
                m.Printer.site_id == agent.site_id,
                m.Printer.discovery_state == m.DiscoveryState.approved,
            )
        )
    )
    db.commit()
    return targets


@router.get("/commands", response_model=list[s.CommandOut])
def get_commands(
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    """Return pending commands and mark them sent (at-least-once delivery)."""
    touch_heartbeat(agent)
    commands = list(
        db.scalars(
            select(m.Command).where(
                m.Command.agent_id == agent.id,
                m.Command.status == m.CommandStatus.pending,
            )
        )
    )
    now = datetime.now(timezone.utc)
    for cmd in commands:
        cmd.status = m.CommandStatus.sent
        cmd.sent_at = now
    db.commit()
    return commands
