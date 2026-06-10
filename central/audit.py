"""Audit-trail writer.

``record`` appends one AuditLog row inside the caller's session/transaction;
the caller's existing ``db.commit()`` persists it together with the action it
describes (so a rolled-back action doesn't leave a phantom audit row).

It must NEVER break the action being audited: any failure is swallowed and
logged. Secrets must never reach ``detail`` -- callers log key NAMES of
changed settings, not values.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from central import models as m

log = logging.getLogger("central.audit")


def _client_ip(request: Optional[Request]) -> Optional[str]:
    if request is None or request.client is None:
        return None
    # ProxyHeadersMiddleware already rewrote request.client from
    # X-Forwarded-For when behind Caddy/Nginx.
    return request.client.host


def record(
    db: Session,
    request: Optional[Request],
    user: Optional[m.User],
    action: str,
    target: str = "",
    detail: str = "",
) -> None:
    """Append an audit row to the caller's transaction. Never raises."""
    try:
        db.add(m.AuditLog(
            user_id=user.id if user is not None else None,
            username=user.username if user is not None else None,
            ip=_client_ip(request),
            action=action[:80],
            target=(target or None) and target[:300],
            detail=(detail or None) and detail[:4000],
        ))
    except Exception:  # noqa: BLE001 - auditing must never break the action
        log.exception("failed to record audit row for action %r", action)


def record_anonymous(
    db: Session,
    request: Optional[Request],
    username: str,
    action: str,
    target: str = "",
    detail: str = "",
) -> None:
    """Audit row for pre-authentication events (failed logins): we know the
    attempted username string but have no User row to attach."""
    try:
        db.add(m.AuditLog(
            user_id=None,
            username=(username or None) and username[:120],
            ip=_client_ip(request),
            action=action[:80],
            target=(target or None) and target[:300],
            detail=(detail or None) and detail[:4000],
        ))
    except Exception:  # noqa: BLE001
        log.exception("failed to record audit row for action %r", action)
