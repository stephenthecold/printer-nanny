"""SCIM 2.0 (RFC 7644) user provisioning + deprovisioning endpoints.

This is the enterprise off-boarding gate: an IdP (Entra ID, Okta, OneLogin, …)
calls these endpoints to CREATE users when they join, UPDATE them as their
attributes change, and -- critically -- DEPROVISION them on termination by
PATCHing ``active`` to false. A deactivated user can no longer log in (the
login route and ``central.deps.current_user`` both reject inactive accounts),
so disabling a leaver in the IdP disables them here within one sync cycle.

Scope: the RFC 7644 *core* subset that real connectors exercise --

  GET    /scim/v2/Users            list, optional ``filter=userName eq "..."``
  GET    /scim/v2/Users/{id}       fetch one
  POST   /scim/v2/Users            provision (create)
  PUT    /scim/v2/Users/{id}       full replace
  PATCH  /scim/v2/Users/{id}       partial update (the ``active=false`` path)
  DELETE /scim/v2/Users/{id}       deprovision (we DEACTIVATE, never hard-delete)

Auth: a single operator-configured bearer token (``Authorization: Bearer ...``),
stored as a SHA-256 hash exactly like an agent API key. Gated behind the
``scim.enabled`` runtime setting -- the whole surface returns 404 when off so it
isn't even discoverable on deployments that don't use SCIM.

Every mutating call is written to the audit log so an account's whole lifecycle
(provisioned / deactivated / reactivated / replaced) is attributable, the same
as the dashboard's own user CRUD.

DELETE intentionally DEACTIVATES rather than hard-deletes: it preserves the
audit trail and any historical references, and matches how IdPs treat a
de-provision (most send PATCH ``active=false`` anyway). The last-admin guard is
honoured on every path that could disable the final admin.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from central import models as m
from central import runtime
from central.audit import record
from central.db import get_db

router = APIRouter(prefix="/scim/v2", tags=["scim"])

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCIM_CONTENT_TYPE = "application/scim+json"


# --------------------------------------------------------------------------- #
# Auth + enablement
# --------------------------------------------------------------------------- #
def _scim_error(detail: str, code: int) -> HTTPException:
    """A SCIM-shaped error. FastAPI renders HTTPException.detail; we hand it the
    RFC 7644 error object so connectors get ``schemas``/``status``/``detail``."""
    return HTTPException(
        status_code=code,
        detail={"schemas": [ERROR_SCHEMA], "detail": detail, "status": str(code)},
    )


def require_scim(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> Session:
    """Gate the SCIM surface: feature must be enabled AND the bearer must match.

    Returns the db session so callers can ``Depends(require_scim)`` once and
    still take ``db`` separately; the real job is the side-effect of raising.
    When SCIM is disabled the whole surface 404s -- it shouldn't be discoverable.
    """
    settings = runtime.load_settings(db)
    if not settings.get("scim.enabled"):
        # 404, not 403: don't advertise the endpoint on deployments not using it.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    if not authorization.lower().startswith("bearer "):
        raise _scim_error("Missing or malformed bearer token", status.HTTP_401_UNAUTHORIZED)
    token = authorization[7:].strip()
    if not runtime.scim_token_matches(db, token):
        raise _scim_error("Invalid SCIM credentials", status.HTTP_401_UNAUTHORIZED)
    return db


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def _meta(user: m.User) -> Dict[str, Any]:
    created = user.created_at.isoformat() if user.created_at else None
    modified = (user.updated_at or user.created_at)
    return {
        "resourceType": "User",
        "created": created,
        "lastModified": modified.isoformat() if modified else None,
        "location": f"/scim/v2/Users/{user.id}",
    }


def _to_scim(user: m.User) -> Dict[str, Any]:
    """Render a User row as a SCIM core User resource."""
    out: Dict[str, Any] = {
        "schemas": [USER_SCHEMA],
        "id": str(user.id),
        "userName": user.username,
        "active": bool(user.active),
        "meta": _meta(user),
    }
    if user.scim_external_id:
        out["externalId"] = user.scim_external_id
    if user.email:
        out["emails"] = [{"value": user.email, "primary": True}]
    # displayName helps connectors show a friendly label; userName is the anchor.
    out["displayName"] = user.username
    # A nested role is informational only; provisioning role policy is operator
    # controlled (scim.default_role), not dictated by the IdP payload.
    out["roles"] = [{"value": user.role.value, "primary": True}]
    return out


def _scim_response(payload: Dict[str, Any], code: int = status.HTTP_200_OK) -> Response:
    import json

    return Response(
        content=json.dumps(payload),
        status_code=code,
        media_type=SCIM_CONTENT_TYPE,
    )


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #
def _extract_email(body: Dict[str, Any]) -> Optional[str]:
    """Pull a primary email from a SCIM payload (``emails`` array or top-level)."""
    emails = body.get("emails")
    if isinstance(emails, list) and emails:
        primary = next(
            (e for e in emails if isinstance(e, dict) and e.get("primary")), None
        )
        chosen = primary or emails[0]
        if isinstance(chosen, dict) and chosen.get("value"):
            return str(chosen["value"]).strip().lower() or None
    # Some connectors send a bare ``email`` or put it under userName.
    direct = body.get("email")
    if isinstance(direct, str) and direct.strip():
        return direct.strip().lower()
    return None


def _resolve_username(body: Dict[str, Any], email: Optional[str]) -> Optional[str]:
    """SCIM userName is authoritative; fall back to email (the OIDC convention
    is username == email for provisioned users)."""
    uname = body.get("userName")
    if isinstance(uname, str) and uname.strip():
        return uname.strip()
    return email


def _default_role(db: Session) -> m.UserRole:
    raw = str(runtime.load_settings(db).get("scim.default_role") or "tech")
    try:
        return m.UserRole(raw)
    except ValueError:
        return m.UserRole.tech


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _admin_count(db: Session, *, active_only: bool = True) -> int:
    stmt = select(func.count()).select_from(m.User).where(m.User.role == m.UserRole.admin)
    if active_only:
        stmt = stmt.where(m.User.active.is_(True))
    return db.scalar(stmt) or 0


def _is_last_active_admin(db: Session, user: m.User) -> bool:
    """True when deactivating/removing ``user`` would leave zero active admins."""
    return (
        user.role == m.UserRole.admin
        and user.active
        and _admin_count(db, active_only=True) <= 1
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/Users")
def list_users(
    filter: str = "",  # noqa: A002 - SCIM spells the query param "filter"
    startIndex: int = 1,  # noqa: N803 - SCIM camelCase query params
    count: int = 100,  # noqa: N803
    db: Session = Depends(require_scim),
) -> Response:
    """List users, optionally filtered by ``userName eq "x"`` / ``emails eq "x"``.

    Only equality on userName/email is implemented -- that's what connectors use
    to look up "does this user already exist?" before a POST. Unrecognised
    filters return an empty list rather than erroring, which connectors tolerate.
    """
    stmt = select(m.User).order_by(m.User.id)
    needle = _parse_eq_filter(filter)
    if needle is not None:
        attr, value = needle
        low = value.lower()
        if attr in ("username", "externalid"):
            col = m.User.username if attr == "username" else m.User.scim_external_id
            stmt = stmt.where(func.lower(col) == low)
        else:  # email / emails / emails.value
            stmt = stmt.where(func.lower(m.User.email) == low)
    elif filter.strip():
        # A filter we don't understand -> no matches (RFC allows this).
        stmt = stmt.where(m.User.id == -1)

    rows = list(db.scalars(stmt))
    total = len(rows)
    # SCIM startIndex is 1-based.
    start = max(startIndex, 1) - 1
    page = rows[start:start + max(count, 0)] if count else rows[start:]
    return _scim_response({
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": max(startIndex, 1),
        "itemsPerPage": len(page),
        "Resources": [_to_scim(u) for u in page],
    })


def _parse_eq_filter(filter: str):  # noqa: A002
    """Parse a minimal SCIM ``attr eq "value"`` filter. Returns (attr, value) or None."""
    parts = filter.strip().split(None, 2)
    if len(parts) != 3 or parts[1].lower() != "eq":
        return None
    attr = parts[0].lower()
    value = parts[2].strip().strip('"')
    if attr not in ("username", "externalid", "email", "emails", "emails.value"):
        return None
    return attr, value


@router.get("/Users/{user_id}")
def get_user(user_id: int, db: Session = Depends(require_scim)) -> Response:
    user = db.get(m.User, user_id)
    if user is None:
        raise _scim_error(f"User {user_id} not found", status.HTTP_404_NOT_FOUND)
    return _scim_response(_to_scim(user))


@router.post("/Users")
async def create_user(request: Request, db: Session = Depends(require_scim)) -> Response:
    """Provision a user from a SCIM payload.

    Reuses the OIDC provisioning conventions: username defaults to the email,
    no local password (auth is via the IdP), role from ``scim.default_role``.
    If the user already exists (by userName or email) we reactivate + return it
    with 200 instead of erroring -- connectors re-POST on re-add and expect this.
    """
    body = await _json_body(request)
    email = _extract_email(body)
    username = _resolve_username(body, email)
    if not username:
        raise _scim_error("userName (or an email) is required", status.HTTP_400_BAD_REQUEST)

    existing = db.scalar(
        select(m.User).where(
            (func.lower(m.User.username) == username.lower())
            | ((m.User.email.is_not(None)) & (func.lower(m.User.email) == (email or "")))
        )
    )
    external_id = body.get("externalId")
    external_id = str(external_id).strip() if external_id else None
    if existing is not None:
        # Idempotent re-provision: ensure active, backfill email/externalId.
        reactivated = not existing.active
        existing.active = True
        if email and not existing.email:
            existing.email = email
        if external_id and not existing.scim_external_id:
            existing.scim_external_id = external_id
        record(db, request, None, "scim.user.provision",
               target=f"user:{existing.username}",
               detail="existing" + (";reactivated" if reactivated else ""))
        db.commit()
        db.refresh(existing)
        return _scim_response(_to_scim(existing), status.HTTP_200_OK)

    active = _as_bool(body.get("active"), True)
    user = m.User(
        username=username,
        email=email,
        password_hash=None,
        auth_provider="scim",
        role=_default_role(db),
        active=active,
        scim_external_id=external_id,
    )
    db.add(user)
    record(db, request, None, "scim.user.provision",
           target=f"user:{username}", detail=f"role={user.role.value};active={active}")
    db.commit()
    db.refresh(user)
    return _scim_response(_to_scim(user), status.HTTP_201_CREATED)


@router.put("/Users/{user_id}")
async def replace_user(
    user_id: int, request: Request, db: Session = Depends(require_scim)
) -> Response:
    """Full-replace (PUT): apply userName/email/active from the payload wholesale.

    Role is deliberately NOT taken from the IdP payload -- provisioning role
    policy stays operator-controlled. ``active`` is honoured (it's a common PUT
    deprovision path for connectors that don't PATCH).
    """
    user = db.get(m.User, user_id)
    if user is None:
        raise _scim_error(f"User {user_id} not found", status.HTTP_404_NOT_FOUND)
    body = await _json_body(request)
    new_active = _as_bool(body.get("active"), user.active)
    if not new_active and _is_last_active_admin(db, user):
        raise _scim_error(
            "Refused: this is the only active admin and cannot be deactivated",
            status.HTTP_409_CONFLICT,
        )
    email = _extract_email(body)
    username = _resolve_username(body, email)
    if username:
        user.username = username
    if email is not None:
        user.email = email
    external_id = body.get("externalId")
    if external_id is not None:
        user.scim_external_id = str(external_id).strip() or None
    became_inactive = user.active and not new_active
    user.active = new_active
    record(db, request, None, "scim.user.replace",
           target=f"user:{user.username}",
           detail=f"active={new_active}" + (";deactivated" if became_inactive else ""))
    db.commit()
    db.refresh(user)
    return _scim_response(_to_scim(user))


@router.patch("/Users/{user_id}")
async def patch_user(
    user_id: int, request: Request, db: Session = Depends(require_scim)
) -> Response:
    """Partial update (PATCH) -- the IdP's primary deprovision path.

    Implements the RFC 7644 PatchOp shape: a list of ``Operations`` with op
    ``replace``/``add`` and a ``path``/``value``. The case that matters most is
    ``{"op":"replace","path":"active","value":false}`` (and its
    no-path ``value:{"active":false}`` variant) -> deactivate. ``active:true``
    reactivates. We also accept the lenient top-level ``{"active": false}``
    body some connectors send.
    """
    user = db.get(m.User, user_id)
    if user is None:
        raise _scim_error(f"User {user_id} not found", status.HTTP_404_NOT_FOUND)
    body = await _json_body(request)

    changes = _collect_patch_changes(body)
    if "active" in changes:
        new_active = _as_bool(changes["active"], user.active)
        if not new_active and _is_last_active_admin(db, user):
            raise _scim_error(
                "Refused: this is the only active admin and cannot be deactivated",
                status.HTTP_409_CONFLICT,
            )
        became_inactive = user.active and not new_active
        reactivated = (not user.active) and new_active
        user.active = new_active
        action_detail = (
            "deactivated" if became_inactive
            else "reactivated" if reactivated
            else f"active={new_active}"
        )
        record(db, request, None, "scim.user.patch",
               target=f"user:{user.username}", detail=action_detail)
    if "userName" in changes and isinstance(changes["userName"], str) and changes["userName"].strip():
        user.username = changes["userName"].strip()
    if "email" in changes:
        val = changes["email"]
        user.email = (str(val).strip().lower() or None) if val else None
    if "externalId" in changes:
        val = changes["externalId"]
        user.scim_external_id = (str(val).strip() or None) if val else None
    db.commit()
    db.refresh(user)
    return _scim_response(_to_scim(user))


@router.delete("/Users/{user_id}")
def delete_user(
    user_id: int, request: Request, db: Session = Depends(require_scim)
) -> Response:
    """Deprovision via DELETE. We DEACTIVATE (soft) rather than hard-delete so the
    audit trail and historical references survive; this also matches how most
    IdPs treat a de-provision. Returns 204 either way (idempotent)."""
    user = db.get(m.User, user_id)
    if user is None:
        # 204 on a missing user keeps DELETE idempotent for connectors retrying.
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if _is_last_active_admin(db, user):
        raise _scim_error(
            "Refused: this is the only active admin and cannot be deprovisioned",
            status.HTTP_409_CONFLICT,
        )
    if user.active:
        user.active = False
        record(db, request, None, "scim.user.deprovision",
               target=f"user:{user.username}", detail="deactivated")
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON -> SCIM 400
        raise _scim_error("Request body is not valid JSON", status.HTTP_400_BAD_REQUEST)
    if not isinstance(body, dict):
        raise _scim_error("Request body must be a JSON object", status.HTTP_400_BAD_REQUEST)
    return body


def _collect_patch_changes(body: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten a PatchOp (or lenient top-level body) into {attr: value}.

    Handles three real-world shapes:
      * RFC PatchOp:   {"Operations":[{"op":"replace","path":"active","value":false}]}
      * No-path op:    {"Operations":[{"op":"replace","value":{"active":false}}]}
      * Lenient body:  {"active": false}
    Paths are matched case-insensitively and tolerate the
    ``urn:...:User:active`` fully-qualified form some connectors emit.
    """
    changes: Dict[str, Any] = {}
    ops = body.get("Operations")
    if isinstance(ops, list):
        for op in ops:
            if not isinstance(op, dict):
                continue
            opname = str(op.get("op", "")).lower()
            if opname not in ("replace", "add", "", "remove"):
                continue
            path = op.get("path")
            value = op.get("value")
            if path:
                attr = _normalize_attr(str(path))
                if attr:
                    changes[attr] = False if opname == "remove" else value
            elif isinstance(value, dict):
                for k, v in value.items():
                    attr = _normalize_attr(str(k))
                    if attr:
                        changes[attr] = v
    else:
        # Lenient: top-level attributes (no Operations array).
        for k in ("active", "userName", "email", "externalId"):
            if k in body:
                changes[k] = body[k]
        if "emails" in body and "email" not in changes:
            em = _scim_email_from_value(body.get("emails"))
            if em is not None:
                changes["email"] = em
    # Normalize an email that arrived as a SCIM array / object into a string.
    if isinstance(changes.get("email"), list):
        changes["email"] = _scim_email_from_value(changes["email"])
    elif isinstance(changes.get("email"), dict) and changes["email"].get("value"):
        changes["email"] = str(changes["email"]["value"])
    return changes


def _scim_email_from_value(value: Any) -> Optional[str]:
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, dict) and first.get("value"):
            return str(first["value"])
    if isinstance(value, str):
        return value
    return None


def _normalize_attr(path: str) -> Optional[str]:
    """Map a SCIM path (possibly URN-qualified) to one of our handled attrs."""
    tail = path.split(":")[-1].strip().lower()
    # Strip filter expressions like emails[type eq "work"].value -> emails.value
    tail = tail.split("[")[0]
    mapping = {
        "active": "active",
        "username": "userName",
        "externalid": "externalId",
        "email": "email",
        "emails": "email",
        "emails.value": "email",
    }
    return mapping.get(tail)
