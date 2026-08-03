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
  - `api/` — JSON API routers: `ingest`, `management`, `reporting`, `exports`.
  - `worker/` — APScheduler jobs (heartbeat, alerts, maintenance, forecast).
  - `channels/` — pluggable `NotificationChannel` impls (email, slack, teams,
    freescout, generic webhook). Attachments supported on email for reports.
  - `dashboard/` — HTMX/Jinja:
    - `routes.py` — overview / client / printer drill-downs, approvals, alerts,
      account, **customer portal** (`/portal` for client_readonly users).
    - `manage.py` — CRUD for clients, sites, printers, agents, subnets, users,
      **maintenance schedules** + **audit log** viewer.
    - `people.py` — **end users, groups, and printer assignment** (`/manage/people`).
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
  - `mdns.py` — optional Bonjour/DNS-SD discovery (`agent[mdns]` extras).
  - `updater.py` — self-update via `update_agent` command; writes
    `.pn-update-result.json` so the dashboard can show success/failure.
- `migrations/` — Alembic environment + versions (0001 → 0033). Revision 0001 is
  `Base.metadata.create_all()`, so **the ORM metadata is what builds a fresh DB** —
  an index declared only in a later migration is silently absent on new installs.
  Declare indexes in the model's `__table_args__` and mirror them in the migration.
- `deploy/` — Caddyfile, installer scripts (`install-agent.sh`/`.ps1`,
  `install-workstation-macos.sh`), sample systemd unit and launchd plists,
  `macos-pkg/` (the `preinstall`/`postinstall` the `.pkg` runs as root — the same
  files that ship, never a second copy), plus `WINDOWS-MSI-TESTING.md` (build +
  Server 2016→2025 smoke, and the 2026-07-30 real-spooler run with the three
  defects it found) and `MACOS-CLIENT-TESTING.md` (real-CUPS verification, the
  `.pkg` procedure, and the 2026-07-30 real-Mac run — done items marked and dated,
  the rest still open).
- `tests/` — pytest suite (~1770 tests, serial, on Postgres-less SQLite).
  **Quote the machine with the number**: ~2.5min on a GitHub `ubuntu-latest`
  runner, ~6-7min on a 4-vCPU container. A bare figure is unfalsifiable, which
  is how a run on a *busy* box (9min) gets reported as a regression against a
  documented one — that happened. ~80% of the wall clock is
  `create_all`/`drop_all` rebuilding the schema per fixture (measured: 282s of
  353s across 744 rebuilds), so the total tracks single-core speed and is the
  first thing to attack if it ever needs to be faster. Password hashing, the
  obvious suspect, is **1.8%** — measured, after it was assumed.
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
- **Checkbox booleans need a presence marker.** An unchecked box posts nothing,
  so a handler that ignores empty fields (`subnet_update`) cannot tell "unchecked"
  from "this form didn't carry the field" — reading it directly makes an inline
  rename silently clear the flag. `trusted` pairs with a hidden `trusted_present`;
  `runtime.save_settings` solves the same problem with its `sections` argument.
  Same failure class as the `save_settings(sections=None)` wipe.

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
re-raises, because terminal is not transient. Note the trap left behind:
`workstation_cli` returns exit 2 for a refused key specifically so a service
manager will not loop — but the plist's `KeepAlive{SuccessfulExit=false}` restarts
on any non-zero exit, so on macOS it loops anyway.

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

**On Windows that rule is only half-applied, and the missing half undoes it.**
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

**Core**: central server, multi-tenant model, push-based agents, brand-agnostic
SNMP, alerting with dedupe + auto-resolve + flap damping, quiet hours + maintenance
windows, scheduled reports, friendly names,
days-until-order supply forecasts, per-client / per-site rollups, recent
activity, maintenance schedules, audit log, DB backup/restore, one-screen
onboarding (claim-code self-enrollment, trusted-subnet auto-approve, bulk
approvals, per-client defaults).

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
customer portal for `client_readonly` users. Per-agent **needs-update** badges +
scoped "update outdated" bulk action, and one-click **Windows MSI** builder
(self-contained installer with enrollment baked in, Server 2016→2025).
