"""CSV exports: inventory, supplies, open alerts.

Operator-facing -- a tech opens /manage, clicks 'Download CSV', drops the file
into their billing spreadsheet or PSA bulk-import. Three views cover the
common asks:

  GET /api/v1/reports/export/inventory.csv   one row per approved printer
  GET /api/v1/reports/export/supplies.csv    one row per supply per printer
  GET /api/v1/reports/export/alerts.csv      one row per open alert

Tenant scoping:
* admins / techs see every approved printer by default; ?client_id=N narrows.
* client_readonly users only ever see their pinned client's printers
  (their session is forced through the same filter).

CSV is streamed (a writer over a StringIO that we yield chunks from) so
a multi-thousand-row fleet doesn't blow up memory. Content-Disposition
forces a download with a sensible default filename.

Every cell goes through ``central.csv_safe.safe_writer``: half of these columns
are SNMP strings off a device on a client LAN, and RFC 4180 quoting does nothing
about a cell that starts with ``=``. See that module for the rule.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import queries
from central.csv_safe import safe_writer
from central.db import get_db
from central.deps import require_user

router = APIRouter(
    prefix="/api/v1/reports/export",
    tags=["reporting"],
    dependencies=[Depends(require_user)],
)


def _resolve_client_filter(user: m.User, client_id: Optional[int]) -> Optional[int]:
    """Pin client_readonly users to their own client; admins/techs respect ?client_id.

    Raises 403 if a client_readonly user tries to request another client's data.
    """
    if user.role == m.UserRole.client_readonly:
        if user.client_id is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "client_readonly user has no client_id assigned",
            )
        if client_id is not None and client_id != user.client_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Cross-client access denied",
            )
        return user.client_id
    return client_id


def _csv_response(filename: str, header: list[str], rows: Iterable[list]) -> StreamingResponse:
    """Stream a CSV file.

    Header + rows go through ``safe_writer``, which is ``csv.writer`` plus
    formula-injection neutralisation. The quoting/escaping stays RFC
    4180-correct on values containing commas, quotes or newlines -- that was
    never in question, and was never the whole job: a cell beginning ``=``,
    ``+``, ``-`` or ``@`` is quoted correctly *and* executed by Excel. Doing it
    in the writer means a column added to any of the three views below is
    covered without its author having to know this exists.
    """

    def iter_rows():
        buf = io.StringIO()
        writer = safe_writer(buf)
        writer.writerow(header)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        for row in rows:
            writer.writerow(row)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(
        iter_rows(), media_type="text/csv; charset=utf-8", headers=headers,
    )


def _datestamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d")


@router.get("/inventory.csv")
def export_inventory(
    client_id: Optional[int] = None,
    user: m.User = Depends(require_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """One row per approved printer: identity + status + last_seen + page_count."""
    cid = _resolve_client_filter(user, client_id)
    stmt = (
        select(m.Printer)
        .where(m.Printer.discovery_state == m.DiscoveryState.approved)
        .order_by(m.Printer.client_id, m.Printer.site_id, m.Printer.ip)
    )
    if cid is not None:
        stmt = stmt.where(m.Printer.client_id == cid)

    header = [
        "client", "site", "ip", "hostname", "brand", "model", "serial",
        "mac", "location", "asset_tag", "status", "page_count",
        "last_seen_utc", "created_at_utc", "tags", "display_name",
    ]

    def rows():
        for p in db.scalars(stmt):
            yield [
                p.client.name if p.client else "",
                p.site.name if p.site else "",
                p.ip or "",
                p.hostname or "",
                p.brand or "",
                p.model or "",
                p.serial or "",
                p.mac or "",
                p.location or "",
                p.asset_tag or "",
                p.status.value if p.status else "",
                p.page_count if p.page_count is not None else "",
                p.last_seen.isoformat() if p.last_seen else "",
                p.created_at.isoformat() if p.created_at else "",
                ",".join(p.tags or []),
                p.display_name or "",
            ]

    return _csv_response(f"printer-nanny-inventory-{_datestamp()}.csv", header, rows())


@router.get("/supplies.csv")
def export_supplies(
    client_id: Optional[int] = None,
    user: m.User = Depends(require_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """One row per supply per approved printer. Includes coarse status_note
    (so 'low' / 'some remaining' show up even when level_pct is null)."""
    cid = _resolve_client_filter(user, client_id)
    stmt = (
        select(m.Supply, m.Printer)
        .join(m.Printer, m.Supply.printer_id == m.Printer.id)
        .where(m.Printer.discovery_state == m.DiscoveryState.approved)
        .order_by(m.Printer.client_id, m.Printer.site_id, m.Printer.ip,
                  m.Supply.type, m.Supply.color)
    )
    if cid is not None:
        stmt = stmt.where(m.Printer.client_id == cid)

    header = [
        "client", "site", "ip", "model", "serial",
        "supply_type", "color", "description",
        "level_pct", "status_note", "current_raw", "max_capacity_raw",
        "supply_updated_utc",
    ]

    def rows():
        for supply, printer in db.execute(stmt):
            yield [
                printer.client.name if printer.client else "",
                printer.site.name if printer.site else "",
                printer.ip or "",
                printer.model or "",
                printer.serial or "",
                supply.type.value if supply.type else "",
                supply.color or "",
                supply.description or "",
                supply.level_pct if supply.level_pct is not None else "",
                supply.status_note or "",
                supply.current if supply.current is not None else "",
                supply.max_capacity if supply.max_capacity is not None else "",
                supply.updated_at.isoformat() if supply.updated_at else "",
            ]

    return _csv_response(f"printer-nanny-supplies-{_datestamp()}.csv", header, rows())


@router.get("/alerts.csv")
def export_alerts(
    client_id: Optional[int] = None,
    include_resolved: bool = False,
    user: m.User = Depends(require_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Alerts with printer context. Open-only by default;
    ?include_resolved=true exports the full history."""
    cid = _resolve_client_filter(user, client_id)
    stmt = (
        select(m.Alert, m.Printer)
        .join(m.Printer, m.Alert.printer_id == m.Printer.id)
        .order_by(m.Alert.created_at.desc())
    )
    if not include_resolved:
        stmt = stmt.where(m.Alert.state == m.AlertState.open)
    if cid is not None:
        stmt = stmt.where(m.Printer.client_id == cid)

    header = [
        "client", "site", "ip", "model", "serial",
        "alert_type", "severity", "state",
        "title", "detail", "created_at_utc", "resolved_at_utc",
    ]

    def rows():
        for alert, printer in db.execute(stmt):
            yield [
                printer.client.name if printer.client else "",
                printer.site.name if printer.site else "",
                printer.ip or "",
                printer.model or "",
                printer.serial or "",
                alert.type.value if alert.type else "",
                alert.severity.value if alert.severity else "",
                alert.state.value if alert.state else "",
                alert.title or "",
                alert.detail or "",
                alert.created_at.isoformat() if alert.created_at else "",
                alert.resolved_at.isoformat() if alert.resolved_at else "",
            ]

    return _csv_response(f"printer-nanny-alerts-{_datestamp()}.csv", header, rows())


