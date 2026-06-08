"""Settings page — edit DB-backed runtime config (admin only)."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Optional

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from central import models as m
from central import runtime
from central.db import get_db

router = APIRouter(tags=["settings"])
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

LOGO_ASSET_NAME = "logo"
LOGO_MAX_BYTES = 512 * 1024  # 512 KB — enough for a logo, blocks accidental DB bloat
LOGO_ALLOWED_TYPES = {
    "image/png", "image/jpeg", "image/svg+xml", "image/webp", "image/gif",
}


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
def settings_page(
    request: Request,
    smtp_oauth_error: str = "",
    db: Session = Depends(get_db),
):
    from central.auth_oauth_smtp import CALLBACK_PATH

    user = _admin(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    values = runtime.load_settings(db)
    has_uploaded_logo = db.get(m.AppAsset, LOGO_ASSET_NAME) is not None
    return _templates.TemplateResponse(
        request, "settings.html",
        {"user": user, "sections": _sections(values),
         "placeholder": runtime.SECRET_PLACEHOLDER,
         "app": runtime.app_branding(db),
         "flash": request.session.pop("flash", None),
         "logo_error": request.session.pop("logo_error", None),
         "has_uploaded_logo": has_uploaded_logo,
         "smtp_oauth_error": smtp_oauth_error or None,
         "smtp_auth_type": str(values.get("smtp.auth_type") or "basic"),
         "smtp_has_refresh_token": bool(values.get("smtp.oauth_refresh_token")),
         "smtp_oauth_redirect_uri": str(request.base_url).rstrip("/") + CALLBACK_PATH},
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


@router.post("/settings/branding/logo")
async def upload_logo(
    request: Request,
    logo: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Store an uploaded logo in app_assets and point app.logo_url at /branding/logo.

    Operators don't need an external image host for one small file — let them
    drop it in here and the dashboard / login page pick it up immediately.
    """
    user = _admin(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    content_type = (logo.content_type or "").lower()
    if content_type not in LOGO_ALLOWED_TYPES:
        request.session["logo_error"] = (
            f"Unsupported file type: {content_type or 'unknown'}. "
            "Use PNG, JPEG, SVG, WEBP, or GIF."
        )
        return RedirectResponse("/settings", status_code=303)
    data = await logo.read()
    if not data:
        request.session["logo_error"] = "Empty file uploaded."
        return RedirectResponse("/settings", status_code=303)
    if len(data) > LOGO_MAX_BYTES:
        request.session["logo_error"] = (
            f"File too large: {len(data) // 1024} KB (limit "
            f"{LOGO_MAX_BYTES // 1024} KB)."
        )
        return RedirectResponse("/settings", status_code=303)
    existing = db.get(m.AppAsset, LOGO_ASSET_NAME)
    if existing is None:
        db.add(m.AppAsset(
            name=LOGO_ASSET_NAME, content_type=content_type, data=data,
            updated_at=datetime.now(timezone.utc),
        ))
    else:
        existing.content_type = content_type
        existing.data = data
        existing.updated_at = datetime.now(timezone.utc)
    # Point the existing app.logo_url setting at the served route so every template
    # that already reads `app.logo_url` picks the upload up without further work.
    runtime.save_settings(db, {"app.logo_url": "/branding/logo"})
    db.commit()
    request.session["flash"] = f"Logo uploaded ({len(data) // 1024} KB)."
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/branding/logo/delete")
def delete_logo(request: Request, db: Session = Depends(get_db)):
    user = _admin(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    existing = db.get(m.AppAsset, LOGO_ASSET_NAME)
    if existing is not None:
        db.delete(existing)
    # Clear the app.logo_url if (and only if) it was set to the served route.
    # An operator who pasted an external URL keeps it.
    values = runtime.load_settings(db)
    if str(values.get("app.logo_url") or "") == "/branding/logo":
        runtime.save_settings(db, {"app.logo_url": ""})
    db.commit()
    request.session["flash"] = "Logo removed."
    return RedirectResponse("/settings", status_code=303)


@router.get("/branding/logo")
def serve_logo(db: Session = Depends(get_db)):
    """Public endpoint that returns the uploaded logo bytes.

    Public by design — same exposure surface as a logo on the login page. Clients
    cache it for an hour; uploads bump the URL via a cache-busting suffix on the
    settings page (no manual purge needed for the operator's own browser).
    """
    asset = db.get(m.AppAsset, LOGO_ASSET_NAME)
    if asset is None:
        return Response(status_code=404)
    return Response(
        content=asset.data, media_type=asset.content_type,
        headers={"Cache-Control": "public, max-age=3600"},
    )


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
