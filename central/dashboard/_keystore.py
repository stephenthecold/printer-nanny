"""Tiny in-process one-shot store for freshly-minted agent keys.

The plaintext agent key is shown to the admin exactly once after enrollment. We
must NOT park it in the session cookie (Starlette signs but does not encrypt the
session, so the client can read it). Instead we keep it server-side keyed by a
random token, put only the token in the session, and pop it on first render.

Single-process API (uvicorn without --workers), so a module dict is sufficient;
a short TTL bounds exposure if the page is never loaded.
"""

from __future__ import annotations

import secrets
import time
from typing import Optional

_TTL_SECONDS = 600
_STORE: dict = {}  # token -> (payload, expires_at)


def _purge(now: float) -> None:
    for tok in [t for t, (_, exp) in _STORE.items() if exp <= now]:
        _STORE.pop(tok, None)


def put(payload: dict) -> str:
    now = time.time()
    _purge(now)
    token = secrets.token_urlsafe(16)
    _STORE[token] = (payload, now + _TTL_SECONDS)
    return token


def pop(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    now = time.time()
    _purge(now)
    item = _STORE.pop(token, None)
    if item is None:
        return None
    payload, exp = item
    return payload if exp > now else None
