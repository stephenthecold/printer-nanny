# Printer Nanny 🖨️

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

Self-hosted, multi-tenant fleet management for printers across an MSP's clients,
sites, and subnets. Lightweight site agents collect printer telemetry over SNMP
and push it to an on-prem central server that tracks **supply levels, errors,
status, page counts, and maintenance**, forecasts **days-until-order** per
printer, and delivers alerts via **email, Slack, Microsoft Teams, FreeScout
tickets, or a generic webhook** (PagerDuty, ConnectWise, HaloPSA, Zapier, …).

Everything operational is managed in the web UI — clients, sites, printers,
agent enrollment, alert thresholds, channels, scheduled reports, maintenance
schedules, branding, and SSO. Only `DATABASE_URL` and `SECRET_KEY` live in the
environment.

## Highlights

- **Brand coverage where it counts.** Brother (the painful one) reads exact
  toner percentages over read-only SNMP from the private MIB — the same path
  BRAdmin Professional uses — and surfaces belt / fuser / laser / PF-kit life
  too. HP, Lexmark, Xerox, Kyocera, Canon, Ricoh, and Konica Minolta get
  brand-tagged with their front-panel status text surfaced as events.
- **Multi-tenant, multi-subnet** — `Client → Site → Subnet → Printer`. One
  agent can collect for several client sites bridged at HQ; each subnet has
  its own SNMP creds (community **or** SNMPv3 USM).
- **Days-until-order forecasts** — per printer, refill-aware consumption-slope
  extrapolation. Builds automatically over ~3 days of polling history.
- **Friendly names** — name printers and subnets so alerts read
  `Low black on Front Desk @ 10.0.0.10`, not a model number and IP.
- **Encrypted credentials at rest** — Fernet, `SECRET_KEY`-derived, lazy
  migration. Database dump alone no longer exposes operator credentials or
  SNMPv3 USM passwords.
- **Audit trail** — every login (including failures), settings change (key
  names only, never values), CRUD, approvals, agent updates, and portal
  reports are recorded with user, IP, and timestamp.
- **Customer portal** — `client_readonly` users land on a trimmed view of just
  their fleet with a "Report a problem" button that opens a FreeScout ticket.
- **Scheduled reports** — weekly fleet-summary email, monthly billing CSV as
  an attachment. Marker-gated, restart-safe.
- **One-line agent install + UI self-update** — install with a copy-paste
  command; future updates roll out by clicking *Update* (or *Update all*) on
  `/manage/agents`. The version timestamp on each card confirms the install.
- **Push architecture** — agents dial *out* over HTTPS. No inbound ports at
  customer sites; subnets/SNMP/intervals managed centrally and fetched at
  runtime.
- **Hands-off discovery** — SNMP sweep across configured subnets, plus
  optional mDNS / Bonjour (`agent[mdns]` extras) on the agent's local subnet.
  New devices land as *pending* for a tech to approve; the Approvals nav
  entry only appears when there's actually something pending.
- **DB backup & restore from the UI** — Postgres (`pg_dump --format=custom` /
  `pg_restore --clean`) or SQLite (streamed copy). Restore requires typing
  `RESTORE` to enable.
- **Provider diagnostics** — when a printer's data looks wrong, the printer
  detail page shows which vendor providers ran, whether they succeeded, and
  what they changed (with the actual private-MIB source noted). No more
  spelunking agent logs to debug a single device.
- **Pluggable auth** — local login plus OIDC/SSO (Entra, Okta, Google,
  Keycloak, Authentik, …). MFA via your IdP.

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
immediately under `/account`. The API runs migrations and bootstraps the admin
user on every container start (idempotent), so a fresh DB is usable on first
boot without touching a shell.

**Recommended**: also add `postgresql-client` to your api container image so
`pg_dump` / `pg_restore` are on PATH for the Backup page.

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

## Security: keep agent ↔ central traffic on TLS

Agents push readings and pull commands over the public API on every heartbeat,
so **always front the central server with HTTPS** — either via the bundled
Caddy + Let's Encrypt (`--proxy bundled` during install) or your own reverse
proxy with a real cert.

- The agent installer call should use the **HTTPS** URL:
  ```bash
  curl -fsSL https://printers.msp.example.com/install-agent.sh | sudo bash -s -- \
      --central-url https://printers.msp.example.com --agent-id 12 --api-key pn_xxxxx
  ```
- The agent's `--verify-tls true` default rejects self-signed / invalid certs
  unless explicitly turned off. **Don't pass `--verify-tls false`** unless
  you're testing against a local dev cert — the API key is the only secret
  protecting the ingest endpoint, and a downgrade to plain HTTP leaks it.
- The central app honors `X-Forwarded-Proto` from the reverse proxy via
  `ProxyHeadersMiddleware`, so the install command rendered on
  `/manage/agents` automatically uses `https://` when you access the
  dashboard via TLS.
- For extra safety pin **Settings → Branding → Public URL** to your
  canonical HTTPS hostname (e.g. `https://printers.msp.example.com`). The
  Agents page then renders that hostname regardless of how the dashboard
  itself was reached.
