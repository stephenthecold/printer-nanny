"""OAuth2 (XOAUTH2) outbound SMTP for Gmail and Microsoft 365.

Modern auth replaces SMTP basic-auth (app passwords / legacy SMTP AUTH) for the
two providers MSPs actually use. The flow is the standard authorization-code
grant with `offline_access` so we get a refresh token; the access token is
short-lived (~1h) and refreshed on demand by ``EmailChannel.send``.

Settings are stored under the existing ``smtp.*`` keys via ``central.runtime``
so the rest of the app keeps treating them as ordinary settings.
"""

from __future__ import annotations

import secrets
import time
from base64 import b64encode
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from central import models as m
from central import runtime
from central.db import get_db

router = APIRouter(tags=["auth-smtp"])

CALLBACK_PATH = "/settings/smtp-oauth/callback"


# --- Provider config --------------------------------------------------------- #
def _provider_config(provider: str, tenant: str) -> Dict[str, Any]:
    """Per-provider OAuth endpoints + scopes. Tenant only matters for Microsoft.

    Gmail: scope must include ``https://mail.google.com/`` for SMTP/IMAP access.
    Microsoft 365: ``offline_access`` (refresh token) + ``SMTP.Send`` for SMTP outbound.
    """
    if provider == "google":
        return {
            "name": "Gmail",
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scope": "https://mail.google.com/",
            "extra_auth": {"access_type": "offline", "prompt": "consent"},
        }
    if provider == "microsoft":
        t = tenant or "common"
        return {
            "name": "Microsoft 365",
            "auth_url": f"https://login.microsoftonline.com/{t}/oauth2/v2.0/authorize",
            "token_url": f"https://login.microsoftonline.com/{t}/oauth2/v2.0/token",
            "scope": "offline_access https://outlook.office.com/SMTP.Send",
            "extra_auth": {"prompt": "consent"},
        }
    raise ValueError(f"unknown provider: {provider}")


def _provider_from_auth_type(auth_type: str) -> Optional[str]:
    if auth_type == "oauth_google":
        return "google"
    if auth_type == "oauth_microsoft":
        return "microsoft"
    return None


def redirect_uri(request: Request) -> str:
    """Public redirect URI for the consent callback — operator pastes this into
    the cloud console's allowed-redirects list during app registration."""
    return str(request.base_url).rstrip("/") + CALLBACK_PATH


# --- Token plumbing --------------------------------------------------------- #
def build_xoauth2(email: str, access_token: str) -> bytes:
    """Build the SASL XOAUTH2 string for SMTP AUTH (RFC-defined format)."""
    raw = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
    return b64encode(raw.encode("utf-8"))


async def _refresh_token_async(
    provider: str, tenant: str, client_id: str, client_secret: str, refresh_token: str
) -> Dict[str, Any]:
    """Exchange a refresh token for a new access token. Returns the token JSON."""
    cfg = _provider_config(provider, tenant)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    # Google requires client_secret even for "Desktop"/"Web" apps; Microsoft
    # public clients skip it (we send it when present and the provider ignores it).
    if client_secret:
        data["client_secret"] = client_secret
    if provider == "google":
        data["scope"] = cfg["scope"]
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(cfg["token_url"], data=data)
        resp.raise_for_status()
        return resp.json()


