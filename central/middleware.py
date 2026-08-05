"""Two small ASGI middlewares whose correctness is their position in the stack.

Starlette's ``add_middleware`` inserts at index 0 and the stack is built by
wrapping in reverse, so **the last one added is the outermost**. Both of these are
wrong if that ordering is disturbed, and both fail silently rather than loudly:

* ``PeerAddressMiddleware`` must sit OUTSIDE ``ProxyHeadersMiddleware``. That
  middleware overwrites ``scope["client"]`` in place from ``X-Forwarded-For``, so
  anything inside it has already lost the real TCP peer -- and would key the login
  throttle on a value the caller chose (central/net.py).
* ``ForcePasswordChangeMiddleware`` must sit INSIDE ``SessionMiddleware``, which
  is what puts ``scope["session"]`` there. Outside it, the session is simply
  absent, the check reads False for everybody, and the gate is open with nothing
  to show for it.
"""

from __future__ import annotations

from typing import Any, Callable, Tuple

from starlette.responses import RedirectResponse

from central.net import PEER_SCOPE_KEY

#: Session key set at login for an account whose password was generated for it.
FORCE_PASSWORD_CHANGE_KEY = "must_change_password"

#: Paths a must-change-password session may still reach. ``/account`` covers the
#: change form and its POST; ``/logout`` and ``/login`` keep the way out open;
#: the rest are unauthenticated infrastructure that a redirect would only break.
FORCE_PASSWORD_CHANGE_EXEMPT: Tuple[str, ...] = (
    "/account", "/logout", "/login", "/static", "/healthz", "/readyz",
)


class PeerAddressMiddleware:
    """Stash the real TCP peer before any proxy header is believed."""

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            client = scope.get("client")
            scope[PEER_SCOPE_KEY] = str(client[0]) if client else ""
        await self.app(scope, receive, send)


class ForcePasswordChangeMiddleware:
    """Serve nothing but the change-password screen until it has been used.

    The first-run bootstrap prints a generated admin password to the container
    log, where it stays for the life of the log. Redirecting at login alone would
    not retire it -- the operator could simply navigate elsewhere -- so the whole
    dashboard is closed until the password is changed. Rate-limit and audit
    behaviour is unaffected: this gates an authenticated session, not
    authentication.

    Reads the session rather than the database so it costs no query on the
    ordinary request, where the flag is absent.
    """

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        session = scope.get("session") or {}
        path = scope.get("path", "")
        if not session.get(FORCE_PASSWORD_CHANGE_KEY) or \
                path.startswith(FORCE_PASSWORD_CHANGE_EXEMPT):
            await self.app(scope, receive, send)
            return
        response = RedirectResponse("/account", status_code=303)
        await response(scope, receive, send)


#: Sent on every response. Deliberately short, and deliberately not a full CSP:
#: this codebase's templates carry inline event handlers by design (the blessed
#: `data-` + `dataset` shape), so a `script-src` policy strict enough to be worth
#: having would break them, and one loose enough not to would be decoration.
#: These four cost nothing and close things a CSRF token cannot.
_SECURITY_HEADERS = (
    # The CSRF module's motivating case is /admin/backup -- a whole-database
    # dump, plus a restore. A synchronizer token does NOT stop a framed,
    # transparent dashboard where an admin is baited into clicking the real
    # button: the token is in the frame, and it is correct. Framing is the hole
    # the token cannot cover, so it gets closed here.
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    # Device-supplied strings reach exports and the remote-hands body view; a
    # browser sniffing one of those into HTML would undo the escaping.
    (b"x-content-type-options", b"nosniff"),
    # Dashboard URLs carry client and printer ids. Full URLs should not travel to
    # a printer's embedded web server in a Referer header.
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
)


class SecurityHeadersMiddleware:
    """Add the headers above, without overwriting a route that set its own.

    ``central/dashboard/remote.py`` serves a captured device page under a much
    stricter, purpose-built CSP (``sandbox; default-src 'none'; ...``). Blanket
    -setting a weaker one here would replace it, so anything already present
    wins -- the specific policy beats the default.
    """

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Any) -> None:
            if message.get("type") == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {name.lower() for name, _ in headers}
                for name, value in _SECURITY_HEADERS:
                    if name not in present:
                        headers.append((name, value))
            await send(message)

        await self.app(scope, receive, send_with_headers)
