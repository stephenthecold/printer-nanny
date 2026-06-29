"""FastAPI application: JSON API (v1) + HTMX dashboard, sharing one DB."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from central import auth_oauth_smtp, auth_oidc
from central.api import exports, ingest, management, reporting, scim
from central.config import settings
from central.dashboard import backup_routes, installer, manage, routes as dashboard, settings_routes
from central.db import create_all

app = FastAPI(title="Printer Nanny", version="0.5.0")
# Honor X-Forwarded-Proto/For from the reverse proxy so request.base_url returns
# https:// when Caddy/Nginx terminates TLS in front of us. Without this, the
# agent install command on /manage/agents leaks http://… to operators behind
# their own TLS proxy. Trusts headers from any source — we already require the
# proxy to be a trusted hop (it's the same docker network or LAN).
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=60 * 60 * 12,
    https_only=settings.secure_cookies,  # Secure flag in production (TLS at the proxy)
    same_site="lax",  # mitigates cross-site POST/CSRF on the dashboard
)

# JSON API
app.include_router(ingest.router)
app.include_router(management.router)
app.include_router(reporting.router)
app.include_router(exports.router)
# SCIM 2.0 user provisioning / deprovisioning (gated behind scim.enabled).
app.include_router(scim.router)
# Dashboard (HTML) + management + settings + SSO
app.include_router(dashboard.router)
app.include_router(manage.router)
app.include_router(settings_routes.router)
app.include_router(backup_routes.router)
app.include_router(auth_oidc.router)
app.include_router(auth_oauth_smtp.router)
app.include_router(installer.router)


@app.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok", "version": app.version}


@app.on_event("startup")
def _startup() -> None:
    import logging

    # Refuse to boot a production deployment with a default/blank SECRET_KEY.
    settings.assert_secure()
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
                    logging.getLogger("central").info(
                        "encrypted %d legacy plaintext secret setting(s)", updated
                    )
    except Exception:  # noqa: BLE001 - never block boot on the sweep
        logging.getLogger("central").exception("secret-encryption sweep failed")
