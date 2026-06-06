# Printer Nanny 🖨️

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

Self-hosted, multi-tenant fleet management for printers across an MSP's clients,
sites, and subnets. Lightweight site agents collect printer telemetry over SNMP
(the brand-agnostic **RFC 3805 Printer MIB** — HP, Brother, Canon, Xerox,
Lexmark, Konica, Ricoh, …) and push it to an on-prem central server that tracks
**supply levels, errors, status, page counts, and maintenance**, and raises
alerts via **email** or **FreeScout** tickets.

Everything operational is managed in the web UI — clients, sites, printers,
agent enrollment, alert thresholds, SMTP/FreeScout, and SSO. Only `DATABASE_URL`
and `SECRET_KEY` live in the environment.

## Highlights

- **Brand-agnostic SNMP** — one code path for every vendor via the standard
  Printer MIB; sentinel-safe supply parsing.
- **Multi-tenant, multi-subnet** — `Client → Site → Subnet → Printer`; agents
  own one or more subnets and reach printers over your tunneled network.
- **Push architecture** — agents dial *out* over HTTPS (no inbound ports at
  sites) and pull queued commands on a heartbeat.
- **One-line agent install** — enroll an agent in the UI and it hands you a
  copy-paste command with the key baked in. Subnets/SNMP/intervals are managed
  centrally and fetched at runtime.
- **Alerting** — low supply, errors, offline agents, maintenance due; dedupe +
  auto-resolve; days-to-empty supply forecast. Delivers to email and FreeScout
  with per-alert delivery status.
- **Auto-discovery → approve** — agents sweep subnets; new devices land as
  *pending* for a tech to approve.
- **Pluggable auth** — local login plus optional OIDC/SSO (Entra, Okta, Google,
  Keycloak, …), configured from Settings.

## Quick start — Docker (Postgres, full stack)

```bash
cp .env.example .env          # set SECRET_KEY (DATABASE_URL is preset for Postgres)
docker compose up -d --build
docker compose exec api python -m central.seed   # optional demo data + admin/admin
# open http://localhost:8080   (login: admin / admin)
```

The stack: Postgres + API + worker + dashboard behind Caddy (`:8080`), plus
MailHog (`:8025`) so demo alert email is visible.

## Quick start — local (SQLite, no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m central.seed                 # tables + demo data
uvicorn central.main:app --reload      # http://localhost:8000  (admin / admin)
python -m central.worker.run --once    # evaluate alerts once
```

## Install a site agent

Enroll the agent in the UI (**Agents → enroll**); it shows a ready command:

```bash
curl -fsSL https://CENTRAL/install-agent.sh | sudo bash -s -- \
  --central-url https://CENTRAL --agent-id 12 --api-key pn_xxxxx
```

The agent is a self-contained package (`pip install` from this repo's `agent/`
subdir) and can also run from env vars or Docker. See
[`agent/README.md`](agent/README.md).

## How it fits together

```
 Site agent (pysnmp)  ──HTTPS push──▶  Central API  ──▶  Postgres
   discover/poll       ◀──pull cmds──   (FastAPI)         │
   ◀── config ─────────────────────────                   ▼
                                       Dashboard ◀──── Worker ──▶ Email / FreeScout
                                       (HTMX)          (alerts, maintenance, forecast)
```

- **Central** — FastAPI JSON API + APScheduler worker + HTMX/Jinja dashboard.
- **Agents** — Python + pysnmp, one per site, owning its subnets.
- See [`CLAUDE.md`](CLAUDE.md) for architecture/conventions and
  [`central/snmp.md`](central/snmp.md) for the SNMP OID reference.

## API surface (v1)

| Area        | Endpoint                                                   |
|-------------|------------------------------------------------------------|
| Ingest      | `POST /api/v1/agents/{id}/heartbeat`                        |
| Ingest      | `POST /api/v1/agents/{id}/readings` (batch)                |
| Ingest      | `POST /api/v1/agents/{id}/discovered` (pending devices)    |
| Ingest      | `GET  /api/v1/agents/{id}/config` (subnets/SNMP/intervals) |
| Ingest      | `GET  /api/v1/agents/{id}/targets` · `/commands`           |
| Management  | CRUD `clients`, `sites`, `subnets`, `agents`, `printers`   |
| Management  | `POST /api/v1/printers/{id}/approve` \| `/ignore`          |
| Reporting   | `GET /api/v1/reports/fleet`, `/supplies/low`, `/errors`    |

Agents authenticate with `Authorization: Bearer <agent-api-key>`.

## Security notes

- Set a strong `SECRET_KEY` (signs dashboard sessions). Agent API keys are stored
  hashed (SHA-256) and shown only once at enrollment; rotate from the UI.
- Put the central server behind TLS (the bundled Caddy config provisions certs
  when you set a hostname). Agents verify TLS by default.
- `/install-agent.sh` is public by design (like `get.docker.com`); the secret is
  the per-agent key in the install command, never in the script.

## Development

```bash
pip install -e ".[dev,agent]"
pytest          # test suite
ruff check .    # lint
```

## License

[Apache License 2.0](LICENSE) — © 2026 Stephen Warren.
