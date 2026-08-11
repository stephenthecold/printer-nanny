"""Staff-reported printer issues and their immediate durable notification."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central.channels.base import Notification
from central.channels.delivery import channel_badges, record_dispatch
from central.channels.freescout import FreeScoutChannel
from central.channels.registry import routable_channels, route_channels
from central.events.emit import emit_alert_opened
from central.runtime import load_settings

IMPACT_SEVERITY = {
    "stopped": m.EventSeverity.critical,
    "degraded": m.EventSeverity.warning,
    "information": m.EventSeverity.info,
}


def one_line(value: str, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def issue_key(printer_id: int, occurred_at: datetime) -> str:
    minute = occurred_at.astimezone(timezone.utc).replace(second=0, microsecond=0)
    # One printer occurrence is one support incident even when two observers
    # describe its symptoms differently. The occurrence minute is entered in
    # the client's local time and normalized by the route before it reaches us.
    material = f"{printer_id}|{minute.isoformat()}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"manual:printer:{printer_id}:{digest}"


def _external_ref(results, channels) -> Optional[str]:
    names = {channel.name for channel in channels if isinstance(channel, FreeScoutChannel)}
    for name, result in results:
        if name in names and result.ok and result.external_ref:
            return str(result.external_ref)[:120]
    return None


def create_issue(
    db: Session,
    *,
    printer: m.Printer,
    impact: str,
    title: str,
    detail: str,
    occurred_at: datetime,
    now: Optional[datetime] = None,
) -> tuple[m.Alert, bool]:
    """Create and notify once; the same printer/occurrence minute reuses one issue."""
    severity = IMPACT_SEVERITY[impact]
    now = now or datetime.now(timezone.utc)
    key = issue_key(printer.id, occurred_at)
    existing = db.scalar(
        select(m.Alert).where(
            m.Alert.dedupe_key == key,
            m.Alert.state.in_([m.AlertState.open, m.AlertState.acknowledged]),
        )
    )
    if existing is not None:
        return existing, False

    issue_title = one_line(title, 300)
    issue_detail = str(detail or "").strip()[:2000]
    alert = m.Alert(
        printer_id=printer.id,
        type=m.AlertConditionType.manual_issue,
        severity=severity,
        state=m.AlertState.open,
        title=issue_title,
        detail=issue_detail,
        occurred_at=occurred_at,
        dedupe_key=key,
        created_at=now,
    )
    db.add(alert)
    db.flush()

    client = db.get(m.Client, printer.client_id)
    site = db.get(m.Site, printer.site_id)
    site_label = site.name if site else None
    if site_label and printer.location:
        site_label = f"{site_label} · {one_line(printer.location, 160)}"
    happened = occurred_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = f"Issue: {issue_detail or issue_title}\nOccurred: {happened}"
    note = Notification(
        title=issue_title,
        body=body,
        severity=severity.value,
        client_name=client.name if client else None,
        site_name=site_label,
        printer_label=(
            f"{one_line(printer.model or 'Unknown model', 160)} @ {printer.ip}"
        ),
        alert_id=alert.id,
    )
    runtime = load_settings(db)
    candidates = routable_channels(db, runtime)
    channels = route_channels(
        candidates, rule=None, printer=printer, severity=severity.value
    )
    results = record_dispatch(
        db, alert.id, note, channels, runtime=runtime, now=now
    )
    alert.notified_channels = channel_badges(results)
    alert.external_ref = _external_ref(results, channels)
    alert.last_notified_at = now
    emit_alert_opened(db, alert)
    return alert, True
