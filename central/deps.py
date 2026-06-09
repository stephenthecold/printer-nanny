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
    """Return the logged-in dashboard user from the signed session, or None."""
    uid = request.session.get("user_id")
    if not uid:
        return None
    return db.get(m.User, uid)


def require_user(user: Optional[m.User] = Depends(current_user)) -> m.User:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Login required")
    return user


def require_admin(user: m.User = Depends(require_user)) -> m.User:
    if user.role != m.UserRole.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
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
