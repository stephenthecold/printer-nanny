"""Scheduled reports: weekly fleet summary email + monthly billing CSV.

Driven by the worker, which calls ``run_scheduled_reports`` every cycle. The
function is cheap when nothing is due: it compares "today" (UTC) against the
configured day + send-hour and a last-sent marker stored in ``app_settings``
(plain rows, not Specs -- they're machine state, not operator config), so a
worker restart can't double-send and a downed worker catches up on the next
cycle after it comes back.

Delivery goes through the email channel only (a billing CSV in Slack helps
nobody); recipients come from ``reports.recipients`` falling back to the
alert recipients. The monthly CSV rides as a real attachment.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from central import models as m
from central import queries
from central.channels import Notification
from central.channels.email import EmailChannel
from central.runtime import load_settings

log = logging.getLogger("central.reports")

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Report types recorded in ReportRun (and the legacy app_settings markers below).
REPORT_WEEKLY = "weekly"
REPORT_MONTHLY = "monthly"

# Legacy last-sent markers in app_settings. Superseded by the transactional
# ReportRun table (the UNIQUE (report_type, period_key) is what makes the send
# race-safe). Kept only so _has_run can read a pre-upgrade marker once, after
# which every claim writes a ReportRun row. load_settings ignores non-Spec keys,
# so these never leak into the Settings UI.
MARKER_WEEKLY = "reports.weekly_last_sent"
MARKER_MONTHLY = "reports.monthly_last_sent"


def _get_marker(db: Session, key: str) -> Optional[str]:
    row = db.get(m.AppSetting, key)
    return row.value if row is not None else None


def _set_marker(db: Session, key: str, value: str) -> None:
    row = db.get(m.AppSetting, key)
    if row is None:
        db.add(m.AppSetting(key=key, value=value))
    else:
        row.value = value


def _has_run(db: Session, report_type: str, period_key: str) -> bool:
    """True if this (report_type, period) was already sent.

    Reads the transactional ReportRun marker. Also honors the legacy
    app_settings marker for the SAME period so an in-flight upgrade (a marker
    written by the pre-ReportRun code, before this period rolls over) still
    suppresses a duplicate send on the very first post-upgrade cycle.
    """
    exists = db.scalar(
        select(m.ReportRun.id).where(
            m.ReportRun.report_type == report_type,
            m.ReportRun.period_key == period_key,
        )
    )
    if exists is not None:
        return True
    legacy_key = MARKER_WEEKLY if report_type == REPORT_WEEKLY else MARKER_MONTHLY
    return _get_marker(db, legacy_key) == period_key


def _claim_report_run(db: Session, report_type: str, period_key: str) -> bool:
    """Atomically claim "(report_type, period_key) is being sent".

    INSERTs the ReportRun marker and commits inside a single transaction. Returns
    True if THIS process won the claim (it should now send), or False if another
    cycle already inserted the same (report_type, period_key) -- the UNIQUE
    constraint raises IntegrityError, which we swallow into "already claimed, skip
    the send". This makes once-per-period race-safe even without the leader lock.

    On a lost race the session is rolled back; the caller must not send.
    """
    db.add(m.ReportRun(report_type=report_type, period_key=period_key))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        log.info(
            "report %s/%s already claimed by another cycle; skipping send",
            report_type, period_key,
        )
        return False


def _release_report_run(db: Session, report_type: str, period_key: str) -> None:
    """Undo a claim whose delivery failed, so the next cycle retries the send.

    Preserves the existing contract that a failed delivery leaves nothing
    recorded -- a downed channel must not silently drop a period's report.
    """
    row = db.scalar(
        select(m.ReportRun).where(
            m.ReportRun.report_type == report_type,
            m.ReportRun.period_key == period_key,
        )
    )
    if row is not None:
        db.delete(row)
        db.commit()


# --------------------------------------------------------------------------- #
# Content builders (pure -- unit-testable without channels)
# --------------------------------------------------------------------------- #
def build_weekly_summary(db: Session) -> Tuple[str, str]:
    """(subject, plain-text body) for the weekly fleet summary."""
    summary = queries.fleet_summary(db)
    rollup = queries.per_client_rollup(db)
    low = queries.low_supplies(db)[:15]

    lines = [
        "Weekly fleet summary",
        "",
        f"Printers monitored : {summary['total_printers']}",
        f"  OK               : {summary['by_status'].get('ok', 0)}",
        f"  Warning          : {summary['by_status'].get('warning', 0)}",
        f"  Error / offline  : "
        f"{summary['by_status'].get('error', 0) + summary['by_status'].get('offline', 0)}",
        f"Pending discovery  : {summary['pending_discovery']}",
        f"Open alerts        : {summary['open_alerts']}",
        f"Agents offline     : {summary['agents_offline']}",
        "",
        "Per client:",
    ]
    for row in rollup:
        lines.append(
            f"  {row['client'].name:<24} printers={row['printer_count']:<4} "
            f"down={row['offline_count']:<3} low-supplies={row['low_supplies']:<3} "
            f"open-alerts={row['open_alerts']}"
        )
    if not rollup:
        lines.append("  (no clients yet)")

    lines += ["", "Low supplies (<= threshold):"]
    if low:
        for sup in low:
            printer = db.get(m.Printer, sup.printer_id)
            where = (
                f"{printer.display_name or printer.model or printer.hostname or 'printer'}"
                f" @ {printer.ip}"
            ) if printer else f"printer:{sup.printer_id}"
            label = sup.description or sup.color or sup.type.value
            lines.append(f"  {label:<28} {sup.level_pct:>5.0f}%  {where}")
    else:
        lines.append("  none -- all supplies healthy")

    # ESG / sustainability — estimated fleet-wide print footprint, derived from
    # the same page-count history. Cheap to compute, increasingly an RFP ask.
    esg = queries.sustainability_rollup(db)
    lines += [
        "",
        "Sustainability footprint (estimated):",
        f"  Sheets printed   : {esg['sheets']:,.0f}",
        f"  Paper            : {esg['paper_kg']:,.1f} kg",
        f"  CO2e             : {esg['co2_kg']:,.1f} kg",
        f"  Energy           : {esg['kwh']:,.1f} kWh",
        f"  Tree-equivalents : {esg['trees']:,.2f}",
        "  (estimates from page-count history x standard conversion factors)",
    ]

    subject = (
        f"Weekly fleet summary: {summary['total_printers']} printers, "
        f"{summary['open_alerts']} open alert(s)"
    )
    return subject, "\n".join(lines)


def build_monthly_billing_csv(db: Session) -> bytes:
    """Inventory + page counts for billing import. One row per approved printer."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "client", "site", "ip", "hostname", "brand", "model", "serial",
        "asset_tag", "page_count", "last_seen_utc",
    ])
    stmt = (
        select(m.Printer)
        .where(m.Printer.discovery_state == m.DiscoveryState.approved)
        .order_by(m.Printer.client_id, m.Printer.site_id, m.Printer.ip)
    )
    for p in db.scalars(stmt):
        writer.writerow([
            p.client.name if p.client else "",
            p.site.name if p.site else "",
            p.ip or "",
            p.hostname or "",
            p.brand or "",
            p.model or "",
            p.serial or "",
            p.asset_tag or "",
            p.page_count if p.page_count is not None else "",
            p.last_seen.isoformat() if p.last_seen else "",
        ])
    return buf.getvalue().encode("utf-8")


