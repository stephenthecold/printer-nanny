"""Synchronizer-token CSRF protection for the cookie-authenticated surface.

Until this existed the only defence was ``same_site="lax"`` on the session
cookie, and Lax is not a CSRF defence -- it is a partial mitigation with two
holes this deployment shape walks straight into:

* **SameSite is a *site* boundary, not an *origin* boundary.** ``lax`` compares
  registrable domains, so an MSP running the dashboard at
  ``printers.example.com`` and anything else -- a customer status page, a
  wiki, a compromised marketing host -- at ``www.example.com`` has a *same-site,
  cross-origin* attacker who gets the session cookie attached to every forged
  POST. Lax stops none of it.
* **Lax permits top-level GET navigation**, so any state change or disclosure
  behind a GET is reachable by making the operator's browser navigate. That is
  the shape ``/admin/backup/download`` had, and why it is now a POST.

The mechanism is a **synchronizer token stored in the signed session**, not a
double-submit cookie. Double-submit compares a cookie against a form field, and
the same same-site attacker above can *write* cookies for the parent domain --
so they can plant a value they know into both halves and the comparison passes.
A token that lives inside the tamper-proof session cookie cannot be planted,
only read, and reading it requires a same-origin response.

**What is protected is decided by the credential, not by a path list.** The
check runs when an unsafe request arrives carrying a non-empty session -- i.e.
exactly when there is an ambient credential to abuse. Agents, workstation
clients and SCIM connectors authenticate with ``Authorization: Bearer`` and
never send a cookie, so they are exempt *by construction*; nothing has to
remember to add them to a list, and a router added later cannot be missed.

A path allowlist would have been actively wrong here, which is worth stating
because it is the obvious first design. ``/api/v1`` is not the agent surface:
``central/api/management.py`` mounts at that same prefix and authenticates with
``require_staff``, which reads the **session cookie**. ``POST
/api/v1/printers/{id}/approve`` takes no body at all, so before this change a
plain cross-site ``<form>`` aimed at it succeeded. Exempting ``/api/v1`` would
have left the one JSON route that genuinely needed protecting unprotected,
while a prefix of ``/api/v1/agents`` would have swept in ``POST /api/v1/agents``
(session-authed agent creation) alongside the bearer-authed
``/api/v1/agents/{id}/...`` ingest routes.

**Two supply channels, one per mechanism, because neither covers both shapes.**
A plain ``<form>`` cannot set a request header, and an htmx button
(``hx-post`` with no body) has no form to carry a field. So the token is
accepted from the ``X-CSRF-Token`` header *or* the ``csrf_token`` form field.
Both are equally unforgeable cross-site: a custom header requires a CORS
preflight we never grant, and reading the field out of a page requires a
same-origin read. Templates keep the rule consistent per mechanism -- forms
emit ``{{ csrf_field() }}``, and htmx inherits one ``hx-headers`` declared once
on ``<body>`` in base.html, which covers every htmx control in the app
including ones added later.

**Enforced as an app-level dependency**, wired as ``FastAPI(dependencies=…)``
so every router -- present and future -- inherits it. Per-router wiring is how
one router ends up unprotected.

Not covered, deliberately: ``GET /logout``. Forced logout is self-inflicted
denial of service with no data, privilege or tenancy impact, and the dangerous
half of that pair -- login CSRF, which lands an operator inside an
attacker-controlled session -- is closed because ``POST /login`` is always
enforced (see ``_ALWAYS_ENFORCED``). Making logout POST-only would either 405 a
bookmarked URL or leave a GET that reports success without ending the session,
and "looks logged out but isn't" is a worse failure than the one it fixes.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from typing import Any, Mapping, Optional

from fastapi import Request

log = logging.getLogger("central.csrf")

#: Methods RFC 9110 defines as safe -- no state change, so nothing to forge.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Where the token lives inside the signed session cookie.
SESSION_KEY = "csrf_token"

#: The hidden form field emitted by ``csrf_field()``.
FIELD_NAME = "csrf_token"

#: The header htmx sends, declared once on ``<body>`` in base.html.
HEADER_NAME = "X-CSRF-Token"

#: Content types whose body may carry ``FIELD_NAME``. Anything else (JSON,
#: octet-stream) must use the header -- a cross-site form POST cannot produce
#: those content types in the first place, so there is nothing to read there.
_FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")

#: Paths checked even when the session is empty. Only ``POST /login`` qualifies:
#: a browser that has never visited us has no session, and skipping the check
#: there would leave login CSRF open -- an attacker silently signing the victim
#: into an account the attacker controls, so that everything the operator does
#: next happens inside the attacker's tenant. A browser always GETs the form
#: first, which is what issues the token.
_ALWAYS_ENFORCED = frozenset({"/login"})


class CSRFError(Exception):
    """An unsafe request arrived without a valid token.

    Carries a ``reason`` for the audit row and a human sentence for the page the
    operator actually sees -- a bare 403 on a form submit is indistinguishable
    from the app being broken, which is the failure mode this whole change has
    to avoid.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


