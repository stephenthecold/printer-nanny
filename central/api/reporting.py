"""Reporting endpoints: fleet status, low supplies, errors, maintenance due."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from central import queries
from central.db import get_db
from central.deps import require_user

router = APIRouter(
    prefix="/api/v1/reports", tags=["reporting"], dependencies=[Depends(require_user)]
)


@router.get("/fleet")
def fleet(client_id: Optional[int] = None, db: Session = Depends(get_db)):
    return queries.fleet_summary(db, client_id)


@router.get("/supplies/low")
def supplies_low(threshold: float = queries.DEFAULT_LOW_SUPPLY_PCT, db: Session = Depends(get_db)):
    supplies = queries.low_supplies(db, threshold)
    return [
        {
            "printer_id": sup.printer_id,
            "type": sup.type.value,
            "color": sup.color,
            "level_pct": sup.level_pct,
        }
        for sup in supplies
    ]


@router.get("/errors")
def errors(limit: int = 50, db: Session = Depends(get_db)):
    events = queries.recent_errors(db, limit)
    return [
        {
            "printer_id": e.printer_id,
            "ts": e.ts,
            "severity": e.severity.value,
            "code": e.code,
            "message": e.message,
        }
        for e in events
    ]


@router.get("/maintenance/due")
def maintenance_due(db: Session = Depends(get_db)):
    schedules = queries.maintenance_due(db)
    return [
        {
            "id": sch.id,
            "printer_id": sch.printer_id,
            "name": sch.name,
            "next_due": sch.next_due,
        }
        for sch in schedules
    ]