# --------------------------------------------------------------------------- #
# Delivery + due-check
# --------------------------------------------------------------------------- #
def _deliver(db: Session, rt: dict, subject: str, body: str,
             attachments: Optional[list] = None) -> Tuple[bool, str]:
    """Send a report through the email channel. Separated as a seam so tests
    (and a future 'send now' button) can stub delivery."""
    recipients = (rt.get("reports.recipients") or "").strip() \
        or (rt.get("email.default_recipients") or "").strip()
    if not recipients:
        return False, "no recipients configured (reports.recipients / email.default_recipients)"
    from central.db import SessionLocal

    channel = EmailChannel(
        "Reports", config={"to": recipients}, runtime=rt, db_factory=SessionLocal,
    )
    note = Notification(
        title=subject, body=body, severity="info", attachments=attachments,
    )
    result = channel.send(note)
    return result.ok, result.detail


def run_scheduled_reports(db: Session, now: Optional[datetime] = None) -> dict:
    """Worker job: send the weekly / monthly reports when due. Cheap no-op when
    nothing is due.

    Once-per-period is race-safe via the ReportRun table: each due report
    transactionally CLAIMs its period (an INSERT guarded by the UNIQUE
    (report_type, period_key)) *before* sending. Two cycles racing the same
    period -> exactly one wins the insert and sends; the loser's IntegrityError
    is swallowed and it skips. A claim whose delivery then fails is released so
    the next cycle retries (no period silently dropped). The period key is the
    UTC date for weekly and the YYYY-MM month for monthly.
    """
    now = now or datetime.now(timezone.utc)
    rt = load_settings(db)
    today = now.date().isoformat()
    send_hour = int(rt.get("reports.send_hour") or 7)
    out = {"weekly_report": "skipped", "monthly_report": "skipped"}

    # --- Weekly --- (one send per UTC date it fires on)
    if rt.get("reports.weekly_enabled") and now.hour >= send_hour:
        want_day = str(rt.get("reports.weekly_day") or "mon").lower()[:3]
        if want_day in _WEEKDAYS and _WEEKDAYS[now.weekday()] == want_day \
                and not _has_run(db, REPORT_WEEKLY, today):
            if not _claim_report_run(db, REPORT_WEEKLY, today):
                out["weekly_report"] = "skipped"  # another cycle claimed it
            else:
                subject, body = build_weekly_summary(db)
                ok, detail = _deliver(db, rt, subject, body)
                if ok:
                    out["weekly_report"] = "sent"
                else:
                    # Release the claim so the next cycle retries (no silent drop).
                    _release_report_run(db, REPORT_WEEKLY, today)
                    log.warning("weekly report delivery failed: %s", detail)
                    out["weekly_report"] = f"failed: {detail}"

    # --- Monthly --- (one send per YYYY-MM)
    if rt.get("reports.monthly_enabled") and now.hour >= send_hour:
        want_dom = int(rt.get("reports.monthly_day") or 1)
        stamp = now.strftime("%Y-%m")
        if now.day == want_dom and not _has_run(db, REPORT_MONTHLY, stamp):
            if not _claim_report_run(db, REPORT_MONTHLY, stamp):
                out["monthly_report"] = "skipped"  # another cycle claimed it
            else:
                csv_bytes = build_monthly_billing_csv(db)
                subject = f"Monthly billing report ({stamp})"
                body = (
                    f"Attached: inventory and page counts for {stamp}, one row per "
                    "monitored printer. Import into your billing system or open in Excel."
                )
                ok, detail = _deliver(
                    db, rt, subject, body,
                    attachments=[(f"printer-nanny-billing-{stamp}.csv", "text/csv", csv_bytes)],
                )
                if ok:
                    out["monthly_report"] = "sent"
                else:
                    _release_report_run(db, REPORT_MONTHLY, stamp)
                    log.warning("monthly report delivery failed: %s", detail)
                    out["monthly_report"] = f"failed: {detail}"

    return out
