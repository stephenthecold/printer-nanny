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