@router.get("/posture.csv")
def export_posture(
    client_id: Optional[int] = None,
    user: m.User = Depends(require_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Device security posture, one row per approved printer.

    STAFF ONLY -- deliberately not merely tenant-scoped. Every other export
    here pins a client_readonly user to their own client and serves them the
    file; this one refuses them outright, because it must match the
    /security/posture page it is the download button for. An export that hands
    a customer a report the UI will not show them is a tenancy hole with a
    filename, and it is reached by guessing a URL rather than by clicking
    anything. See the route in central/dashboard/routes.py for why the whole
    surface is staff-only.

    The community string itself is never a column. Whether it is a factory
    default is the finding; the value is the credential, and this file gets
    mailed around.
    """
    if user.role == m.UserRole.client_readonly:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The security-posture report is staff-only",
        )
    posture = queries.security_posture_rollup(db, client_id=client_id)

    header = [
        "client", "site", "ip", "hostname", "display_name", "brand", "model",
        "serial", "firmware", "firmware_known",
        "snmp_version", "snmp_grade", "snmp_source", "snmp_default_community",
        "flags",
    ]

    def rows():
        for row in posture["rows"]:
            printer = row["printer"]
            yield [
                row["client"].name if row["client"] else "",
                row["site"].name if row["site"] else "",
                printer.ip or "",
                printer.hostname or "",
                printer.display_name or "",
                printer.brand or "",
                printer.model or "",
                printer.serial or "",
                # Honest blank, never a placeholder that reads as a version.
                row["firmware"] or "",
                "yes" if row["firmware_known"] else "no",
                row["snmp_version"],
                row["snmp_grade"],
                row["snmp_source"] or "",
                "yes" if row["snmp_default_community"] else "no",
                " ".join(row["flags"]),
            ]

    return _csv_response(f"printer-nanny-posture-{_datestamp()}.csv", header, rows())
