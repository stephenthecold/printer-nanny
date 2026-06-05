"""FastAPI application: JSON API (v1) + HTMX dashboard, sharing one DB."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from central.api import ingest, management, reporting
from central.config import settings
from central.dashboard import routes as dashboard
from central.db import create_all

app = FastAPI(title="Printer Nanny", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=60 * 60 * 12)

# JSON API
app.include_router(ingest.router)
app.include_router(management.router)
app.include_router(reporting.router)
# Dashboard (HTML)
app.include_router(dashboard.router)


@app.get("/healthz", tags=["meta"])
def healthz():
    return {"status": "ok", "version": app.version}


@app.on_event("startup")
def _startup() -> None:
    # On SQLite (local dev) create tables automatically. On Postgres, migrations own
    # the schema, but create_all is a harmless no-op if they've already run.
    if settings.is_sqlite:
        create_all()
