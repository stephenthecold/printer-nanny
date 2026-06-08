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

## Quick start — Docker (one-liner)

```bash
curl -fsSL https://raw.githubusercontent.com/stephenthecold/printer-nanny/main/deploy/install.sh | bash
```

The installer clones the repo into `./printer-nanny`, generates a `.env` with
a strong `SECRET_KEY`, and walks you through how you want to terminate TLS:

| Mode | When to pick | Result |
|---|---|---|
| `external` (default) | You already run Caddy / Nginx / Traefik. | API exposed on `:8000` for your existing proxy. |
| `bundled` | You want auto-TLS and have a public hostname. | Bundled Caddy on `:80`+`:443` with Let's Encrypt. |
| `none` | LAN testing, no TLS needed. | Bundled Caddy on a plain HTTP port you pick. |

Skip the prompts with flags for unattended installs:

```bash
# Bundled Caddy + Let's Encrypt
curl -fsSL .../install.sh | bash -s -- --proxy bundled \
    --hostname printers.msp.example.com --acme-email ops@msp.example.com
# Or plain HTTP for testing on a LAN
curl -fsSL .../install.sh | bash -s -- --proxy none --http-port 8536
```

Log in at the URL it prints with **`admin` / `admin`** — change that password
immediately under `/manage`. The API runs migrations and bootstraps the admin
user on every container start (idempotent), so a fresh DB is usable on first
boot without touching a shell.

### Updating

```bash
bash deploy/install.sh --update
```

Pulls the latest code, rebuilds the images (`--pull` so the Python / Postgres
base layers refresh), and recreates only the changed containers. Your `.env`
and Postgres data volume are preserved. Migrations run automatically as part
of the api service's startup chain.

### Demo data (destructive)

```bash
bash deploy/install.sh --demo
```

DROPS all tables and reseeds with fake clients/printers. Asks for `yes`
confirmation before doing anything. Don't run this against a real instance.

<details>
<summary>Manual steps (what the installer does under the hood)</summary>

```bash
git clone https://github.com/stephenthecold/printer-nanny.git
cd printer-nanny
echo "SECRET_KEY=$(openssl rand -base64 48)" > .env
docker compose up -d --build                  # API on :8000, BYO proxy
# or, include the bundled Caddy on :80 + :443:
docker compose --profile caddy up -d --build
# optional: drop & re-seed with demo clients/printers
docker compose exec api python -m central.seed
```

For the bundled Caddy path you'll also need a `deploy/Caddyfile` — copy
`deploy/Caddyfile.template` and replace `__SITE__` with your hostname (or
`:8080` for HTTP-only) and `__GLOBAL_OPTIONS__` with `email you@…` or
`auto_https off`.
</details>

### Point your own reverse proxy at it

If you picked `external` mode, the API listens on
`http://<docker-host>:${API_PORT:-8000}` with no TLS. Minimal Caddyfile:

```Caddyfile
printers.msp.example.com {
    reverse_proxy localhost:8000
}
```

Nginx equivalent:

```nginx
server {
    server_name printers.msp.example.com;
    location / { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
}
```

## Quick start — local (SQLite, no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m central.seed                 # tables + demo data (drops & recreates all tables)
uvicorn central.main:app --reload      # http://localhost:8000  (admin / admin)
python -m central.worker.run --once    # evaluate alerts once
```

## Modern-auth email (OAuth SMTP)

Out of the box, the email channel speaks plain SMTP AUTH (great for MailHog,
internal relays, or providers that still accept app passwords). Gmail and
Microsoft 365 have been deprecating that path, so the **Settings → Email
(SMTP)** section also supports OAuth2 / XOAUTH2:

1. Register an OAuth app:
   - **Gmail** — Google Cloud Console → APIs & Services → Credentials → "OAuth
     client ID" (Type: Web). Scope: `https://mail.google.com/`.
   - **Microsoft 365** — Entra ID → App registrations → New registration
     (Web). API permissions → `offline_access` + `https://outlook.office.com/SMTP.Send`
     (delegated). Grant admin consent. Your tenant must have SMTP AUTH enabled
     for the mailbox.
2. Copy the redirect URI shown on the Settings page (it's
   `https://CENTRAL/settings/smtp-oauth/callback`) into the cloud console's
   allowed redirects.
3. Back on Settings → Email: set `smtp.auth_type` to `oauth_google` or
   `oauth_microsoft`, fill in `oauth_client_id` + `oauth_client_secret`
   (+ tenant for Microsoft), Save.
4. Click **Connect Gmail** / **Connect Microsoft 365**. After consent, the
   refresh token is stored encrypted at rest and the channel refreshes
   access tokens on demand. Use the **Send test notification** button to
   confirm.

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
| Reporting   | `GET /api/v1/reports/fleet`, `/supplies/low`, `/errors`, `/maintenance/due` |

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
