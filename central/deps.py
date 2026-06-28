"""FastAPI dependencies: agent API-key auth and dashboard user/session auth."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from central import models as m
from central.db import get_db
from central.security import hash_api_key


def authenticated_agent(
    agent_id: int,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> m.Agent:
    """Resolve the path ``agent_id`` and verify the Bearer API key matches it."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization[7:].strip()
    agent = db.get(m.Agent, agent_id)
    if agent is None or agent.api_key_hash != hash_api_key(token):
        # Same error whether the id or the key is wrong — don't leak which.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid agent credentials")
    return agent


def current_user(request: Request, db: Session = Depends(get_db)) -> Optional[m.User]:
    """Return the logged-in dashboard user from the signed session, or None.

    A deactivated account (``User.active is False``) is treated as logged out:
    it resolves to ``None`` so a session that was live when the user was
    deprovisioned (e.g. via SCIM PATCH ``active=false``) stops working on its
    very next request, not just at the next login attempt.
    """
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = db.get(m.User, uid)
    if user is None or not user.active:
        return None
    return user


def require_user(user: Optional[m.User] = Depends(current_user)) -> m.User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Login required")
    return user


def require_admin(user: m.User = Depends(require_user)) -> m.User:
    if user.role != m.UserRole.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


def require_staff(user: m.User = Depends(require_user)) -> m.User:
    """Operator-only API surface (management CRUD + fleet reporting).

    admin/tech are the operator roles; client_readonly users are pinned to the
    customer ``/portal`` and their own tenant-scoped CSV exports, and must never
    reach the cross-tenant management/reporting JSON. Mirrors the dashboard's
    ``_MANAGER_ROLES`` gate so the JSON API enforces the same boundary the HTML
    management routes already do.
    """
    if user.role not in (m.UserRole.admin, m.UserRole.tech):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff (admin or tech) only")
    return user


def touch_heartbeat(
    agent: m.Agent,
    version: Optional[str] = None,
    install_path: Optional[str] = None,
    last_update_result: Optional[dict] = None,
) -> None:
    """Update agent online state + diagnostic fields from a heartbeat payload.

    The diagnostic fields are operator-facing only; they don't change agent
    routing or auth. Passed individually instead of stuffing them in the
    payload so the agent-driven callsites (readings, discovery, commands,
    targets, config) that just bump last_heartbeat stay terse.
    """
    agent.last_heartbeat = datetime.now(timezone.utc)
    agent.status = m.AgentStatus.online
    if version:
        agent.version = version
    if install_path:
        agent.install_path = install_path
    if last_update_result is not None:
        agent.last_update_result = last_update_result
