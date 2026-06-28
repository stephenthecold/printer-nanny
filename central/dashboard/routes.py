"""HTMX/Jinja dashboard. Server-rendered, session-authenticated."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from central import models as m
from central import queries
from central.db import get_db
from central.runtime import app_branding
from central.security import hash_password, verify_password

router = APIRouter(tags=["dashboard"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _user(request: Request, db: Session):
    uid = request.session.get("user_id")
    user = db.get(m.User, uid) if uid else None
    # A deactivated (SCIM-deprovisioned) account is treated as logged out so a
    # live cookie stops working on its next request, not just at next login.
    return user if (user is not None and user.active) else None


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def _forbidden_client(user, client_id) -> bool:
    """A client_readonly user may only see their own client's data."""
    return user.role == m.UserRole.client_readonly and user.client_id != client_id


def _render(
    request: Request, template: str, db: Optional[Session] = None, **ctx
) -> HTMLResponse:
    from central import __version__ as _central_version

    ctx.setdefault("user", ctx.get("user"))
    # White-label branding injected into every render. Templates read ``app.name``,
    # ``app.logo_url``, ``app.primary_color``, ``app.support_email``, ``app.footer_text``.
    ctx.setdefault("app", app_branding(db) if db is not None else {})
    # Central server version surfaced in the footer of every page so operators
    # can verify a rollout landed without dropping to the shell.
    ctx.setdefault("central_version", _central_version)
    # Pending-discovery count drives the conditional Approvals nav entry:
    # the link only renders when there's actually something to approve.
    if db is not None and "nav_pending" not in ctx:
        ctx["nav_pending"] = db.scalar(
            select(func.count())
            .select_from(m.Printer)
            .where(m.Printer.discovery_state == m.DiscoveryState.pending)
        ) or 0
    return _templates.TemplateResponse(request, template, ctx)


# --- Auth ------------------------------------------------------------------- #
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, sso_error: str = "", db: Session = Depends(get_db)):
    from central.auth_oidc import oidc_config, oidc_enabled

    cfg = oidc_config(db)
    return _render(
        request, "login.html", db=db, error=None,
        sso_enabled=oidc_enabled(db),
        sso_label=cfg.get("button_label") or "Sign in with SSO",
        sso_error=sso_error,
    )


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    from central.audit import record, record_anonymous

    user = db.scalar(select(m.User).where(m.User.username == username))
    if user is None or not verify_password(password, user.password_hash):
        record_anonymous(db, request, username, "login.failed")
        db.commit()
        return _render(request, "login.html", db=db, error="Invalid credentials")
    # Deactivated (deprovisioned) accounts cannot log in even with a valid
    # password -- the same generic message so we don't leak that the account
    # exists-but-is-disabled. This is the SCIM off-boarding gate at the login
    # boundary (current_user enforces it for already-live sessions).
    if not user.active:
        record_anonymous(db, request, username, "login.deactivated")
        db.commit()
        return _render(request, "login.html", db=db, error="Invalid credentials")
    request.session["user_id"] = user.id
    record(db, request, user, "login")
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    from central.audit import record

    user = _user(request, db)
    if user is not None:
        record(db, request, user, "logout")
        db.commit()
    request.session.clear()
    return _login_redirect()


