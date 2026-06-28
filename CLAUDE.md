# Printer Nanny

Self-hosted fleet management for printers across MSP clients/sites. Monitors
supply levels, errors, status, and page counts over SNMP; tracks maintenance;
alerts to email / Slack / Teams / FreeScout / generic webhook. Multi-tenant
(client → site → subnet → printer), multi-subnet, agent-collected.

## Working agreements (how to build here)
These are standing instructions for anyone (human or agent) doing work in this repo:

- **Parallelize with agents.** Prefer fanning work out across multiple agents /
  a Workflow when it helps — independent implementation, testing, and research
  run concurrently in isolated git worktrees. This lowers overall token usage
  and wall-clock time versus doing everything in one sequential context. Default
  to it for any multi-feature batch or broad search/review; each agent
  self-verifies, and a separate agent adversarially re-verifies before integration.
- **Interview, don't assume.** When a decision has real alternatives (scope,
  architecture, scheme, ordering, product direction), ask the user before
  committing to one — don't silently pick. Reserve this for genuine forks; keep
  obvious-default choices moving.
- **Always verify, never assume code is good.** Every change is proven, not
  trusted: run `ruff`, the pytest suite, AND an end-to-end smoke against a freshly
  seeded throwaway DB (`python -m central.seed` → exercise the feature / run the
  worker). "Tests pass" alone is not enough — check real values/states on seeded
  data. Adversarially re-verify non-trivial work in a fresh checkout. Report
  failures honestly with the output.
- **Bump version numbers between changes.** So the running **program** version is
  distinguishable from the **agent** version, bump them on every behavior-changing
  change (a feature batch bumps the minor; a fix bumps the patch; docs/test-only
  changes skip). The two version lines move **independently** — bump the program
  version only when central changes, the agent base version only when the agent
  changes — so e.g. "central 0.4.0 / agent 0.2.0" tells you at a glance which side
  moved.
  - **Program version** (keep these in lockstep with each other): `pyproject.toml`
    `version`, `central/__init__.py` `__version__`, and `central/main.py`
    `FastAPI(version=…)`. Surface it in the dashboard footer.
  - **Agent version**: `agent/printer_nanny_agent/__init__.py` `__base_version__`
    (and `agent/pyproject.toml`); the install-timestamp suffix
    (`0.x.y+YYYYMMDD-HHMMSS`) is appended automatically at install/self-update.
  - Scheme: **SemVer**. (Confirm with the user if a different scheme is wanted.)

## Architecture
- **Central server** (on-prem, Docker Compose): FastAPI JSON API + APScheduler
  worker + HTMX/Jinja dashboard, backed by PostgreSQL (SQLite for local dev/tests).
- **Site agents** (Python, pysnmp): one per site, own one or more subnets. Poll
  printers locally, **push** readings to central over HTTPS, **pull** queued
  commands on heartbeat. No inbound ports needed at sites.
- An agent can serve **multiple clients** when their networks bridge at HQ —
  assign a subnet to another client's site + a bind-IP. Each subnet row carries
  its own SNMP creds (community / v3 USM), so v2c and v3 devices can coexist.
- Data flows agent → `/api/v1/agents/{id}/...` → DB → worker (alerts, reports,
  forecasts) → channels (email / Slack / Teams / FreeScout / webhook).

## Layout
- `central/` — FastAPI app, models, worker, dashboard, notification channels.
  - `api/` — JSON API routers: `ingest`, `management`, `reporting`, `exports`.
  - `worker/` — APScheduler jobs (heartbeat, alerts, maintenance, forecast).
  - `channels/` — pluggable `NotificationChannel` impls (email, slack, teams,
    freescout, generic webhook). Attachments supported on email for reports.
  - `dashboard/` — HTMX/Jinja:
    - `routes.py` — overview / client / printer drill-downs, approvals, alerts,
      account, **customer portal** (`/portal` for client_readonly users).
    - `manage.py` — CRUD for clients, sites, printers, agents, subnets, users,
      **maintenance schedules** + **audit log** viewer.
    - `settings_routes.py` — grouped settings tabs (Branding / Notifications /
      Alerts & Reports / Polling & SNMP / Authentication / Agents).
    - `backup_routes.py` — admin-only DB backup & restore.
  - `runtime.py` — spec-driven DB-backed settings (`SPECS`) grouped by
    `SETTINGS_GROUPS`. Env only supplies defaults.
  - `secrets.py` — Fernet encryption-at-rest for stored credentials, keyed off
    `SECRET_KEY`. Self-identifying `enc:v1:…` prefix; legacy plaintext passes
    through `decrypt_value` so upgrades are lazy/no-flag-day.
  - `audit.py` — `record(db, request, user, action, target, detail)` writer used
    at every security-relevant boundary; never raises.
  - `reports.py` — scheduled weekly fleet email + monthly billing CSV.
  - `auth_oidc.py`, `auth_oauth_smtp.py` — pluggable SSO + OAuth SMTP.
  - `snmp_parse.py` — brand-agnostic SNMP supply/level parsing (shared w/ agent).
  - `snmp.md` — Printer-MIB OID reference.
