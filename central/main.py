"""FastAPI application: JSON API (v1) + HTMX dashboard, sharing one DB."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from central import auth_oauth_smtp, auth_oidc
from central.api import (
    enrollment,
    exports,
    ingest,
    management,
    reporting,
    scim,
    workstations,
)
from central.config import settings
from central.csrf import CSRFError, csrf_protect
from central.dashboard import (
    events_routes,
    backup_routes,
    billing as billing_routes,
    definitions,
    installer,
    machines,
    manage,
    people,
    remote as remote_routes,
    routes as dashboard,
    settings_routes,
)
from central.dashboard.templating import render_csrf_error
from central.db import create_all, get_db
from central.health import database_ok, worker_health
from central.logging_config import configure_logging
from central.middleware import ForcePasswordChangeMiddleware, PeerAddressMiddleware

# `uvicorn central.main:app` gives the api container no entrypoint of ours, so
# importing this module is the only moment we get before it serves. Until this
# call existed the worker configured logging in its main() and the api
# configured it NOWHERE: every log.info() in central/ was dropped on the floor
# there, and warnings came out through logging.lastResort with no timestamp,
# level or logger name. Safe at import time -- uvicorn applies its own
# dictConfig in Config.__init__, i.e. before load() imports this module, and
# that config names only the uvicorn* loggers. See central/logging_config.py.
configure_logging()

log = logging.getLogger("central.main")

app = FastAPI(title="Printer Nanny", version="0.31.0")

# MIDDLEWARE ORDER IS LOAD-BEARING. add_middleware inserts at index 0 and the
# stack is wrapped in reverse, so the LAST one added is the OUTERMOST. Reading
# the four below from the bottom up gives the order a request meets them.
#
# ``dependencies`` here is the CSRF gate, and it is declared on the app rather
# than on each router on purpose: APIRouter.include_router prepends the app
# router's dependencies to every route it takes in, so a router added later
# inherits the check instead of quietly shipping without it. See central/csrf.py
# for what it does and does not cover -- in particular why "carries a session
# cookie" is the trigger rather than a path allowlist.
app = FastAPI(
    title="Printer Nanny",
    version="0.31.0",
    dependencies=[Depends(csrf_protect)],
)
# Honor X-Forwarded-Proto/For from the reverse proxy so request.base_url returns
# https:// when Caddy/Nginx terminates TLS in front of us. Without this, the
# agent install command on /manage/agents leaks http://… to operators behind
# their own TLS proxy. Trusts headers from any source — we already require the
# proxy to be a trusted hop (it's the same docker network or LAN). That trust is
# fine for display and for the audit trail and is NOT fine for a rate limiter,
# which is why PeerAddressMiddleware below captures the peer before this runs;
# see central/net.py.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

#: Named explicitly rather than left to Starlette's default so
#: _NoSessionRefreshOnPoll below can match on it. The two must agree; a drift
#: would make that middleware silently stop doing anything.
SESSION_COOKIE = "session"

# Inside SessionMiddleware (added next), which is what puts scope["session"] there.
app.add_middleware(ForcePasswordChangeMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie=SESSION_COOKIE,
    max_age=60 * 60 * 12,
    https_only=settings.secure_cookies,  # Secure flag in production (TLS at the proxy)
    # Defence in depth, NOT the CSRF defence -- that is central/csrf.py. "lax"
    # compares registrable domains, so a sibling host on the same domain is
    # same-site and gets this cookie attached to its forged requests; and it
    # permits top-level GET navigation, which is why a disclosure behind a GET
    # (the old /admin/backup/download) was reachable despite it.
    same_site="lax",
)
# Outermost: must see scope["client"] before ProxyHeadersMiddleware rewrites it.
app.add_middleware(PeerAddressMiddleware)


class _NoSessionRefreshOnPoll:
    """Stop the dashboard's auto-refresh poll from renewing the session cookie.

    SessionMiddleware re-signs and re-sends the cookie on EVERY response with a
    non-empty session, and the signature carries a fresh timestamp, so the 12h
    expiry is a rolling one measured from the last request. That is the right
    behaviour for a human clicking around. It is the wrong behaviour for a
    timer: without this, one dashboard tab left open on an unattended NOC
    workstation would hold its session open indefinitely, because the page
    would keep renewing its own credential with nobody present. The 12h cap
    would become no cap at all -- a security property quietly removed as a side
    effect of a convenience feature.

    So for the poll path only, the Set-Cookie the session middleware appended is
    dropped on the way out. The request still authenticates (the browser's
    existing cookie is untouched and stays valid until its own Max-Age), it just
    does not extend anything: the operator's clock keeps running from their last
    real interaction, and when it lapses the poll gets a 286 and stops.

    Written as raw ASGI rather than @app.middleware("http") on purpose --
    BaseHTTPMiddleware wraps the whole response cycle and interferes with
    streaming responses, and /admin/backup streams a database dump through it.
    This only inspects one header on one path and never touches the body.
    """

    def __init__(self, app, paths) -> None:
        self.app = app
        self.paths = frozenset(paths)
        self._prefix = f"{SESSION_COOKIE}=".encode("latin-1")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") not in self.paths:
            await self.app(scope, receive, send)
            return

        async def send_no_session_cookie(message):
            if message.get("type") == "http.response.start":
                # Only OUR session cookie is dropped; anything else a response
                # legitimately sets is passed through untouched.
                message = dict(message)
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if not (key.lower() == b"set-cookie" and value.startswith(self._prefix))
                ]
            await send(message)

        await self.app(scope, receive, send_no_session_cookie)


# Added last, so it is the OUTERMOST middleware and therefore sees (and can
# remove) the header SessionMiddleware appended further in.
app.add_middleware(_NoSessionRefreshOnPoll, paths=[dashboard.FRESHNESS_PATH])

# JSON API
# Registered before `ingest` so /api/v1/agents/register is matched by its own
# literal route rather than by ingest's /api/v1/agents/{agent_id} prefix, which
# would otherwise try to parse "register" as an agent id.
app.include_router(enrollment.router)
app.include_router(ingest.router)
app.include_router(workstations.router)
app.include_router(management.router)
app.include_router(reporting.router)
app.include_router(exports.router)
# SCIM 2.0 user provisioning / deprovisioning (gated behind scim.enabled).
app.include_router(scim.router)
# Dashboard (HTML) + management + settings + SSO
app.include_router(dashboard.router)
app.include_router(manage.router)
app.include_router(people.router)
app.include_router(machines.router)
app.include_router(billing_routes.router)
app.include_router(events_routes.router)
app.include_router(definitions.router)
app.include_router(remote_routes.router)
app.include_router(settings_routes.router)
app.include_router(backup_routes.router)
app.include_router(auth_oidc.router)
app.include_router(auth_oauth_smtp.router)
app.include_router(installer.router)


@app.exception_handler(CSRFError)
async def _csrf_rejected(request: Request, exc: CSRFError):
    """Turn a refused request into something the operator can act on.

    A bare 403 on a form submit is the failure mode this whole change had to
    avoid: to an operator it is indistinguishable from the app being broken, and
    to an htmx-driven control it is *invisible* -- htmx does not swap a non-2xx
    response, so the button simply does nothing. So both shapes get a stated
    reason: a rendered page for a form post, and for htmx a plain-text body that
    base.html's ``htmx:responseError`` listener puts on screen.

    Audited as ``csrf.rejected``. It is the only signal that someone is aiming
    cross-site requests at an operator's browser, and it is written on its own
    session because the request's DB dependency is already torn down by the time
    an exception handler runs. Never the tokens -- only the path and which of
    the three ways it failed.
    """
    try:
        from central.audit import record_anonymous
        from central.db import SessionLocal

        with SessionLocal() as audit_db:
            record_anonymous(
                audit_db, request, "", "csrf.rejected",
                target=f"{request.method} {request.url.path}"[:300],
                detail=exc.reason,
            )
            audit_db.commit()
    except Exception:  # noqa: BLE001 - a refusal must not depend on the audit landing
        log.exception("failed to audit csrf rejection")

    if request.headers.get("HX-Request", "").lower() == "true":
        return PlainTextResponse(
            exc.message,
            status_code=403,
            # htmx would not swap a 403 anyway; saying so keeps a future
            # hx-swap-oob or global error-swap config from pasting this in.
            headers={"HX-Reswap": "none"},
        )
    return render_csrf_error(request, exc.message)


class _RevalidatingStatic(StaticFiles):
    """StaticFiles that forces a conditional request on every asset load.

    Starlette sends ETag/Last-Modified but no Cache-Control, which leaves
    browsers free to apply *heuristic* caching -- they invent a freshness
    lifetime from Last-Modified and serve the old file without asking. Across an
    upgrade that means a browser pairing the new HTML with the previous
    stylesheet, i.e. a subtly broken dashboard that a reload does not fix and
    that the operator cannot diagnose.

    ``no-cache`` does not mean "do not store": the browser still caches, it just
    has to revalidate, so the steady state is a 304 with no body. On a LAN-hosted
    dashboard serving two small files that is the right trade -- correctness
    after an upgrade, at the cost of one conditional request per asset.

    Hooked at ``get_response`` rather than ``file_response`` so the header rides
    on the 304 as well as the 200; a 304 updates the stored response's headers,
    so skipping it would let a stale directive outlive the file it applies to.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Vendored Tailwind + htmx. These were loaded from public CDNs until it turned