# --- Overview --------------------------------------------------------------- #
@router.get("/", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    # client_readonly users get the trimmed portal -- one fleet, one view, a
    # "Report a problem" button. The dense admin/tech overview would just be
    # noise for them.
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/portal", status_code=303)
    client_filter = user.client_id if user.role == m.UserRole.client_readonly else None
    rollup = queries.per_client_rollup(db)
    if user.role == m.UserRole.client_readonly:
        rollup = [r for r in rollup if r["client"].id == user.client_id]
    return _render(
        request,
        "overview.html",
        db=db,
        user=user,
        summary=queries.fleet_summary(db, client_filter),
        low=queries.low_supplies(db)[:10],
        errors=queries.recent_errors(db, 10),
        alerts=queries.open_alerts(db, 10),
        clients=list(db.scalars(select(m.Client).order_by(m.Client.name))),
        rollup=rollup,
        recent_activity=queries.recent_activity(db, 12),
        printer_label=_printer_label,
    )


# --- Customer portal (client_readonly only) --------------------------------- #
@router.get("/portal", response_class=HTMLResponse)
def customer_portal(request: Request, db: Session = Depends(get_db)):
    """Trimmed view for client_readonly users: their fleet status, low
    supplies with "your supplies last ~Nd" forecasts, open alerts, and a
    'Report a problem' form that opens a FreeScout ticket via the existing
    channel (no extra credential plumbing)."""
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly and user.client_id is None:
        # Misconfigured account -- send them to their account screen.
        return RedirectResponse("/account", status_code=303)
    client_id = user.client_id if user.role == m.UserRole.client_readonly \
        else int(request.query_params.get("client_id") or 0) or None
    if client_id is None and user.role != m.UserRole.client_readonly:
        # Admins/techs land on /portal for previewing; pick a client.
        first = db.scalar(select(m.Client).order_by(m.Client.name))
        if first is None:
            return RedirectResponse("/", status_code=303)
        client_id = first.id
    client = db.get(m.Client, client_id)
    if client is None:
        return RedirectResponse("/", status_code=303)
    printers = list(db.scalars(
        select(m.Printer)
        .where(m.Printer.client_id == client.id,
               m.Printer.discovery_state == m.DiscoveryState.approved)
        .order_by(m.Printer.site_id, m.Printer.ip)
    ))
    runway = queries.supply_runway(db, [p.id for p in printers])
    low = [
        s for s in queries.low_supplies(db)
        if s.printer and s.printer.client_id == client.id
    ][:10]
    alerts = [
        a for a in queries.open_alerts(db, 30)
        if a.printer_id and db.get(m.Printer, a.printer_id).client_id == client.id
    ][:10]
    from central.runtime import load_settings as _ls
    rt = _ls(db)
    freescout_on = bool(rt.get("freescout.enabled"))
    # ESG / sustainability panel — estimated print footprint for THIS client's
    # fleet, scoped by client_id so one tenant never sees another's totals.
    esg = queries.sustainability_rollup(db, client_id=client.id)
    return _render(
        request, "portal.html", db=db, user=user,
        client=client, printers=printers, runway=runway,
        low_supplies=low, open_alerts=alerts,
        freescout_enabled=freescout_on,
        esg=esg,
        printer_label=_printer_label,
        portal_flash=request.session.pop("portal_flash", None),
    )


@router.post("/portal/report")
def portal_report(
    request: Request,
    printer_id: str = Form(""),
    subject: str = Form(...),
    body: str = Form(...),
    db: Session = Depends(get_db),
):
    """Open a FreeScout ticket from the portal. Falls back to the alert
    email channel if FreeScout isn't configured -- whichever the operator
    wired up, the report lands somewhere actionable."""
    user = _user(request, db)
    if user is None or user.role != m.UserRole.client_readonly:
        return RedirectResponse("/", status_code=303)
    if not subject.strip() or not body.strip():
        request.session["portal_flash"] = "Subject and description are required."
        return RedirectResponse("/portal", status_code=303)
    client = db.get(m.Client, user.client_id) if user.client_id else None
    printer = None
    if printer_id.strip():
        try:
            p = db.get(m.Printer, int(printer_id))
            if p and p.client_id == (user.client_id or -1):
                printer = p
        except ValueError:
            printer = None
    from central.channels import Notification
    from central.channels.email import EmailChannel
    from central.channels.freescout import FreeScoutChannel
    from central.runtime import load_settings as _ls

    rt = _ls(db)
    note = Notification(
        title=f"[Portal report] {subject.strip()}",
        body=f"From: {user.username}\n\n{body.strip()}",
        severity="info",
        client_name=client.name if client else None,
        printer_label=f"{printer.display_name or printer.model or 'printer'} @ {printer.ip}"
            if printer else None,
    )
    sent_via = None
    if rt.get("freescout.enabled"):
        result = FreeScoutChannel("FreeScout", {}, rt).send(note)
        if result.ok:
            sent_via = "ticket"
    if sent_via is None and rt.get("email.default_recipients"):
        from central.db import SessionLocal
        channel = EmailChannel(
            "Reports", {"to": rt["email.default_recipients"]},
            runtime=rt, db_factory=SessionLocal,
        )
        result = channel.send(note)
        if result.ok:
            sent_via = "email"
    from central.audit import record
    record(db, request, user, "portal.report",
           target=f"client:{client.id} {client.name}" if client else "no-client",
           detail=f"via:{sent_via or 'none'}; subject={subject[:80]}")
    db.commit()
    request.session["portal_flash"] = (
        "Thanks -- your request was sent to support." if sent_via
        else "Could not deliver the report. Your operator has not configured a ticket channel yet."
    )
    return RedirectResponse("/portal", status_code=303)


# --- Client drill-down ------------------------------------------------------ #
@router.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail(client_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    client = db.get(m.Client, client_id)
    if client is None or _forbidden_client(user, client_id):
        return RedirectResponse("/", status_code=303)
    printers = list(
        db.scalars(
            select(m.Printer)
            .where(
                m.Printer.client_id == client_id,
                m.Printer.discovery_state == m.DiscoveryState.approved,
            )
            .order_by(m.Printer.site_id, m.Printer.ip)
        )
    )
    # Days-until-order forecast per printer (min days-to-empty across its
    # supplies) -- replaces the raw page count in the listing, which told the
    # operator nothing actionable.
    runway = queries.supply_runway(db, [p.id for p in printers])
    # Lowest supply per printer so the listing shows actual levels, not just
    # the forecast: (label, pct) of the most-depleted supply.
    lowest_supply: dict[int, tuple] = {}
    low_threshold = 20.0
    # Per-site rollup chips: printers / down / warnings / low supplies /
    # soonest order. Plus a client-level total across all sites.
    site_stats: dict[int, dict] = {}
    client_stats = {
        "printers": 0, "down": 0, "warnings": 0, "low_supplies": 0,
        "soonest_days": None,
    }
    for p in printers:
        stats = site_stats.setdefault(p.site_id, {
            "printers": 0, "down": 0, "warnings": 0, "low_supplies": 0,
            "soonest_days": None,
        })
        stats["printers"] += 1
        client_stats["printers"] += 1
        if p.status in (m.PrinterStatus.error, m.PrinterStatus.offline):
            stats["down"] += 1
            client_stats["down"] += 1
        elif p.status == m.PrinterStatus.warning:
            stats["warnings"] += 1
            client_stats["warnings"] += 1
        leveled = [s for s in p.supplies if s.level_pct is not None]
        if leveled:
            worst = min(leveled, key=lambda s: s.level_pct)
            lowest_supply[p.id] = (
                worst.description or worst.color or worst.type.value,
                worst.level_pct,
            )
        low_count = sum(1 for s in leveled if s.level_pct <= low_threshold)
        stats["low_supplies"] += low_count
        client_stats["low_supplies"] += low_count
        days = (runway.get(p.id) or {}).get("days")
        if days is not None:
            if stats["soonest_days"] is None or days < stats["soonest_days"]:
                stats["soonest_days"] = days
            if client_stats["soonest_days"] is None or days < client_stats["soonest_days"]:
                client_stats["soonest_days"] = days
    client_stats["open_alerts"] = db.scalar(
        select(func.count())
        .select_from(m.Alert)
        .join(m.Printer, m.Printer.id == m.Alert.printer_id)
        .where(m.Printer.client_id == client_id, m.Alert.state == m.AlertState.open)
    ) or 0
    return _render(
        request,
        "client.html",
        db=db,
        user=user,
        client=client,
        sites=client.sites,
        printers=printers,
        runway=runway,
        lowest_supply=lowest_supply,
        site_stats=site_stats,
        client_stats=client_stats,
        printer_label=_printer_label,
    )


# --- Printer detail --------------------------------------------------------- #
@router.get("/printers/{printer_id}", response_class=HTMLResponse)
def printer_detail(printer_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    printer = db.get(m.Printer, printer_id)
    if printer is None or _forbidden_client(user, printer.client_id):
        return RedirectResponse("/", status_code=303)
    history = queries.page_count_history(db, printer_id, 60)
    events = list(
        db.scalars(
            select(m.PrinterEvent)
            .where(m.PrinterEvent.printer_id == printer_id)
            .order_by(m.PrinterEvent.ts.desc())
            .limit(25)
        )
    )
    maint = list(
        db.scalars(
            select(m.MaintenanceRecord)
            .where(m.MaintenanceRecord.printer_id == printer_id)
            .order_by(m.MaintenanceRecord.performed_at.desc())
        )
    )
    return _render(
        request,
        "printer.html",
        db=db,
        user=user,
        printer=printer,
        supplies=printer.supplies,
        history=history,
        events=events,
        maintenance=maint,
        spark=_sparkline_points([r.page_count for r in history]),
        printer_label=_printer_label,
    )


# --- Device security posture ------------------------------------------------ #
@router.get("/security/posture", response_class=HTMLResponse)
def security_posture(request: Request, db: Session = Depends(get_db)):
    """Operator-facing "treat printers like endpoints" report: per-device
    security posture flags (insecure SNMP, firmware visibility) plus a fleet
    summary. Admin/tech only; client_readonly users are bounced to their
    portal."""
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/portal", status_code=303)
    posture = queries.security_posture_rollup(db)
    return _render(
        request,
        "security_posture.html",
        db=db,
        user=user,
        rows=posture["rows"],
        summary=posture["summary"],
        printer_label=_printer_label,
    )


# --- Pending discovery approvals -------------------------------------------- #
@router.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/", status_code=303)
    pending = list(
        db.scalars(
            select(m.Printer)
            .where(m.Printer.discovery_state == m.DiscoveryState.pending)
            .order_by(m.Printer.created_at.desc())
        )
    )
    agents_by_id = {a.id: a for a in db.scalars(select(m.Agent))}
    return _render(
        request, "approvals.html", db=db, user=user,
        pending=pending, agents_by_id=agents_by_id,
    )


@router.post("/approvals/{printer_id}/{action}", response_class=HTMLResponse)
def approval_action(
    printer_id: int, action: str, request: Request, db: Session = Depends(get_db)
):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/", status_code=303)
    printer = db.get(m.Printer, printer_id)
    if printer is not None and action in ("approve", "ignore"):
        from central.audit import record

        printer.discovery_state = (
            m.DiscoveryState.approved if action == "approve" else m.DiscoveryState.ignored
        )
        record(db, request, user, f"printer.{action}",
               target=f"printer:{printer.id} {printer.ip}")
        db.commit()
    # HTMX swaps out the row; return empty so the row disappears.
    return HTMLResponse("")


# --- Alerts inbox ----------------------------------------------------------- #
@router.get("/alerts", response_class=HTMLResponse)
def alerts_inbox(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/", status_code=303)
    rows = list(
        db.scalars(
            select(m.Alert)
            .where(m.Alert.state != m.AlertState.resolved)
            .order_by(m.Alert.created_at.desc())
        )
    )
    return _render(request, "alerts.html", db=db, user=user, alerts=rows)


@router.post("/alerts/{alert_id}/{action}", response_class=HTMLResponse)
def alert_action(alert_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    if user.role == m.UserRole.client_readonly:
        return RedirectResponse("/", status_code=303)
    alert = db.get(m.Alert, alert_id)
    if alert is not None:
        from central.audit import record

        if action == "ack":
            alert.state = m.AlertState.acknowledged
        elif action == "resolve":
            alert.state = m.AlertState.resolved
            alert.resolved_at = datetime.now(timezone.utc)
        record(db, request, user, f"alert.{action}",
               target=f"alert:{alert.id}", detail=alert.title or "")
        db.commit()
    return HTMLResponse("")


# --- My account: self-service profile + password change -------------------- #
@router.get("/account", response_class=HTMLResponse)
def account_view(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    return _render(
        request, "account.html", db=db, user=user,
        flash=request.session.pop("account_flash", None),
        error=request.session.pop("account_error", None),
    )


@router.post("/account/password")
def account_change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    # SSO-only users have no local password to change.
    if user.auth_provider != "local" or user.password_hash is None:
        request.session["account_error"] = (
            "Your account is managed by SSO — change your password with the identity provider."
        )
        return RedirectResponse("/account", status_code=303)
    if not verify_password(current_password, user.password_hash):
        request.session["account_error"] = "Current password is incorrect."
        return RedirectResponse("/account", status_code=303)
    if len(new_password) < 8:
        request.session["account_error"] = "New password must be at least 8 characters."
        return RedirectResponse("/account", status_code=303)
    if new_password != confirm_password:
        request.session["account_error"] = "New password and confirmation don't match."
        return RedirectResponse("/account", status_code=303)
    user.password_hash = hash_password(new_password)
    from central.audit import record

    record(db, request, user, "account.password_change")
    db.commit()
    request.session["account_flash"] = "Password changed."
    return RedirectResponse("/account", status_code=303)


# --- Discovery ---------------------------------------------------------------
# Folded into /manage/agents (each agent card carries its subnets' discovery
# status + rescan). The old URL redirects so bookmarks keep working; the
# rescan POST stays for anything that still targets it.
@router.get("/discovery")
def discovery_status(request: Request):
    return RedirectResponse("/manage/agents", status_code=303)


@router.post("/discovery/agents/{agent_id}/rescan")
def discovery_rescan(agent_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None or user.role == m.UserRole.client_readonly:
        return _login_redirect() if user is None else RedirectResponse("/", status_code=303)
    agent = db.get(m.Agent, agent_id)
    if agent is None:
        return RedirectResponse("/manage/agents", status_code=303)
    db.add(m.Command(agent_id=agent.id, type=m.CommandType.rescan, payload=None))
    db.commit()
    request.session["flash"] = (
        f"Rescan queued for {agent.name} — the agent will discover on its next heartbeat."
    )
    return RedirectResponse("/manage/agents", status_code=303)


# --- helpers ---------------------------------------------------------------- #
def _printer_label(printer: m.Printer) -> str:
    # Friendly name first: operators name printers so alerts and dashboards
    # say "Front Desk" instead of a model number + IP.
    name = printer.display_name or printer.model or printer.hostname or "printer"
    return f"{name} @ {printer.ip}"


def _sparkline_points(values: list, width: int = 280, height: int = 48) -> str:
    """Build an SVG polyline points string from a numeric series."""
    nums = [v for v in values if v is not None]
    if len(nums) < 2:
        return ""
    lo, hi = min(nums), max(nums)
    span = (hi - lo) or 1
    step = width / (len(nums) - 1)
    pts = []
    for i, v in enumerate(nums):
        x = i * step
        y = height - ((v - lo) / span) * height
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)
