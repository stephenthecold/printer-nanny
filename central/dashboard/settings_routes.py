"""Settings page — edit DB-backed runtime config (admin only)."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from central import models as m
from central import runtime
from central.db import get_db

router = APIRouter(tags=["settings"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _admin(request: Request, db: Session) -> Optional[m.User]:
    uid = request.session.get("user_id")
    user = db.get(m.User, uid) if uid else None
    if user is None or user.role != m.UserRole.admin:
        return None
    return user


def _sections(values: dict):
    """Group specs by section for rendering, with masked secrets."""
    masked = runtime.masked_for_form(values)
    grouped: "OrderedDict[str, list]" = OrderedDict()
    for spec in runtime.SPECS:
        grouped.setdefault(spec.section, []).append({"spec": spec, "value": masked.get(spec.key)})
    return grouped


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = _admin(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    values = runtime.load_settings(db)
    return _templates.TemplateResponse(
        request, "settings.html",
        {"user": user, "sections": _sections(values),
         "placeholder": runtime.SECRET_PLACEHOLDER, "flash": request.session.pop("flash", None)},
    )


@router.post("/settings")
async def settings_save(request: Request, db: Session = Depends(get_db)):
    user = _admin(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    form = dict(await request.form())
    runtime.save_settings(db, form)
    request.session["flash"] = "Settings saved."
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/test-notification")
def settings_test(request: Request, db: Session = Depends(get_db)):
    """Send a test alert through every enabled channel and report each result."""
    from central.channels import Notification, active_channels, dispatch

    user = _admin(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    channels = active_channels(runtime.load_settings(db))
    if not channels:
        request.session["flash"] = (
            "No channels enabled — turn on Email and/or FreeScout above, then save first."
        )
        return RedirectResponse("/settings", status_code=303)
    note = Notification(
        title="Printer Nanny test notification",
        body="If you're reading this, the channel is wired up correctly.",
        severity="info",
        client_name="Test Client",
        site_name="Test Site",
        printer_label="Test Printer @ 10.0.0.1",
    )
    results = dispatch(note, channels)
    summary = "; ".join(
        f"{name}: {'OK' if res.ok else 'FAILED'} ({res.detail})" for name, res in results
    )
    request.session["flash"] = f"Test sent — {summary}"
    return RedirectResponse("/settings", status_code=303)