- **Stored credentials are Fernet-encrypted at rest** (SMTP password, OAuth
  tokens, FreeScout API key, Slack/Teams/webhook URLs, OIDC client secret,
  SNMPv3 USM passwords). Rotating `SECRET_KEY` makes them unreadable — the
  affected settings tabs and the SNMPv3 panel say so; you'd re-enter the
  secrets after a rotation.

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
Microsoft 365 have been deprecating that path, so the **Settings → Notifications
→ Email (SMTP)** section also supports OAuth2 / XOAUTH2:

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
3. Back on Settings → Notifications: set `smtp.auth_type` to `oauth_google` or
   `oauth_microsoft`, fill in `oauth_client_id` + `oauth_client_secret`
   (+ tenant for Microsoft), Save.
4. Click **Connect Gmail** / **Connect Microsoft 365**. After consent, the
   refresh token is stored encrypted at rest and the channel refreshes
   access tokens on demand. Use the **Send test notification** button to
   confirm.

## Other channels (Slack / Teams / generic webhook / FreeScout)

Each lives in its own card under **Settings → Notifications**. The generic
webhook channel is a JSON POST to any URL with a configurable auth header,
covering PagerDuty Events v2, ConnectWise REST, HaloPSA, Autotask, Zapier /
Make, or any in-house tool. Each channel has a min-severity filter.

The **Send test notification** button fires through every enabled channel
and reports per-channel results.

## Scheduled reports

**Settings → Notifications → Reports** (also Alerts & Reports tab on Settings):

- **Weekly fleet summary email** — fleet totals, per-client rollup, low
  supplies with printer locations.
- **Monthly billing CSV** — one row per approved printer with page counts;
  ready for billing import.

Marker-gated: a worker that was down at send time catches up on its next
cycle that day; a failed delivery is retried instead of silently skipping a
week.

## Install a site agent

Enroll the agent in the UI (**Agents → Enroll**); it shows a ready command:

```bash
curl -fsSL https://CENTRAL/install-agent.sh | sudo bash -s -- \
  --central-url https://CENTRAL --agent-id 12 --api-key pn_xxxxx
```

The agent is a self-contained package (`pip install` from this repo's `agent/`
subdir) and can also run from env vars or Docker. See
[`agent/README.md`](agent/README.md).

After enrollment, manage everything from the **Agents** page:

- Assign subnets (incl. cross-client for the HQ-multi-tenant pattern), pick
  community **or** SNMPv3 USM credentials per subnet, set a bind-IP for
  overlapping RFC 1918 ranges.
- Force a poll cycle, a discovery sweep, or a self-update.
- Discovery status (last scan / found / new / pending) lives on each subnet
  row — there's no separate Discovery page.
- Each agent's version badge ends with the install timestamp; when you click
  *Update*, the suffix changes on the next heartbeat. That's your rollout
  confirmation.

## How it fits together

```
 Site agent (pysnmp + providers) ──HTTPS push──▶  Central API  ──▶  Postgres
   SNMP + Brother PJL/maint blob    ◀──pull cmds── (FastAPI)         │
   ◀── config (subnets/SNMP) ──────                                  ▼
                                                Dashboard ◀──── Worker ──▶ channels
                                                (HTMX)         (alerts, maintenance,
                                                                forecasts, reports)
```

- **Central** — FastAPI JSON API + APScheduler worker + HTMX/Jinja dashboard.
- **Agents** — Python + pysnmp, one per site, owning its subnets.
- See [`CLAUDE.md`](CLAUDE.md) for architecture/conventions and
  [`central/snmp.md`](central/snmp.md) for the SNMP OID reference.

## API surface (v1)

| Area        | Endpoint                                                   |
|-------------|------------------------------------------------------------|
| Ingest      | `POST /api/v1/agents/{id}/heartbeat`                       |
| Ingest      | `POST /api/v1/agents/{id}/readings` (batch)                |
| Ingest      | `POST /api/v1/agents/{id}/discovered` (pending devices)    |
| Ingest      | `GET  /api/v1/agents/{id}/config` (subnets/SNMP/intervals) |
| Ingest      | `GET  /api/v1/agents/{id}/targets` · `/commands`           |
| Management  | CRUD `clients`, `sites`, `subnets`, `agents`, `printers`   |
| Management  | `POST /api/v1/printers/{id}/approve` \| `/ignore`          |
| Reporting   | `GET /api/v1/reports/fleet`, `/supplies/low`, `/errors`, `/maintenance/due` |
| Exports     | `GET /api/v1/reports/export/inventory.csv`                 |
| Exports     | `GET /api/v1/reports/export/supplies.csv`                  |
| Exports     | `GET /api/v1/reports/export/alerts.csv`                    |

Agents authenticate with `Authorization: Bearer <agent-api-key>`. Dashboard
sessions use signed cookies.

## Development

```bash
pip install -e ".[dev,agent]"
pytest          # full suite (~413 tests)
ruff check .    # lint
```

Probe a real printer for vendor data (handy when adding/extending a provider):

```bash
printer-nanny-agent probe 10.4.1.120
```

Dumps the standard MIB + the matched vendor's private subtree + (for Brother)
decoded maintenance blob percentages, so you can compare against the
printer's EWS gauge before trusting the dashboard.

## License

[Apache License 2.0](LICENSE) — © 2026 Stephen Warren.
