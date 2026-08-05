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


def authenticated_machine(
    machine_id: int,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> m.Machine:
    """Resolve the path ``machine_id`` and verify the Bearer key matches it.

    Mirrors ``authenticated_agent`` deliberately, including the single
    indistinguishable error: reporting "no such machine" separately from "wrong
    key" turns this into an oracle for enumerating a tenant's machines.

    An inactive machine is rejected here rather than further in. Deactivating a
    machine in the UI must stop it polling on its very next request -- if it only
    took effect at the resolver, a retired PC would keep authenticating and keep
    being told it has no printers, which reads as a broken client rather than a
    retired one.
    """
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    token = authorization[7:].strip()
    machine = db.get(m.Machine, machine_id)
    if (
        machine is None
        or not machine.active
        or not machine.api_key_hash
        or machine.api_key_hash != hash_api_key(token)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid machine credentials")
    return machine


#: Session key holding the ``User.session_epoch`` this session was minted under.
#: Compared on every request; a mismatch means the user rotated a credential or
#: logged out, so the cookie is spent even though its signature still verifies.
SESSION_EPOCH_KEY = "epoch"


def session_is_current(request_session, user) -> bool:
    """Has this session survived the user's credential rotations?

    Sessions here are signed cookies with no server-side store, so nothing can
    delete one -- ``session.clear()`` asks the *browser* to drop it and the
    signed value keeps verifying for the full max_age. Comparing an epoch is how
    a logout, a password change or an admin reset actually revokes.

    A session minted before the column existed carries no epoch. Treating that
    as current is deliberate: the alternative logs out every operator on upgrade
    to fix a problem none of them has yet, and the first rotation after upgrade
    stamps it properly.
    """
    stamped = request_session.get(SESSION_EPOCH_KEY)
    return stamped is None or int(stamped) == int(user.session_epoch or 0)


def session_user(request: Request, db: Session) -> Optional[m.User]:
    """Resolve a signed session to a live user, or ``None``. THE one place.

    Every dashboard module used to spell this itself -- seven copies of
    ``session.get("user_id")`` -> ``db.get`` -> maybe check ``active``. They had
    already drifted (``auth_oauth_smtp`` never checked ``active`` at all, so a
    deactivated admin could still run the SMTP consent flow), and adding the
    epoch check to two of them left the other five accepting a revoked cookie --
    which is how the first attempt at this fix passed its own logout test and
    failed on ``/manage/users``.

    Three conditions, and they are not negotiable per call site:

    * the session names a user,
    * that user is ``active`` -- so an off-boarding lands on the next request
      rather than at the next login,
    * and the session was minted at the user's current ``session_epoch`` -- so
      logout, a password change and an admin reset actually revoke.

    Role checks stay with the caller: they differ per module and are not a
    property of "is this session valid".
    """
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = db.get(m.User, uid)
    if user is None or not user.active:
        return None
    if not session_is_current(request.session, user):
        return None
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> Optional[m.User]:
    """Return the logged-in dashboard user from the signed session, or None.

    A deactivated account (``User.active is False``) is treated as logged out:
    it resolves to ``None`` so a session that was live when the user was
    deprovisioned (e.g. via SCIM PATCH ``active=false``) stops working on its
    very next request, not just at the next login attempt.
    """
    return session_user(request, db)


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
