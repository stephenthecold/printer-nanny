"""HTMX/Jinja dashboard. Server-rendered, session-authenticated."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import queries
from central.db import get_db
from central.security import verify_password

router = APIRouter(tags=["dashboard"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _user(request: Request, db: Session):
    uid = request.session.get("user_id")
    return db.get(m.User, uid) if uid else None


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def _render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", ctx.get("user"))
    return _templates.TemplateResponse(request, template, ctx)


# --- Auth ------------------------------------------------------------------- #
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, sso_error: str = "", db: Session = Depends(get_db)):
    from central.auth_oidc import oidc_config, oidc_enabled

    cfg = oidc_config(db)
    return _render(
        request, "login.html", error=None,
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
    user = db.scalar(select(m.User).where(m.User.username == username))
    if user is None or not verify_password(password, user.password_hash):
        return _render(request, "login.html", error="Invalid credentials")
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return _login_redirect()


# --- Overview --------------------------------------------------------------- #
@router.get("/", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    client_filter = user.client_id if user.role == m.UserRole.client_readonly else None
    return _render(
        request,
        "overview.html",
        user=user,
        summary=queries.fleet_summary(db, client_filter),
        low=queries.low_supplies(db)[:10],
        errors=queries.recent_errors(db, 10),
        alerts=queries.open_alerts(db, 10),
        clients=list(db.scalars(select(m.Client).order_by(m.Client.name))),
        printer_label=_printer_label,
    )


# --- Client drill-down ------------------------------------------------------ #
@router.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail(client_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    client = db.get(m.Client, client_id)
    if client is None:
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
    return _render(
        request,
        "client.html",
        user=user,
        client=client,
        sites=client.sites,
        printers=printers,
        printer_label=_printer_label,
    )


# --- Printer detail --------------------------------------------------------- #
@router.get("/printers/{printer_id}", response_class=HTMLResponse)
def printer_detail(printer_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    printer = db.get(m.Printer, printer_id)
    if printer is None:
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
        user=user,
        printer=printer,
        supplies=printer.supplies,
        history=history,
        events=events,
        maintenance=maint,
        spark=_sparkline_points([r.page_count for r in history]),
        printer_label=_printer_label,
    )


# --- Pending discovery approvals -------------------------------------------- #
@router.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    pending = list(
        db.scalars(
            select(m.Printer)
            .where(m.Printer.discovery_state == m.DiscoveryState.pending)
            .order_by(m.Printer.created_at.desc())
        )
    )
    return _render(request, "approvals.html", user=user, pending=pending)


@router.post("/approvals/{printer_id}/{action}", response_class=HTMLResponse)
def approval_action(
    printer_id: int, action: str, request: Request, db: Session = Depends(get_db)
):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    printer = db.get(m.Printer, printer_id)
    if printer is not None and action in ("approve", "ignore"):
        printer.discovery_state = (
            m.DiscoveryState.approved if action == "approve" else m.DiscoveryState.ignored
        )
        db.commit()
    # HTMX swaps out the row; return empty so the row disappears.
    return HTMLResponse("")


# --- Alerts inbox ----------------------------------------------------------- #
@router.get("/alerts", response_class=HTMLResponse)
def alerts_inbox(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    rows = list(
        db.scalars(
            select(m.Alert)
            .where(m.Alert.state != m.AlertState.resolved)
            .order_by(m.Alert.created_at.desc())
        )
    )
    return _render(request, "alerts.html", user=user, alerts=rows)


@router.post("/alerts/{alert_id}/{action}", response_class=HTMLResponse)
def alert_action(alert_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return _login_redirect()
    alert = db.get(m.Alert, alert_id)
    if alert is not None:
        if action == "ack":
            alert.state = m.AlertState.acknowledged
        elif action == "resolve":
            alert.state = m.AlertState.resolved
            alert.resolved_at = datetime.now(timezone.utc)
        db.commit()
    return HTMLResponse("")


# --- helpers ---------------------------------------------------------------- #
def _printer_label(printer: m.Printer) -> str:
    name = printer.model or printer.hostname or "printer"
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