- `agent/` — standalone `printer-nanny-agent` package.
  - `providers/` — vendor-specific enrichment plugins; one registered per
    enterprise prefix. **Brother is consolidated**: a single `brother`
    provider sequences four passes internally (maintenance blob → live
    alert + history events → PJL → EWS), skipping the network fallbacks
    once exact percentages exist.
  - `mdns.py` — optional Bonjour/DNS-SD discovery (`agent[mdns]` extras).
  - `updater.py` — self-update via `update_agent` command; writes
    `.pn-update-result.json` so the dashboard can show success/failure.
- `migrations/` — Alembic environment + versions (0001 → 0013).
- `deploy/` — Caddyfile, installer scripts, sample systemd unit.
- `tests/` — pytest suite (~413 tests; ~25s end-to-end on Postgres-less SQLite).

## Conventions
- Python 3.12 in Docker; code stays 3.9-compatible (`from __future__ import
  annotations`) so it runs on the local system Python too.
- Sync SQLAlchemy 2.0 (`Mapped[]` style) + Alembic. Sessions via
  `central.db.SessionLocal` / the `get_db` FastAPI dependency.
- API is versioned under `/api/v1`. Agents authenticate with a per-agent API key
  (`Authorization: Bearer <key>`, hashed at rest). Dashboard users use signed
  sessions + roles (`admin` / `tech` / `client_readonly`).
- Time-series lives in `readings`, append-only and indexed by `(printer_id, ts)`.
  On Postgres a BRIN index on `ts` keeps range scans cheap (migration 0002).
- SNMP is brand-agnostic via RFC 3805 Printer MIB. Vendor providers add real
  percentages where the standard MIB only reports buckets (Brother) or
  brand-tag/status-message scalars (HP, Lexmark, Xerox, Kyocera, Canon, Ricoh,
  Konica Minolta). SNMPv3 USM creds per subnet, passwords encrypted at rest.
- Operational config (channels, alert thresholds, polling, SNMP defaults, SSO,
  reports, branding, agent install source) lives in DB via `central/runtime.py`
  and is edited in the Settings UI **grouped into tabs**. Only `DATABASE_URL` +
  `SECRET_KEY` come from env (`central/config.py`).
- Secret-typed settings + SNMPv3 USM passwords are **encrypted at rest** with
  a Fernet key derived from `SECRET_KEY`. Lazy migration: legacy plaintext is
  swept into encrypted form on every save and at api startup.
- **Audit trail** — every login (incl. failures with attempted username),
  settings change (key names only), user/agent/printer/subnet CRUD, approvals,
  alert acks, agent updates, portal reports, backup downloads / restores are
  recorded in `audit_log` with `(ts, user_id, username, ip, action, target,
  detail)`. Admin-only viewer at `/manage/audit` with a substring filter.
- Agents are managed entirely in the UI: enroll (key shown once), assign
  subnets/SNMP under Agents (discovery status lives on each subnet row), update
  via the `update_agent` command. Versions are `0.1.0+YYYYMMDD-HHMMSS` — the
  suffix is the install timestamp, changes on every self-update.
