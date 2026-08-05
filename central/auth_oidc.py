"""Pluggable OIDC / SSO login.

Standard Authorization Code flow, configured entirely from the DB-backed settings
(Settings → Single sign-on) so an MSP can point it at any OIDC IdP — Entra ID,
Okta, Google, Authentik, Keycloak — without code changes or env vars. Local
username/password login always remains available as a fallback.

Disabled by default; the login page shows an SSO button only when enabled.
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import runtime
from central.csrf import rotate_token
from central.db import get_db
from central.deps import SESSION_EPOCH_KEY
from central.middleware import FORCE_PASSWORD_CHANGE_KEY

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

CALLBACK_PATH = "/auth/sso/callback"


def oidc_config(db: Session) -> dict:
    s = runtime.load_settings(db)
    return {k.split(".", 1)[1]: v for k, v in s.items() if k.startswith("oidc.")}


def oidc_enabled(db: Session) -> bool:
    cfg = oidc_config(db)
    return bool(cfg.get("enabled") and cfg.get("issuer") and cfg.get("client_id"))


async def _discover(issuer: str) -> dict:
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=15) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        return resp.json()


def _redirect_uri(request: Request) -> str:
    # Honors the external scheme/host seen by the reverse proxy where available.
    base = str(request.base_url).rstrip("/")
    return base + CALLBACK_PATH


def _err(message: str) -> RedirectResponse:
    # Surface the reason on the login page without leaking specifics in the URL.
    return RedirectResponse(f"/login?sso_error={message}", status_code=303)


@router.get("/auth/sso/login")
async def sso_login(request: Request, db: Session = Depends(get_db)):
    if not oidc_enabled(db):
        return _err("disabled")
    cfg = oidc_config(db)
    try:
        meta = await _discover(cfg["issuer"])
    except httpx.HTTPError:
        return _err("discovery_failed")

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    request.session["oidc_state"] = state
    request.session["oidc_nonce"] = nonce

    params = {
        "client_id": cfg["client_id"],
        "response_type": "code",
        "scope": cfg.get("scopes") or "openid email profile",
        "redirect_uri": _redirect_uri(request),
        "state": state,
        "nonce": nonce,
    }
    auth_url = httpx.URL(meta["authorization_endpoint"], params=params)
    return RedirectResponse(str(auth_url), status_code=303)


@router.get(CALLBACK_PATH)
async def sso_callback(
    request: Request, code: str = "", state: str = "", db: Session = Depends(get_db)
):
    if not oidc_enabled(db):
        return _err("disabled")
    if not code or state != request.session.pop("oidc_state", None):
        return _err("state_mismatch")
    nonce = request.session.pop("oidc_nonce", None)
    cfg = oidc_config(db)

    try:
        meta = await _discover(cfg["issuer"])
        async with httpx.AsyncClient(timeout=15) as c:
            token_resp = await c.post(
                meta["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _redirect_uri(request),
                    "client_id": cfg["client_id"],
                    "client_secret": cfg.get("client_secret", ""),
                },
                headers={"Accept": "application/json"},
            )
            token_resp.raise_for_status()
            tokens = token_resp.json()
            claims = await _verify_id_token(c, meta, tokens.get("id_token", ""), cfg, nonce)
            email = claims.get("email") or await _userinfo_email(c, meta, tokens.get("access_token"))
    except (httpx.HTTPError, ValueError) as exc:
        return _err(f"exchange_failed:{type(exc).__name__}")

    if not email:
        return _err("no_email")

    user = _match_or_provision(db, email, claims, cfg)
    if user is None:
        return _err("not_provisioned")
    request.session["user_id"] = user.id
    request.session[SESSION_EPOCH_KEY] = user.session_epoch or 0
    # Same rule as the local login: a CSRF token issued before authentication is
    # not this user's secret, so it does not survive becoming their session.
    rotate_token(request.session)
    # ...and the same rule for forced rotation. The middleware gates on a SESSION
    # key, which the local login sets and this path did not -- so signing in
    # through SSO walked straight past it into the whole dashboard, and the flag
    # was never cleared, so the password printed to the container log at
    # bootstrap stayed live indefinitely. Cheap to set here; the alternative is a
    # DB read on every request the middleware sees.
    if user.must_change_password:
        request.session[FORCE_PASSWORD_CHANGE_KEY] = True
        return RedirectResponse("/account", status_code=303)
    return RedirectResponse("/", status_code=303)


async def _verify_id_token(
    client: httpx.AsyncClient, meta: dict, id_token: str, cfg: dict, nonce: Optional[str]
) -> dict:
    """Validate the ID token signature and standard claims via the IdP's JWKS."""
    from authlib.jose import JsonWebKey, jwt

    if not id_token:
        raise ValueError("missing id_token")
    jwks_resp = await client.get(meta["jwks_uri"])
    jwks_resp.raise_for_status()
    key_set = JsonWebKey.import_key_set(jwks_resp.json())
    claims = jwt.decode(
        id_token,
        key_set,
        claims_options={
            "iss": {"essential": True, "value": meta.get("issuer", cfg["issuer"])},
            "aud": {"essential": True, "value": cfg["client_id"]},
        },
    )
    claims.validate()  # exp / iat / nbf
    # If we sent a nonce, the token MUST echo it back exactly (a missing nonce
    # claim is a failure, not a pass — prevents replay).
    if nonce and claims.get("nonce") != nonce:
        raise ValueError("nonce mismatch")
    return dict(claims)


