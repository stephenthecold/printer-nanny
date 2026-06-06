# Printer Nanny 🖨️

Self-hosted, multi-tenant fleet management for printers across an MSP's clients,
sites, and subnets. Lightweight site agents collect printer telemetry over SNMP
(the brand-agnostic RFC 3805 Printer MIB) and push it to an on-prem central
server that tracks **supply levels, errors, status, page counts, and
maintenance** — and raises alerts via email, FreeScout tickets, or Teams.

> **Milestone 1 — central server:** data model, JSON API, background worker +
> alerting, and an HTMX dashboard, all demoable with seeded data.
> **Milestone 2 — site agent** (`printer-nanny-agent`): SNMP discovery/polling
> over pysnmp that pushes to central and pulls commands. Self-contained; install
> with a one-liner the UI generates (key baked in) — see
> [`agent/README.md`](agent/README.md).
> **Milestone 3 — operator UI**: clients/sites/printers CRUD, agent enrollment +
> one-line installer, DB-backed Settings (no env-var sprawl), and pluggable
> OIDC/SSO.

## Quick start (local, SQLite — no Docker needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Create tables and load realistic demo data (clients, sites, printers, readings)
python -m central.seed

# Start the API + dashboard
uvicorn central.main:app --reload
# open http://localhost:8000  (login: admin / admin)

# In another shell, run the worker once to evaluate alerts:
python -m central.worker.run --once
```

## Full stack (Docker Compose — Postgres)

```bash
cp .env.example .env          # set SECRET_KEY, FreeScout/SMTP creds, etc.
docker compose up --build
# api + dashboard via Caddy on http://localhost:8080
```

## How it fits together

```
 Site agent (pysnmp)  ──HTTPS push──▶  Central API  ──▶  Postgres
   discover/poll       ◀──pull cmds──   (FastAPI)         │
                                          │               ▼
                                       Dashboard ◀──── Worker ──▶ Email / FreeScout / Teams
                                       (HTMX)          (alerts, maintenance, forecast)
```

See [`CLAUDE.md`](CLAUDE.md) for architecture and conventions, and
[`central/snmp.md`](central/snmp.md) for the SNMP OID reference the agent uses.

## API surface (v1)

| Area        | Endpoint                                              |
|-------------|-------------------------------------------------------|
| Ingest      | `POST /api/v1/agents/{id}/heartbeat`                   |
| Ingest      | `POST /api/v1/agents/{id}/readings` (batch)           |
| Ingest      | `POST /api/v1/agents/{id}/discovered` (pending devices)|
| Ingest      | `GET  /api/v1/agents/{id}/commands` (pull queue)       |
| Management  | CRUD `clients`, `sites`, `subnets`, `printers`         |
| Management  | `POST /api/v1/printers/{id}/approve` \| `/ignore`      |
| Management  | maintenance schedules & records, alert rules, channels |
| Reporting   | `GET /api/v1/reports/fleet`, `/supplies/low`, `/errors`|

Agents authenticate with `Authorization: Bearer <agent-api-key>`.