- Auth is pluggable: local username/password always works; OIDC/SSO turns on
  from Settings, matching/provisioning users by email. (Use your IdP for MFA;
  this project doesn't ship its own TOTP.)
- The **customer portal** at `/portal` is the home for `client_readonly` users:
  trimmed view of their fleet with friendly names, "your supplies last ~Nd"
  forecasts, open issues, and a "Report a problem" form that opens a FreeScout
  ticket via the existing channel (or falls back to alert-email recipients).
- **Printer friendly names** (`printers.display_name`) are used everywhere a
  printer is named — dashboards, alert titles, recent activity, the weekly
  report. Operators set them in the printer edit form.
- Maintenance schedules at `/manage/maintenance`: per-printer or model-wide
  with interval-days and/or page-threshold and `next_due`. **Mark serviced**
  rolls `next_due` forward by `interval_days` and the worker's next
  reconcile pass auto-resolves the maintenance-due alert.
- DB **backup & restore** from the UI at `/admin/backup` (admin only).
  Postgres: `pg_dump --format=custom` / `pg_restore --clean`. SQLite: streamed
  file copy + atomic replace. Restore is gated behind typed `RESTORE`
  confirmation.

## Dev
- `pip install -e ".[dev]"` (add `postgres` / `agent` / `agent-mdns` extras as needed).
- `python -m central.seed` — create tables + load demo data (SQLite by default).
- `uvicorn central.main:app --reload` — API + dashboard at http://localhost:8000.
- `python -m central.worker.run` — background worker loop (alerts, reports,
  forecasts).
- `python -m central.worker.run --once` — single cycle, useful in CI.
- `docker compose up` — full stack (Postgres + api + worker + dashboard + Caddy).
- `alembic upgrade head` — apply migrations (Postgres).
- `python -m central.enroll --client … --site … --agent … --subnet … --json` —
  mint an agent + key server-side (used by setup scripts / `docker compose exec`).
- `python -m central.seed --minimal` — clean slate (admin/tech + alert rules,
  no demo data) for real-equipment testing.
- Local agent (same box as central): `scripts/setup-local-agent.sh` (one-shot) or
  `scripts/install-local-agent-macos.sh` (persistent launchd). Docker Desktop
  containers can't reach the LAN, so the local agent runs on the host; on Linux
  the optional `agent` compose profile runs it host-networked. SNMP to LAN peers
  needs an unsandboxed shell.
- `printer-nanny-agent probe <ip>` — dumps standard + vendor private-MIB
  subtrees + decoded Brother maintenance blob percentages. Use this against
  any printer that needs a new/extended provider — paste the output as the
  starting point.
- `pytest` — full suite. ruff via `ruff check central agent tests migrations`.

## Status
Production-ready feature surface (as of PR #46):

**Core**: central server, multi-tenant model, push-based agents, brand-agnostic
SNMP, alerting with dedupe + auto-resolve, scheduled reports, friendly names,
days-until-order supply forecasts, per-client / per-site rollups, recent
activity, maintenance schedules, audit log, DB backup/restore.

**Channels**: email (incl. OAuth SMTP / XOAUTH2), Slack, Teams, FreeScout,
generic webhook. Attachments supported on email for reports.

**Vendor providers** (in `agent/printer_nanny_agent/providers/`):
- **Brother** (consolidated): maintenance blob (BRAdmin data path, exact
  percentages from the SNMP private MIB), live alert + history, PJL on
  TCP/9100, EWS HTML scrape. Adds belt/fuser/laser/PF-kit life rows.
- **HP**, **Lexmark** — brand tag, model, front-panel message.
- **Xerox**, **Kyocera**, **Canon**, **Ricoh**, **Konica Minolta** — defensive
  scaffolding (brand tag + front-panel message). Exact private-MIB supply
  decoding extended per-model when a probe lands.

**Discovery**: SNMP sweep across configured subnets + optional mDNS / Bonjour
(zeroconf, `agent[mdns]` extras) on the agent's local subnet.

**Security**:
- Per-agent API keys hashed at rest, shown once at enrollment, rotatable.
- All operator-managed secrets (SMTP password, OAuth tokens, FreeScout key,
  Slack/Teams/webhook URLs, OIDC client secret, SNMPv3 USM passwords)
  Fernet-encrypted at rest with a `SECRET_KEY`-derived key.
- Audit trail at `/manage/audit`.
- MFA via the configured OIDC IdP (no built-in TOTP).

**Operator surface**: grouped settings (six tabs), agents page with collapsed
discovery + diagnostics, conditional Approvals nav, contextual nav badges,
customer portal for `client_readonly` users.