def refresh_access_token(db: Session, settings: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Refresh the access token if it's missing or about to expire.

    Persists the new token via ``runtime.save_settings`` and returns the access
    token string (or None if no refresh token is configured). Synchronous so the
    existing ``EmailChannel.send`` can call it without restructuring.
    """
    settings = settings or runtime.load_settings(db)
    auth_type = str(settings.get("smtp.auth_type") or "basic")
    provider = _provider_from_auth_type(auth_type)
    if provider is None:
        return None
    refresh_token = settings.get("smtp.oauth_refresh_token") or ""
    if not refresh_token:
        return None
    now = int(time.time())
    expires_at = int(settings.get("smtp.oauth_access_token_expires_at") or 0)
    cached_token = str(settings.get("smtp.oauth_access_token") or "")
    if cached_token and expires_at - now > 60:
        return cached_token  # still valid for at least another minute

    cfg = _provider_config(provider, str(settings.get("smtp.oauth_tenant_id") or "common"))
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": str(settings.get("smtp.oauth_client_id") or ""),
    }
    client_secret = str(settings.get("smtp.oauth_client_secret") or "")
    if client_secret:
        data["client_secret"] = client_secret
    if provider == "google":
        data["scope"] = cfg["scope"]
    resp = httpx.post(cfg["token_url"], data=data, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    access_token = payload["access_token"]
    expires_in = int(payload.get("expires_in", 3600))
    updates = {
        "smtp.oauth_access_token": access_token,
        "smtp.oauth_access_token_expires_at": str(now + expires_in),
    }
    # Some providers issue a rolling refresh token; persist it if so.
    new_refresh = payload.get("refresh_token")
    if new_refresh:
        updates["smtp.oauth_refresh_token"] = new_refresh
    runtime.save_settings(db, updates)
    return access_token


# --- Consent flow ----------------------------------------------------------- #
def _admin(request: Request, db: Session) -> Optional[m.User]:
    uid = request.session.get("user_id")
    user = db.get(m.User, uid) if uid else None
    if user is None or user.role != m.UserRole.admin:
        return None
    return user


def _err(message: str) -> RedirectResponse:
    return RedirectResponse(f"/settings?smtp_oauth_error={message}", status_code=303)


@router.get("/settings/smtp-oauth/start")
def start(provider: str, request: Request, db: Session = Depends(get_db)):
    """Admin-initiated: redirect to the provider's consent page."""
    if _admin(request, db) is None:
        return RedirectResponse("/login", status_code=303)
    if provider not in ("google", "microsoft"):
        return _err("unknown_provider")
    settings = runtime.load_settings(db)
    client_id = str(settings.get("smtp.oauth_client_id") or "")
    if not client_id:
        return _err("missing_client_id")
    cfg = _provider_config(provider, str(settings.get("smtp.oauth_tenant_id") or "common"))
    state = secrets.token_urlsafe(24)
    request.session["smtp_oauth_state"] = state
    request.session["smtp_oauth_provider"] = provider
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri(request),
        "scope": cfg["scope"],
        "state": state,
        **cfg["extra_auth"],
    }
    return RedirectResponse(str(httpx.URL(cfg["auth_url"], params=params)), status_code=303)


@router.get(CALLBACK_PATH)
async def callback(
    request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)
):
    if _admin(request, db) is None:
        return RedirectResponse("/login", status_code=303)
    expected_state = request.session.pop("smtp_oauth_state", None)
    provider = request.session.pop("smtp_oauth_provider", None)
    if not code or state != expected_state or provider not in ("google", "microsoft"):
        return _err("state_mismatch")
    settings = runtime.load_settings(db)
    client_id = str(settings.get("smtp.oauth_client_id") or "")
    client_secret = str(settings.get("smtp.oauth_client_secret") or "")
    tenant = str(settings.get("smtp.oauth_tenant_id") or "common")
    cfg = _provider_config(provider, tenant)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(cfg["token_url"], data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri(request),
                "client_id": client_id,
                "client_secret": client_secret,
            })
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        return _err(f"exchange_failed:{type(exc).__name__}")

    if "refresh_token" not in payload:
        # Without offline_access / access_type=offline we'd get only a short-lived
        # access token — useless for background sending.
        return _err("no_refresh_token")

    now = int(time.time())
    updates = {
        "smtp.auth_type": f"oauth_{provider}",
        "smtp.oauth_refresh_token": payload["refresh_token"],
        "smtp.oauth_access_token": payload.get("access_token", ""),
        "smtp.oauth_access_token_expires_at": str(now + int(payload.get("expires_in", 3600))),
    }
    runtime.save_settings(db, updates)
    request.session["flash"] = (
        f"Connected to {cfg['name']} — outbound email now uses OAuth (XOAUTH2)."
    )
    return RedirectResponse("/settings", status_code=303)