async def _userinfo_email(
    client: httpx.AsyncClient, meta: dict, access_token: Optional[str]
) -> Optional[str]:
    endpoint = meta.get("userinfo_endpoint")
    if not endpoint or not access_token:
        return None
    resp = await client.get(endpoint, headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code != 200:
        return None
    return resp.json().get("email")


#: ``auth_provider`` values that an SSO login may adopt without an operator
#: saying so first. Both are already IdP-managed identities -- ``scim`` in
#: particular is the normal Entra/Okta shape, where SCIM provisions the account
#: and OIDC authenticates it, so refusing that pairing would break the common
#: deployment. ``local`` is deliberately absent: see ``_match_or_provision``.
_LINKABLE_PROVIDERS = frozenset({"oidc", "scim"})


def _match_or_provision(db: Session, email: str, claims: dict, cfg: dict) -> Optional[m.User]:
    email = email.lower()
    # An IdP that says the address is NOT verified is telling us the user typed
    # it. Self-service and guest sign-up are ordinary features of Entra, Google
    # and Keycloak, so an unverified claim is attacker-chosen text -- and this
    # function turns text into "which account is this". A *missing* claim is
    # tolerated because plenty of IdPs omit it for their own managed users;
    # an explicit false is not.
    if claims.get("email_verified") is False:
        log.warning("SSO refused: the IdP reports %s as unverified", email)
        return None

    user = db.scalar(
        select(m.User).where((m.User.email == email) | (m.User.username == email))
    )
    if user is not None:
        # A deactivated (SCIM-deprovisioned / manually disabled) account must not
        # be able to log back in via SSO -- treat it as "not provisioned" so the
        # off-boarding gate holds across both auth paths.
        if not user.active:
            return None
        # DO NOT let an SSO login adopt a PASSWORD account. Matching alone used
        # to be enough, which made this the shortest path to admin in the
        # product: register any address the IdP will accept, and the first
        # existing row whose email OR username equals it becomes your session --
        # including the bootstrap admin, whose username is an operator's address
        # often enough. No password, no throttle, no forced rotation, and the
        # match ran BEFORE the auto_provision gate, so turning auto-provisioning
        # off did not close it either.
        #
        # Linking is still legitimate -- an MSP moving a local account to SSO --
        # so it is a decision an operator makes, not one an unauthenticated
        # caller makes for them. Two supported ways to grant it: set
        # ``oidc.link_local_accounts``, or change that user's auth provider.
        if user.auth_provider not in _LINKABLE_PROVIDERS and not cfg.get(
            "link_local_accounts", False
        ):
            log.warning(
                "SSO refused: %s already exists as a %r account. Enable "
                "'Let SSO adopt existing password accounts' in Settings if this "
                "is a deliberate migration.",
                email, user.auth_provider,
            )
            return None
        if user.email is None:
            user.email = email
        db.commit()
        return user
    if not cfg.get("auto_provision", True):
        return None
    try:
        role = m.UserRole(cfg.get("default_role", "tech"))
    except ValueError:
        role = m.UserRole.tech
    user = m.User(
        username=email, email=email, auth_provider="oidc", password_hash=None, role=role
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