def issue_token(session: dict) -> str:
    """Return this session's token, minting one on first use.

    Called from the Jinja global, so *rendering any page* is what puts a token
    into a session -- including the login form, and including a session that
    predates this feature. That is what makes the upgrade seamless: an operator
    holding an old cookie gets a token the moment they load the page carrying
    the form they are about to submit.
    """
    token = session.get(SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session[SESSION_KEY] = token
    return token


def rotate_token(session: dict) -> str:
    """Mint a fresh token, discarding any previous one.

    Called on successful login so a token observed before authentication is not
    still valid after it.
    """
    token = secrets.token_urlsafe(32)
    session[SESSION_KEY] = token
    return token


def clear_token(session: dict) -> None:
    """Drop the token. ``session.clear()`` on logout already does this; this
    exists so a caller clearing selectively cannot leave a stale one behind."""
    session.pop(SESSION_KEY, None)


def _tokens_match(supplied: str, expected: str) -> bool:
    # compare_digest refuses non-ASCII str, and the supplied half is hostile
    # input. Encoding both sides makes the comparison total; "replace" cannot
    # collapse two distinct tokens together because the expected value is
    # url-safe base64 and contains no replacement characters.
    return hmac.compare_digest(
        supplied.encode("utf-8", "replace"), expected.encode("utf-8", "replace")
    )


def _field_value(form: Mapping[str, Any]) -> str:
    """The token from a parsed form, ignoring a same-named *file* part.

    ``form.get`` returns an ``UploadFile`` when the client sends the field as a
    file part; treating that as a string would compare a repr.
    """
    value = form.get(FIELD_NAME)
    return value if isinstance(value, str) else ""


async def _supplied_token(request: Request) -> str:
    header = request.headers.get(HEADER_NAME)
    if header:
        return header
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type in _FORM_CONTENT_TYPES:
        # Safe and free: FastAPI parses the body *before* solving dependencies
        # (fastapi/routing.py, body_field handling precedes solve_dependencies),
        # and Starlette caches the parse on the Request, so this is a cache hit
        # on every route that declares Form/File parameters -- including a
        # multi-hundred-megabyte driver upload, which stays spooled to disk and
        # is never buffered here. On a route with no declared body FastAPI never
        # reads it, so this is the only parse and nothing downstream re-reads.
        return _field_value(await request.form())
    return ""


async def csrf_protect(request: Request) -> None:
    """App-level dependency: reject unsafe cookie-authenticated requests
    that do not carry a matching token.

    Raises ``CSRFError``, which ``central.main`` renders as a 403 the operator
    can act on and audits as ``csrf.rejected``.
    """
    if request.method.upper() in SAFE_METHODS:
        return

    # SessionMiddleware always populates scope["session"]; an anonymous caller
    # gets {}. An empty session means no ambient credential, so a forged request
    # has nothing to spend -- the route's own auth 401s it. The one exception is
    # POST /login, which is where a session gets created.
    session: Optional[dict] = request.scope.get("session")
    if not session and request.url.path not in _ALWAYS_ENFORCED:
        return

    expected = (session or {}).get(SESSION_KEY)
    if not isinstance(expected, str) or not expected:
        raise CSRFError(
            "no-token-in-session",
            "This session has no security token yet. Reload the page and try "
            "again -- submitting the form again from the reloaded page will work.",
        )

    supplied = await _supplied_token(request)
    if not supplied:
        raise CSRFError(
            "missing",
            "This request did not carry a security token. Reload the page and "
            "try again.",
        )
    if not _tokens_match(supplied, expected):
        raise CSRFError(
            "mismatch",
            "This page's security token no longer matches your session -- most "
            "often because you signed in again in another tab. Reload the page "
            "and try again.",
        )


def install_jinja_globals(env) -> None:
    """Register ``csrf_token()`` / ``csrf_field()`` on a Jinja environment.

    Both pull the request out of the *template context* rather than taking it as
    an argument, so a form needs exactly ``{{ csrf_field() }}`` and no route has
    to thread anything into its context. Every dashboard render already carries
    ``request`` (Starlette's ``TemplateResponse`` puts it there).
    """
    import jinja2
    from markupsafe import Markup, escape

    @jinja2.pass_context
    def csrf_token(ctx) -> str:
        request = ctx.get("request")
        if request is None:  # a template rendered outside a request (email, tests)
            return ""
        return issue_token(request.scope.setdefault("session", {}))

    @jinja2.pass_context
    def csrf_field(ctx) -> Markup:
        token = csrf_token(ctx)
        return Markup(
            '<input type="hidden" name="{}" value="{}">'.format(
                FIELD_NAME, escape(token)
            )
        )

    env.globals["csrf_token"] = csrf_token
    env.globals["csrf_field"] = csrf_field