# out that the deployment shape this project targets -- MSP management VLANs --
# frequently has no outbound internet, which rendered the dashboard unstyled with
# every htmx-driven control dead. Serving them from the image makes the UI a
# function of the deployment alone. Regenerate with scripts/build-assets.sh.
# Unauthenticated by design: login.html needs the stylesheet before a session
# exists, and the directory holds nothing but those two build artifacts.
app.mount(
    "/static",
    _RevalidatingStatic(directory=str(Path(__file__).parent / "static")),
    name="static",
)


@app.get("/healthz", tags=["meta"])
def healthz():
    """LIVENESS only: this process is up and serving HTTP.

    Deliberately touches nothing -- no database, no worker state -- because it is
    polled at container/proxy frequency and because "restart me" is the only
    sensible response to it failing. A 200 here says nothing about whether the
    deployment is *working*; that is ``/readyz``.
    """
    return {"status": "ok", "version": app.version}


@app.get("/readyz", tags=["meta"])
def readyz(worker: str = "check", db: Session = Depends(get_db)):
    """READINESS: the database answers and the background worker is not stalled.

    503 when either check fails, 200 otherwise. This is the endpoint to point
    uptime monitoring at -- ``/healthz`` cannot detect the failure this exists
    for (a dead worker leaves the dashboard showing a green fleet over frozen
    data, because ``mark_offline_agents`` is itself a worker job).

    A worker that has *never* stamped (fresh install, first cycle not finished)
    is reported as ``never_ran`` and does NOT fail the probe -- a new deployment
    must not come up 503. That case is covered by the dashboard banner and by the
    worker container's own healthcheck, which does fail on it after its
    start_period. Likewise a missing table mid-migration reads ``unknown`` and
    passes, so a migration window is not an outage.

    ``?worker=skip`` narrows this to the database check. Used by the api
    container's healthcheck so a stalled *worker* does not mark the *api*
    container unhealthy and send an operator debugging the wrong process. Any
    other value keeps the full check, so a typo fails closed (stricter).

    The response body is deliberately terse: a status, which checks passed, and
    the stale job names -- enough to act on, with no exception text, driver
    message, DSN, hostname or credential. This route is typically unauthenticated
    behind the reverse proxy.
    """
    db_ok = database_ok(db)
    checks = {"database": "ok" if db_ok else "error"}

    if not db_ok:
        # Nothing to read the stamps from, so the worker's state is unknowable.
        checks["worker"] = "unknown"
        return JSONResponse({"status": "degraded", "checks": checks}, status_code=503)

    if worker == "skip":
        checks["worker"] = "skipped"
        return {"status": "ready", "checks": checks}

    health = worker_health(db)
    checks["worker"] = health["state"]
    if health["state"] == "stale":
        # Deliberately does NOT name the wedged jobs. The Caddyfile reverse-proxies
        # every path, so this endpoint is reachable unauthenticated from the
        # internet, and "which job is wedged" tells a caller that fleet monitoring
        # is blind right now -- useful recon, no use to a container probe (which
        # reads only the status code). Signed-in operators get the job names from
        # the dashboard banner; the worker's own probe reads the table directly.
        return JSONResponse(
            {"status": "degraded", "checks": checks},
            status_code=503,
        )
    return {"status": "ready", "checks": checks}


@app.on_event("startup")
def _startup() -> None:
    # Refuse to boot on a SECRET_KEY that is published in this repository —
    # on ANY database backend. See central/config.py.
    settings.assert_secure()
    log.info(
        "printer nanny api %s starting (secret key: %s)",
        app.version, settings.secret_key_source,
    )
    # On SQLite (local dev) create tables automatically. On Postgres, migrations own
    # the schema, but create_all is a harmless no-op if they've already run.
    if settings.is_sqlite:
        create_all()
    # One-shot lazy migration: encrypt any plaintext secret rows left over
    # from before encryption-at-rest shipped. Idempotent; guarded so a stack
    # mid-migration (app_settings table not created yet) doesn't fail boot.
    try:
        from sqlalchemy import inspect as sa_inspect

        from central import models as m
        from central.db import SessionLocal
        from central.runtime import encrypt_existing_settings

        with SessionLocal() as db:
            if sa_inspect(db.get_bind()).has_table(m.AppSetting.__tablename__):
                updated = encrypt_existing_settings(db)
                if updated:
                    log.info("encrypted %d legacy plaintext secret setting(s)", updated)
    except Exception:  # noqa: BLE001 - never block boot on the sweep
        log.exception("secret-encryption sweep failed")
