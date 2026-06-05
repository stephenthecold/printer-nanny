# Printer Nanny

Self-hosted fleet management for printers across MSP clients/sites. Monitors
supply levels, errors, status, and page counts over SNMP; tracks maintenance;
alerts and opens FreeScout tickets. Multi-tenant (client → site → subnet →
printer), multi-subnet, agent-collected.

## Architecture
- **Central server** (on-prem, Docker Compose): FastAPI JSON API + APScheduler
  worker + HTMX/Jinja dashboard, backed by PostgreSQL (SQLite for local dev/tests).
- **Site agents** (Python, pysnmp): one per site, own one or more subnets. Poll
  printers locally, **push** readings to central over HTTPS, **pull** queued
  commands on heartbeat. No inbound ports needed at sites. (Milestone 2.)
- Data flows agent → `/api/v1/agents/{id}/...` → DB → worker (alerts) →
  channels (email / FreeScout / Teams).

## Layout
- `central/` — FastAPI app, models, worker, dashboard, notification channels.
  - `api/` — JSON API routers: `ingest`, `management`, `reporting`.
  - `worker/` — APScheduler jobs (heartbeat, alerts, maintenance, forecast).
  - `channels/` — pluggable `NotificationChannel` impls (email, freescout, teams).
  - `dashboard/` — HTMX/Jinja: `routes` (overview/drill-down/approvals/alerts/login),
    `manage` (clients/sites/printers/agents CRUD + enrollment), `settings_routes`.
  - `runtime.py` — spec-driven DB-backed settings (the Settings page); env only
    supplies defaults. `auth_oidc.py` — pluggable OIDC/SSO login.
  - `snmp_parse.py` — brand-agnostic SNMP supply/level parsing (shared w/ agent).
  - `snmp.md` — Printer-MIB OID reference.
- `migrations/` — Alembic environment + versions.
- `deploy/` — Caddyfile, sample systemd unit for the agent.
- `tests/` — pytest suite.
- `agent/` — standalone agent package (Milestone 2, placeholder for now).

## Conventions
- Python 3.12 in Docker; code stays 3.9-compatible (`from __future__ import
  annotations`) so it runs on the local system Python too.
- Sync SQLAlchemy 2.0 (`Mapped[]` style) + Alembic. Sessions via
  `central.db.SessionLocal` / the `get_db` FastAPI dependency.
- API is versioned under `/api/v1`. Agents authenticate with a per-agent API key
  (`Authorization: Bearer <key>`, hashed at rest). Dashboard users use signed
  sessions + roles (`admin`/`tech`/`client_readonly`).
- Time-series lives in the `readings` table — append-only, indexed by
  `(printer_id, ts)`. On Postgres a BRIN index on `ts` keeps range scans cheap
  (migration `0002`); monthly range-partitioning is a documented future step,
  not needed at the 50–500 printer scale.
- SNMP is brand-agnostic via RFC 3805 Printer MIB + Host Resources MIB. Handle
  sentinel supply levels (-1 other / -2 unknown / -3 some-remaining) in
  `central/snmp_parse.py`. OIDs documented in `central/snmp.md`.
- Operational config (SMTP, FreeScout, alert thresholds, polling intervals, SNMP
  defaults, SSO) lives in the DB via `central/runtime.py` and is edited in the
  Settings UI. Only `DATABASE_URL` + `SECRET_KEY` come from env (`central/config.py`,
  which also supplies first-run defaults for the settings specs).
- Agents are managed entirely in the UI: enroll (key shown once) and assign
  subnets/SNMP under Agents; the agent fetches subnets, SNMP, and intervals from
  `GET /api/v1/agents/{id}/config`, so its local file holds only URL + API key.
- Auth is pluggable: local username/password always works; OIDC/SSO turns on from
  Settings (`auth_oidc.py`), matching/provisioning users by email.

## Dev
- `pip install -e ".[dev]"` (add `postgres` / `agent` extras as needed).
- `python -m central.seed` — create tables + load demo data (SQLite by default,
  no live agent needed).
- `uvicorn central.main:app --reload` — API + dashboard at http://localhost:8000.
- `python -m central.worker.run` — run the background worker loop.
- `docker compose up` — full stack (Postgres + api + worker + dashboard + Caddy).
- `alembic upgrade head` — apply migrations (Postgres).
- `pytest` — run tests.

## Status
Milestone 1 (DONE): central server (API, model, dashboard, seeded data, alerting).
Milestone 2 (DONE): `printer-nanny-agent` — SNMP discovery/poll over pysnmp,
pushes to central, pulls commands. See `agent/README.md`. The SNMP layer is
behind a swappable `SnmpBackend` so poller/discovery unit-test with a fake;
`scripts/e2e_agent_demo.py` drives a full agent cycle against a live central.
