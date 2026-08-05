# Printer Nanny — Completion Playbook

*Written 2026-08-03 against `524136b` (central 0.27.2 / agent 0.16.3). This is the
execution plan for finishing the product: every decision needed to build it is
recorded here, so the work can proceed without further interviewing.*

*Updated 2026-08-03 against `4c68f40` on `claude/completion-program`
(**central 0.30.0 / agent 0.17.0**) — phases 1 and 2 executed. Every `DONE` mark
below was verified against the code, not against a commit message.*

The two older strategy documents in this directory are **snapshots, not backlogs**.
`audit-2026-07.md` cites fixes at 0.6.0–0.12.0 and `competitive-playbook.md` was
written before several of the features it asks for existed. Both were re-verified
against current code before this plan was written, and roughly a third of what
they list is already shipped. **Read this file, not those, for what is left.**

---

## 0. Status, and the errors in this document that were found by checking

**Every phase that can be completed without hardware is complete. What is left is
§6b and only §6b** — the verification that needs a real printer, a real Windows
box and a real Mac. `deploy/HARDWARE-VERIFICATION.md` is the single
operator-followable brief for it.

> **Re-verified 2026-08-04 against `8ca354d`, by reading the code rather than the
> commit messages.** The rows below previously read "Phase 3 NOT STARTED" and
> "F12 NOT BUILT"; both were **wrong**, and had been since `e7d64f8`. The status
> block was written at `4c68f40` and never updated as the work landed, which is
> the same failure this document opens by warning about — committed, for the
> third time, by this document. Anything that says a §6a item is open is stale;
> §3's State column is left as written at `4c68f40` and is superseded by the
> re-verification recorded there.

| | State |
|---|---|
| Phase 1 (§4) | **DONE**, all six items, plus one found while doing them |
| Phase 2 (§5) | **DONE — F1–F12.** F12 was the documented cut point and was *not* cut: `central/remote.py`, `central/dashboard/remote.py` |
| Phase 3 (§6a) | **DONE — all eleven**, plus the fourteen `confirm()` sinks and the duplicated `FastAPI(...)`. Evidence per item in §3 |
| Phase 3 (§6b) | **NOT STARTED**, and not startable from a container — see `deploy/HARDWARE-VERIFICATION.md`. **This is the whole of what remains.** |
| Version | central **1.0.0** / agent **1.0.0** (`8ca354d`) |

### The 1.0.0 claim, stated honestly

§6c gates 1.0.0 on **§6a and §6b**, and singles out D4 — "a downgrade that
deletes operator settings including SMTP credentials is data loss with a
documented trigger" — as something that must be fixed before any 1.0 claim.

**D4 is fixed** (`migrations/guard.py`: a global `drop_table` guard that refuses
a destructive downgrade unless `refuse_if_populated` finds the table empty, with
an explicit override env var, exercised against the real `alembic` CLI in
`tests/test_migration_downgrade_guard.py`). So the gate that was called out by
name is genuinely met, and so is the rest of §6a.

**§6b is not**, and it is half the gate. So 1.0.0 is currently a claim about the
code, not about the product: nothing in it has been verified against the printer
that is known to reject a queue Windows says is healthy. That is the one thing
this codebase has learned four times not to infer — a queue that exists, lists
and converges is not a queue that prints. Treat the version as provisional until
§6b runs, or re-cut it afterwards.

### The 2026-08-04 security audit — what it found after §6a was "done"

Ten parallel read-only agents audited the finished branch: tenancy/authz,
device-input injection, secrets, remote hands + driver staging, auth/session/CSRF,
an adversarial re-verification of §6a, migrations, test-suite honesty, docs, and
supply chain / CI / deployment. Every finding below was re-verified by hand
before it was fixed, and each has a regression test.

**This section exists because §6a being complete did not mean the product was
secure.** Every item here sat underneath work that was correctly marked done.

**The one that mattered most: SSO and SCIM composed into an admin takeover.**
Neither half looked like a vulnerability alone. `_match_or_provision` matched an
IdP email against `email OR username` and returned whatever it found, with no
check of what kind of account that was, *before* the `auto_provision` gate --
so an address the IdP would issue (guest or self-service sign-up, ordinary
features) was enough to become the bootstrap admin. Meanwhile a SCIM token could
rewrite any local account's **email**, which chose the address SSO would then
match. Both halves are closed and neither can be re-opened silently, because
four tests asserted the old behaviour and now assert the new rule.

| Area | Found | Now |
|---|---|---|
| SSO / SCIM | Account takeover, no password, no throttle | Refused unless an operator opts in; SCIM manages only what it provisioned |
| Sessions | Logout, password change and admin reset left a captured cookie live 12h | `User.session_epoch`, compared per request |
| Session resolution | **Seven** copies of the lookup; one never checked `active` | One `deps.session_user` |
| Windows agent | `%PROGRAMDATA%\PrinterNanny` had no ACL -- `os.chmod` writes no DACL there | SYSTEM + Administrators, granted by SID; verified by observation |
| macOS drivers | A bad `driver_ref` ran `installer -pkg` as root **every poll, forever** | Shape-checked before anything executes; the install is marked |
| Driver cache | A cache hit was decided by a marker's existence, never its recorded digest | Digest compared |
| Tenancy | A printer could be re-homed to another client's site, exposing its SNMP community to that client's agent while billing stayed put | Refused on both doors |
| Reorder events | The publisher resolved to nothing; the toggle published **nothing, forever** | Wired, with the resolution itself tested |
| HTTP | `/openapi.json` public; no frame-busting; `/api/v1` CRUD wrote no audit rows | Closed, headers added, audited |
| Schema | Three columns differed on upgraded installs; nine docstrings named the wrong parent | Fixed, both guarded by tests |
| Supply chain | The live `SECRET_KEY` was baked into every image layer | `.dockerignore` |

**Two patterns are worth carrying forward, because both are about the shape of
the code rather than any one bug:**

1. **Duplication defeats a security fix.** The session-epoch change passed its own
   logout test and did not work, because five of the seven session resolvers had
   not been touched. The fix was consolidation, and consolidating immediately
   surfaced a second bug nobody was looking for.
2. **A test can assert that a feature is broken.** `test_supply_reorder.py`
   asserted `_resolve_publisher() is None` -- with a comment saying to rewire it
   when the event bus arrived. The bus arrived under a different name, nothing
   failed, and nobody read the comment. Every other test injected a fake
   publisher, so they exercised the loop and never the wiring.

### Errors in this document, corrected below

This plan was written to stop a third round of planning from stale docs. Checking
it against the code found five things wrong with it, and they are listed here
rather than only fixed in place, because a reader who trusted the original needs
to know *what* to distrust:

1. **F6 was not "a view-only build of something missing".** §2 said "no route or
   template renders one" and §5 called it "the missing route and template". Both
   are wrong: `/security/posture` and `security_posture.html` shipped in
   **`b9d08c2`, 2026-06-28**, five weeks before this plan was written. The real
   work — and it was substantial — was correcting **what the report asserted**,
   plus adding a client scope and a CSV export. See §5 F6.
2. **SCIM is not deferred; it already exists.** The decision table says
   "Compliance (SCIM/SIEM/FIPS/KMS) — Deferred entirely". SCIM 2.0 provisioning
   and deprovisioning shipped in `b608c55` (`central/api/scim.py`, router
   registered in `main.py`, `tests/test_scim.py`). Deferring it reads as "not
   built", which is the exact failure mode this document exists to end. SIEM,
   FIPS and KMS *are* genuinely deferred.
3. **C6's mechanism is misdescribed.** It says "Mark serviced ... sets
   `next_due = None` while reporting success". It does not:
   `manage.py:1892` guards the assignment with `if next_due is not None`, and
   that guard has been there since the feature shipped (`9c4a70e`). What
   actually happens is that the `MaintenanceRecord` row is written with
   `next_due=None` and the *schedule* keeps its stale, now-past `next_due` — so
   the schedule stays permanently due and the maintenance alert never
   auto-resolves, while the UI says "Service logged". Still a bug, different
   bug — and **fixed since** (§6a): `next_due` is cleared for an interval-less
   schedule, which resolves the alert on the next worker cycle.
4. **The baseline figures are stale by ~45%.** §7 quotes 1756 passed / 21
   skipped; §8 quotes "~2.5 min here". Re-measured — see §7.
5. ~~**`python3 scripts/ipp_bisect.py --contract` does not work as documented.**~~
   **Fixed 2026-08-04 — in the code, not the docs.** §6b,
   `deploy/WINDOWS-IPP-BISECT-PROMPT.md` and `deploy/WINDOWS-MSI-TESTING.md` all
   documented the bare form; argparse evaluates required-ness *during*
   `parse_args`, before the `--contract` early return, so it exited 2 and printed
   usage. Three documents agreeing on the intended behaviour is the specification
   — so `subject`/`donor`/`--oracle` are now enforced by hand *after* that return
   rather than declared required, and the docs were left saying what they said.
   A real run still refuses identically (exit 2, argparse's own usage line and
   message) when an argument is missing; only `--contract` behaves better.
   The placeholder workaround still works. `tests/test_ipp_bisect.py`.

### One code defect found while checking — **since fixed**

`central/main.py:54-55` declared `app = FastAPI(title="Printer Nanny",
version="0.30.0")` **twice**, on consecutive lines — a merge artefact from
`78f3345` (the F10 merge). It was harmless as it stood, since the second binding
replaced an identical object before any route, middleware or handler was
attached, but it was dead code in the most load-bearing statement in the file and
the first person to add a line between the two would have produced a silently
unconfigured app. `central/main.py` now declares `app` exactly once.

---

## 1. Decisions locked

| Question | Decision |
|---|---|
| Scope | Complete everything, in three phases: light bug pass → features → complete bug pass |
| Delivery | **One branch, one PR for the entire program** (`claude/completion-program`) |
| Hardware | All three rigs available: Brother MFC-L8900CDW, Windows box, Mac |
| Billing CSV | **Add** period-delta columns; leave lifetime columns untouched |
| Feature order | Delegated — sequenced here by dependency and risk, with rationale |
| Supplies loop | **Recommend-only**. No SKU catalog, no inventory, no PO generation |
| Remote hands | Write actions **where the device supports them**, degrading to read-only otherwise |
| Retention | Raw readings 90 days, then one rollup row per printer per day, kept forever |
| PSA integration | **Dropped.** FreeScout remains the ticket path |
| Compliance (SIEM/FIPS/KMS) | **Deferred entirely.** ~~SCIM~~ — **correction:** SCIM 2.0 already shipped (`b608c55`, `central/api/scim.py`), so it was never deferrable. SIEM, FIPS and KMS remain deferred |
| Version at completion | **1.0.0** *(cut — see §0 on what half of its gate is still unmet)* |

### On the single-PR decision

This was raised as a concern and reaffirmed, so it is the plan. Two mitigations
make it workable, and neither changes the decision:

- **Commit granularity stays one-item-per-commit** with a real message. A single
  PR can still be reviewed commit-by-commit; what is lost is the ability to merge
  or revert one theme independently, not the ability to read it.
- **Rebase onto `main` weekly.** A long-lived branch spanning a whole program is
  the drift risk. `agent/` changes in particular retarget every running worktree
  (see CLAUDE.md), so the branch must not fall far behind.

---

## 2. Already done — removed from scope

Verified against current code on 2026-08-03. Do not rebuild these; the older docs
still list them as open.

| Claimed open by | Item | Evidence it is done |
|---|---|---|
| audit P0 (all six) | settings bool-wipe, unrouted alert loss, dead-worker invisibility, Brother regex, agent spool, SNMPv3 polling | `save_settings(sections=…)`; `tests/test_unrouted_notifications.py`; `/readyz` + `last_success_at`; anchored `(?<![A-Za-z0-9])` colour group; `ReadingSpool`; `_params_for_target` → `config.snmp_for(subnet)` |
| playbook | Cross-tenant leak on JSON API | `require_staff` rejects `client_readonly` on both routers; `tests/test_api_authz.py` |
| playbook | Silent alert drops / no retry | `central/channels/delivery.py::retry_due`, dead-letter states |
| playbook | Dead-letter has no UI | `alerts.html` renders owed/dead counts |
| playbook | Naive two-point-slope forecasting | `forecast_days_to_empty` is **least-squares over the depleting segment**, refill-aware |
| playbook | Predicted-depletion alerts absent | `AlertConditionType.predicted_depletion` |
| playbook | Colour/mono meter split absent | `printers.mono_count` / `color_count`, exported in billing CSV |
| playbook | ESG/sustainability absent | `queries.sustainability_rollup`, rendered in the portal |
| playbook | Component-life predictive maintenance absent | Maintenance schedules trigger off belt/fuser/laser/drum/PF-kit `Supply` rows |
| audit | Flash messages never displayed | `request.session.pop("portal_flash"…)`, `approvals_flash` |
| audit | CDN assets, a11y, dark mode | Vendored Tailwind/htmx, `tests/test_dashboard_a11y.py` |
| audit | No quiet hours / suppression | `central/suppression.py` |
| audit | No hysteresis / flap damping | deadband + `renotify_cooldown_min` |

~~**Security-posture reporting is half done**: `printers.firmware` is collected
and the schema comments reference a posture report, but no route or template
renders one. Treated below as a view-only build, not a data build.~~

**CORRECTION (2026-08-03).** That paragraph is wrong, and it is the same class of
error this document was written to end. The route (`/security/posture`) and the
template (`security_posture.html`) shipped in **`b9d08c2` on 2026-06-28** — over
a month before this plan was written — with the collection behind them. This
document should have listed F6 in the table above, not below in §5. What was
genuinely missing was an **export**, a **client scope**, and an **honest claim**;
see §5 F6 for what the work actually turned out to be.

---

## 3. Verified still open *(as originally written; State column re-checked 2026-08-03, then again 2026-08-04)*

> **The State column below is superseded.** It was accurate at `4c68f40`. Every
> item in it — S3, S4, S5, C5, C6, D3, D4, O4, O5, O6, O7 — has since been
> closed; each row's evidence is given inline where it was re-verified against
> `8ca354d`. Read §0 for the current position.

Every item below was confirmed against current code **when this plan was
written**. Three were reproduced outright; those are marked **[reproduced]** and
each has a failing-condition recipe in §7.

The **State** column was added on 2026-08-03 and re-verified against
`4c68f40` — by reading the code, not the commit messages.

### Security — device-controlled input reaching operator surfaces

| # | Item | Location | Size | State |
|---|---|---|---|---|
| S1 | **[reproduced]** FreeScout ticket body is raw HTML built from SNMP strings. A printer label of `<img src=x onerror=alert(1)>` survives unescaped into the ticket | `channels/freescout.py:36-42` | XS | **DONE** — and wider than scoped. Slack (`<!channel>`, forged links) and Teams (HTML *and* Markdown) were also live; each channel escapes per its own contract, the webhook stays verbatim on purpose, and a multi-line `sysDescr` reaching the email `Subject` turned out to be suppressing delivery entirely. `tests/test_channel_injection.py` |
| S2 | CSV formula injection. `_csv_response` neutralises RFC 4180 quoting only — its own docstring says so — and nothing neutralises a leading `= + - @`. Affects `/api/v1/reports/export/*` and the emailed billing CSV, whose `model`/`hostname`/`serial` cells are device-controlled | `api/exports.py:64`, `reports.py:190` | XS | **DONE** — `central/csv_safe.py`, `safe_writer()` neutralises every cell incl. the header; numeric literals exempted by a closed-form grammar (not `float()`); `None` stays a blank cell. `tests/test_csv_injection.py` |
| S3 | No CSRF tokens anywhere. `SameSite=lax` is the only mitigation and it does **not** cover `GET /admin/backup/download`, which exfiltrates the whole database via top-level navigation | `main.py:49`, `backup_routes.py` | M | **OPEN** — `main.py` still has only the `same_site="lax"` cookie; no token anywhere. Largest remaining security item |
| S4 | `assert_secure()` only fires when `DATABASE_URL` is not SQLite, so the copy-`.env.example` path boots on a published signing key: forgeable sessions, decryptable stored secrets | `config.py:52-60` | S | **OPEN** — still gated on `self.is_production` |
| S5 | No login rate limiting; `admin`/`admin` recreated on container start with no forced rotation | `dashboard/routes.py`, `seed.py` | S | **OPEN** — `security.py` has a comment about it and no mechanism |

### Correctness

| # | Item | Location | Size | State |
|---|---|---|---|---|
| C1 | **[reproduced]** Customer portal hides live faults. `open_alerts(db, 30)` takes the newest 30 **fleet-wide**, then filters by tenant in Python — so a customer with an open critical fault sees "no open issues" whenever 30 newer alerts belong to other tenants | `dashboard/routes.py:191-194`, `queries.py:179` | S | **DONE** — filter and LIMIT both in SQL; also removed a per-alert `db.get` and a latent `AttributeError` on a dangling `printer_id`. `tests/test_portal_tenant_scoping.py` |
| C2 | **[reproduced]** `alembic upgrade head` dies on any `%` in `DATABASE_URL` — `ValueError: invalid interpolation syntax`. The offending line is redundant; line 36 already injects the URL bypassing interpolation. A percent-encoded password gives a permanently unmigrated schema while the app itself boots fine | `migrations/env.py:16` | XS | **DONE** — line deleted (not escaped to `%%`) with the reasoning in-file. Regression tests drive the real `python -m alembic` in a subprocess. `tests/test_alembic_env.py` |
| C3 | Monthly billing CSV emits **lifetime cumulative** meters. The reset-safe delta helper exists and is called only by the ESG rollup | `reports.py:203-219`, `queries.py:537` | M | **DONE** as part of F3, in the form chosen: `pages_period`/`mono_period`/`color_period` **added beside** untouched lifetime columns |
| C4 | `monthly_day` of 29/30/31 silently skips months — `now.day == want_dom` never matches in February. Setting 31 loses 5 of 12 billing reports a year | `reports.py:286` | XS | **DONE** as part of F3 — clamped to the month's last day |
| C5 | One out-of-range supply level 422s an entire ingest batch; with the spool the poisoned batch re-sends every cycle and never drains | `schemas.py:42` | S | **OPEN** — `level_pct: Optional[float] = Field(default=None, ge=0, le=100)` unchanged |
| C6 | ~~"Mark serviced" on a schedule with no `interval_days` sets `next_due = None` while reporting success — the schedule silently stops recurring~~ **Mechanism misdescribed** — see below | `dashboard/manage.py:1880-1893` (was cited as `:1531`) | S | **OPEN**, with a corrected description: `if next_due is not None` (present since `9c4a70e`) means `sched.next_due` is **never** nulled. The `MaintenanceRecord` is written with `next_due=None` and the schedule keeps its stale past `next_due`, so it stays permanently due and the maintenance alert never auto-resolves — while the UI reports "Service logged" |

### Deployment / infrastructure

| # | Item | Location | Size | State |
|---|---|---|---|---|
| D1 | Dockerfile fallback install list omits `authlib`, `cryptography` **and** `tzdata`, and `pip install -e . \|\| true` swallows failure — the image builds green and 500s on SSO, or silently loses tz data that quiet hours depend on | `deploy/Dockerfile:17-22` | XS | **DONE** — the dep layer is now *projected from* `pyproject.toml` so a second list cannot exist to drift, and the `\|\| true` is gone. The "fallback" was never a fallback: `pip install ".[postgres]"` with only `pyproject.toml` copied has never once succeeded |
| D2 | **CI has zero Postgres.** The BRIN index, advisory-lock leader election and `pg_dump`/`pg_restore` paths run only in production and are tested nowhere | `.github/workflows/` | M | **DONE** — `.github/workflows/postgres.yml`, whole suite on `postgres:16-alpine`, deliberately **not** path-filtered. Writing it found two shipped defects immediately (see below) |
| D3 | `0001_baseline` is `create_all` against *current* metadata, so migration drift cannot be detected — the check passes by construction | `migrations/versions/` | M | **OPEN**, and the wording overstates: there is no automated drift check to "pass by construction" — `tests/test_alembic_env.py` covers the `%` regression and `tests/test_migration_chain.py` covers chain *shape*. Neither compares metadata to migrations. What D2 did add is real Alembic runs on Postgres, which is what caught the bootstrap defect |
| D4 | Migration downgrades drop tables their upgrade did not create (`0003`, `0007`, `0012`, `0014`, `0019`); on a pre-Alembic install `downgrade 0002` deletes every operator setting including SMTP credentials | `migrations/versions/` | S | **OPEN** — `0003.downgrade()` still drops `app_settings` on `has_table`, i.e. regardless of whether this revision created it |

**Two defects D2 found on its first run**, neither visible from SQLite, both
fixed on this branch and both worth carrying as rules rather than as fixes:

- A `Boolean` declared `server_default=text("1")` renders as an unquoted integer.
  Postgres refuses it (`DatatypeMismatch`), and since revision 0001 is
  `create_all` the ORM declaration *is* what builds a fresh database — so
  `alembic upgrade head` could not create the schema at all, and had not been
  able to since 0.12.0. Use `true()`/`false()`.
- `pg_dump` was handed SQLAlchemy's URL verbatim. **libpq does not understand the
  `+driver` suffix**: a conninfo string not beginning exactly `postgresql://` or
  `postgres://` and containing no `=` is read as a bare *database name*, so host,
  port and user silently fall back to defaults — it tried a local socket, failed
  with `role "root" does not exist`, and left a **zero-byte** file. Backup and
  restore had never worked on Postgres. The password also travelled in argv,
  where `ps` discloses it; it is in `PGPASSWORD` now.

### Scale / observability / UX

| # | Item | Location | Size | State |
|---|---|---|---|---|
| O1 | **No readings retention, rollup or partitioning.** ~52M rows/year at 500 printers. Two docstrings claim monthly partitioning that `0002` explicitly deferred | `models.py:394`, `db.py:52` | L | **DONE** via F1 — `central/retention.py` + `reading_rollups`. Both false partitioning docstrings now say so explicitly. Partitioning itself remains deliberately unbuilt; the rollup is what was chosen |
| O2 | API container configures no logging at all — only `worker/run.py` calls `basicConfig`, so every `log.info()` in the API is discarded | `main.py` | XS | **DONE** — `central/logging_config.py`, one `configure_logging()` both processes call. `LOG_LEVEL` moves `central`/`printer_nanny` **only**, never root, because `httpx` logs a webhook URL (which *is* the credential) at INFO and SQLAlchemy logs bound parameters at DEBUG |
| O3 | No fleet-wide printer list and no printer search anywhere. A tech handed "10.4.7.23 is jamming" must already know the client | `dashboard/` | M | **DONE** via F7 |
| O4 | Audit log caps at 200 rows with no offset — at 100k rows, 99,800 are permanently unreachable | `dashboard/manage.py:1620` | S | **OPEN** — still `.limit(200)` with no offset (now at `manage.py:1954`/`1965`) |
| O5 | Unbounded dashboard queries; `per_client_rollup` is 5N+1 (~1,001 queries at 200 clients) | `queries.py` | M | **OPEN** — `per_client_rollup` still loops clients issuing per-client counts |
| O6 | `retry_due` has no LIMIT and no ordering — one SMTP outage can stall a whole worker cycle under the leader lock | `channels/delivery.py:450` | S | **OPEN** — `_due_deliveries` still selects without LIMIT or ORDER BY (now at `delivery.py:256`) |
| O7 | Nothing live-updates and there is no "data as of" timestamp — a NOC screen shows a green fleet that has been on fire for hours, authoritatively | `dashboard/templates/` | M | **OPEN** |

---

## 4. Phase 1 — light bug pass — **DONE**

All six landed, plus a seventh found while doing them: the fresh-Postgres
bootstrap defect (a `Boolean` with `server_default=text("1")`), which only
existed to be found because item 4 put Postgres in CI. That is the phase working
as intended — the ordering was chosen so that each item made the next one's
defects visible, and it did.

**Rule for what belongs here:** only items where building features on top would
either be untrustworthy or would multiply the defect. Everything else waits for
phase 3. This phase should be days, not weeks.

| Order | Item | Why it must precede feature work |
|---|---|---|
| 1 | **C2** alembic `%` (one-line delete) | Retention, rollup and billing all add migrations. A migration path that dies on a realistic password is not a foundation |
| 2 | **D1** Dockerfile deps | `tzdata` is missing from the fallback, and quiet hours resolve wall-clock-local. Features get built against an image whose behaviour differs from dev |
| 3 | **O2** API logging | Everything after this is debugged through logs that currently go nowhere |
| 4 | **D2** CI Postgres | Retention/rollup is Postgres-shaped work (BRIN, partitioning). Landing it with zero Postgres coverage repeats the exact blindness that let tier-1 ship broken four times |
| 5 | **S1 + S2** escaping/neutralisation | New surfaces (posture report, billing exports, event payloads) will carry the same device strings. Fix the pattern before multiplying it |
| 6 | **C1** portal tenant scoping | Cheap, customer-facing, and currently wrong in production |

Everything else in §3 explicitly waits.

**Version:** `0.28.0` (central only — no agent change in this phase).

---

## 5. Phase 2 — the feature program — **DONE, F1–F12**

| | Feature | State |
|---|---|---|
| F1 | Readings retention + daily rollup | **DONE** — `central/retention.py`, `reading_rollups`, migration 0034 |
| F2 | Typed outbound event surface | **DONE** — `central/events/`, `/manage/events`, migration 0035 |
| F3 | Cost-per-page billing (+ C3, C4) | **DONE** — `central/billing.py` + `central/money.py`, `/manage/billing`, migration 0036 |
| F4 | Supplies recommend-only | **DONE** — `central/reorder.py`, `/supplies/reorder`, migration 0041 |
| F5 | Occurrence-rate alerting | **DONE** — `AlertConditionType.occurrence_rate` + `/manage/alert-rules`, migration 0037 |
| F6 | Device security-posture report | **DONE**, but **not the work this document described** — see below |
| F7 | Fleet-wide printer search | **DONE** — `/printers`, tenant-scoped in the WHERE, no index by design |
| F8 | Yield-gap / non-OEM detection | **DONE** — `central/supply_yield.py`, `supply_cycles` + `supply_yield_expectations`, `/supplies/yield`, migration 0043 |
| F9 | Per-client white-label | **DONE** — `central/branding.py`, migration 0038 |
| F10 | Server-pushed device definitions | **DONE** — `central/device_definitions.py` + the agent's vendored copy, migration 0039 |
| F11 | Collector redundancy | **DONE** — `central/collector.py`, migration 0040 |
| F12 | Remote hands / EWS proxy | **DONE** — `central/remote.py` + `central/dashboard/remote.py`, `/manage/printers/{id}/remote/*`. It was the documented cut point (§8: "if the program has to be cut short, cut F12 first") and did not need cutting |

Sequenced by dependency and risk, since order was delegated. The governing
principles: cheap-and-independent first so the branch shows progress early;
anything gated on retention after retention; and the one item that widens the
security surface **last**, so it lands on a spine that is already hardened and
fully covered.

Note how much of the "enabler" tier from `competitive-playbook.md` is already
built (§2) — colour/mono meters, regression forecasting and predicted-depletion
alerts were the three prerequisites it named, and all three exist. What actually
remains as an enabler is retention and a typed event surface.

### F1 — Readings retention + daily rollup *(enabler, L)*

Raw readings kept **90 days**; older data collapsed to one row per printer per
day and kept **forever**.

**This is verified safe for the existing forecast, which was the stated
constraint.** `FORECAST_HISTORY_WINDOW_DAYS` and `RUNWAY_HISTORY_WINDOW_DAYS` are
both **30**, and the confidence gate needs only `MIN_HISTORY_DAYS = 3.0`. A
90-day raw window clears the forecast's reach by 3×, so supply-runway estimates
never read a rolled-up row and their behaviour cannot change. Two rules follow
and must be honoured by anything that touches this later:

- **If the forecast window is ever widened past 90 days, the rollup must carry
  per-supply level history**, not just page counts — otherwise widening the
  window silently degrades every estimate instead of improving it.
- The rollup row must preserve `mono_count`/`color_count` alongside `page_count`,
  because F3 bills from it.

Declare the new index in `__table_args__` **and** mirror it in the migration —
revision 0001 is `create_all`, so an index declared only in a migration is
absent on fresh installs.

### F2 — Typed outbound event surface *(enabler, L)*

Signed, typed outbound events. This is what makes "recommend-only" supplies
integrable without Printer Nanny holding commercial state, and it replaces the
PSA work that was dropped. Scoped partner tokens; payloads carry no secrets and
no unescaped device strings (see S1/S2).

### F3 — Cost-per-page billing *(revenue, XL)*

Gated on F1. Rate cards per client with mono/colour split and tiers; invoice-shaped
output over the readings series.

Includes **C3**, in the form chosen: **add** `pages_period` / `mono_period` /
`color_period` columns beside the existing lifetime columns, computed with the
reset-safe `_printed_pages_for_printer`. The lifetime columns keep their exact
current meaning so no existing downstream import silently re-interprets. Also
fix **C4** here (clamp `monthly_day` to the month's last day) — a billing report
that skips February is a billing bug, not a scheduling nicety.

### F4 — Supplies recommend-only *(M)*

Per the decision: surface "order these N cartridges" in the dashboard and in
reports, driven by the forecast that already exists. **No SKU catalog, no
on-hand inventory, no PO generation, no order state machine.** Emits an F2 event
so an MSP's own ERP can act on it; a human places every order.

Calibration points worth adopting from the audit's research: Xerox ASR orders at
**2–3 weeks of usage remaining**; MPS Monitor triggers on level **OR**
days-remaining **OR** pages-remaining, and pages-remaining is nearly free here
because page counts are already collected.

### F5 — Occurrence-rate alerting *(M)*

"Not every jam, but 10 jams/day." Rides the existing alert spine (dedupe,
deadband, cooldown, suppression windows all already exist), so this is mostly a
new `AlertConditionType` plus a windowed count.

### F6 — Device security-posture report — **DONE, and this brief was wrong**

~~The data is already collected (`printers.firmware`, SNMP version in use,
TLS/cert state). This is the missing route and template: firmware currency,
insecure-SNMP exposure, cert/TLS state per device. Honest `None` where the device
exposes nothing parseable — the schema comments already commit to that.~~

**The route and template were not missing.** `/security/posture` and
`security_posture.html` shipped in `b9d08c2` on **2026-06-28**, five weeks before
this plan was written. Anyone who had built to this brief would have rebuilt a
shipped page — which is precisely the failure this document opens by warning
about, committed by this document.

What the work actually turned out to be is more interesting than a view, and
worth recording because all three defects are **one shape: reporting absence as
safety**, which is the single thing a security report must never do.

1. **SNMPv3 was graded secure on the version alone.** USM's `noAuthNoPriv` level
   is v3 with authentication *and* privacy switched off, and the agent polls at
   exactly that level when no `security_level` is recorded (`snmp.py`:
   `level = (params.v3_security_level or "noAuthNoPriv")`). So a subnet set to
   v3 and never configured earned a **green "authenticated" badge over an
   unauthenticated cleartext session**. Graded three ways now: cleartext
   (v1/v2c or v3 noAuthNoPriv) / authenticated (authNoPriv) / encrypted
   (authPriv).
2. **It cried wolf on v2c.** Every v1/v2c device was flagged `insecure-snmp` in
   red, which is not a claim we can support: SET is the attack that reconfigures
   a printer, we never attempt one, and current firmware commonly refuses writes
   while still answering reads — HP's Secure by Default (FutureSmart 4.5+) does
   precisely that. A technician sent to fix a hardened printer finds nothing and
   stops believing the next report. The finding is `snmp-cleartext` now and says
   only what we know: **our** polling credential and readings cross the customer
   VLAN unencrypted. The page states its own scope.
   The genuinely assertable credential finding is new and needs no inference
   about the device at all: **`snmp-default-community`**, raised when the
   community string in *our own* subnet row is still a vendor default. v1/v2c
   only — a leftover `public` on a v3 subnet is a credential nothing uses. The
   community string is **never a column**: "is it a default" is the finding, the
   value is a live credential, and this file gets mailed around.
3. **`firmware-unknown` was counted in `flagged`** alongside real exposures. "We
   cannot see this" and "this is exposed" are different claims, and merging them
   is how a security report becomes noise. Firmware is a visibility column with
   its own counter now and never enters `flags`.

Added on top: `?client_id=` scoping for staff (remediation happens one customer
at a time; an unknown or non-numeric id degrades to the whole fleet rather than
erroring — it is a filter, not a route), and a CSV export.

**The export is staff-only by 403, not by tenant-scoping**, and that asymmetry is
deliberate. Every *other* export pins a `client_readonly` user to their own
client and serves the file; `posture.csv` returns 403. An export the UI refuses
to show is a tenancy hole with a filename, reachable by guessing a URL. The
report is an MSP remediation worklist — every finding is about our own monitoring
config, and every fix lives on `/manage/agents`, which `client_readonly` cannot
open. Publishing it to the portal is a separate decision with wording written for
that audience, not a flag flip.

One process note worth keeping, because it is what parallel agents in separate
worktrees do to a codebase: `central/csv_safe.py` was written **twice**,
independently. It first landed with S2 (`9e76ecb`); the posture agent branched
from `origin/main` (`worktree.baseRef` defaults to `fresh`) and rebuilt it
because it needed the same protection for its own export. The branch's version
was kept — its numeric exemption is a closed-form decimal grammar rather than
`float()`, which matters because `float()` accepts `-inf`, `-nan` and `-1_0` —
and the one assertion only the duplicate's tests carried was ported across:
`None` must export as a **blank cell**, never the string `"None"`, since the
billing CSV relies on blank-not-zero to avoid billing a missing meter as zero.
Two agents converging on the same module is the cost of the parallelism; picking
the better implementation *and* porting the loser's tests is what makes it cheap
rather than lossy.

### F7 — Fleet-wide printer search *(M)*

**O3** promoted into the feature phase because it is the single most-cited
operational gap: search by IP, serial, asset tag, hostname, model and display
name across all tenants for staff, scoped for `client_readonly`.

### F8 — Yield-gap and non-OEM detection *(M–L)* — **DONE**

`central/supply_yield.py`, `supply_cycles` + `supply_yield_expectations`,
`/supplies/yield` (staff only), worker job `scan_supply_cycles`, migration 0043,
59 tests. LibreNMS's trick is what it is built on: a level that **rises** is the
cartridge-change signal, and that test is `supplies.refill_boundaries`, extracted
from the depletion forecast rather than copied beside it — the forecast wants the
last boundary, this wants every one, and two implementations that disagreed would
credit a cartridge's pages to its predecessor with nothing reporting it.

Both things F1 asked of it are honoured, and one of them is where the only real
defect was. The series is rollups below `retention.effective_raw_days` and raw
readings above it — **plus raw readings below it for days no rollup covers**,
because deletion ships OFF and the rollup pass works forward from a watermark a
bounded number of printer-days per cycle, so a real install has old raw rows with
no rollup. Reading only rollups there discarded that history in silence: the
cartridges with the most history were measured over the least of it. Every unit
test passed over it; the required end-to-end smoke against a seeded DB is what
found it.

**Expected yield is BOTH sources, with the source stated on every row.** An
operator-entered datasheet figure per model tag (matched as a case-insensitive
substring, longest wins, exact tie refused — the driver-package rule) beats a
fleet-derived MEDIAN across *other* printers of the same model. The subject is
excluded from its own baseline, or a printer running non-OEM calibrates the
expectation to itself; and the baseline's real weakness — a fleet where every
unit of a model runs non-OEM calibrates to non-OEM and finds nothing — is stated
in the UI rather than buried.

**Minimum confidence: 3 completed cartridges**, each having consumed ≥60% of its
level, before any verdict; below that the row reads *insufficient data* and shows
the measurement without a conclusion. A missing expectation reads *no expected
yield*, never *within expected* — the same correction §5's F6 needed, applied
before it could be made. The flag is presented as "yield below expected" at a
conservative default 30% shortfall, operator-settable, with the non-OEM reading
offered as an interpretation beside the things that equally explain it.

### F9 — Per-client white-label *(M)*

Branding is currently global (`app.name`, `app.logo_url`, `app.primary_color` in
`runtime.py`). Make it per-client for the portal, falling back to global.

### F10 — Server-pushed device/model definitions *(M)*

So a new printer model does not require an agent release. Definitions are signed
and validated centrally; the agent treats them as data, never as code.

### F11 — Collector redundancy *(M)*

A second agent able to cover a subnet when the primary stops reporting. The
worker already has leader election and a standby concept to model this on
(`worker/health.py`); **agents** have nothing equivalent. PrintFleet's own figure:
10–20% of collectors stop working at some point.

### F12 — Remote hands: EWS proxy, writes where supported *(XL, last)* — **DONE**

Confirmed absent 2026-08-03: no module, no route, no migration, no test. This is
the documented cut point (§8: "if the program has to be cut short, cut F12
first"), and the product is currently in the strictly read-only posture the audit
called strategically safe. Its preconditions are now all met — phase 1 is done,
Postgres is in CI, and every other feature except F8 has landed — so the
sequencing argument for doing it last is satisfied rather than outstanding.

**Deliberately last, because it is the only item that widens the blast radius.**
It lands after the spine is hardened (phase 1), covered on Postgres (D2), and
after every other feature is in — so a regression here is isolable.

Per the decision: **write actions where the device supports them, falling back to
read-only otherwise.** That fallback is the whole design, and it must be a
*probe*, never an assumption:

- Capability is **detected per device and recorded**, not inferred from brand or
  model. A device that has not been proven writable is read-only.
- Read-only EWS proxying is the baseline path and must work standalone — if
  capability detection fails, the feature degrades to the proxy, it does not fail
  closed to nothing and does not fail open to attempting writes.
- Every write is **authorised, tenant-scoped and audited** with the actual
  operation, per CLAUDE.md's audit rule. Writes are an operator action, never
  something a poll performs.
- HP's "Secure by Default" disables SNMP **writes** while keeping reads — so
  read-only-fallback is the common case on hardened fleets, not the edge case.
  Build and test it as the primary path.

**Versioning during phase 2:** minor bump per feature landing
(`0.29.0`, `0.30.0`, …). Agent version moves independently and only when
`agent/` actually changes — F10 and F11 will move it; F3–F9 will not.

*Outcome: central reached **0.30.0** and agent **0.17.0**. The independence rule
held — the agent line moved only for F10/F11, which is exactly what it is for.*

---

## 6. Phase 3 — complete bug pass + hardware verification — **6a DONE; 6b NOT STARTED**

### 6a. Every remaining item from §3 — **DONE**

> **All eleven closed, re-verified 2026-08-04 against `8ca354d` by reading the
> code.** S3 `central/csrf.py` (app-level dependency, plus a `POST /download`
> beside the GET); S4 key safety no longer consults the backend —
> `resolve_secret_key` *generates* rather than warns; S5 sign-in throttle +
> `must_change_password`; C5 `level_pct` coerced, not bounded; C6 `next_due`
> cleared so the alert resolves; D3 `tests/test_schema_drift.py`; D4
> `migrations/guard.py`; O4 `queries.audit_page`; O5 five grouped aggregates
> (measured 1002 → 5 statements); O6 `_due_deliveries(limit=BATCH_LIMIT)`;
> O7 `central/freshness.py`. The two extras below are closed too: zero
> `confirm()` calls now interpolate into an `on*` attribute, and
> `central/main.py` declares `app` once.
>
> The text below is the brief as written, kept for its sequencing rationale.

**Still open, all of it**, re-verified against `4c68f40` on 2026-08-03:
**S3, S4, S5, C5, C6, D3, D4, O4, O5, O6, O7** — with C6's mechanism corrected in
§3 and D3's phrasing corrected there too. O1/O3 were consumed by F1/F7 as
planned; C3/C4 by F3. Plus what the feature phase uncovered and did not fix:

- **Fourteen `confirm()` dialogs still interpolate operator or device text into
  an `on*` attribute**, where the HTML parser decodes entities before the
  attribute is compiled as script. The count **grew** during phase 2 (it was
  eleven) because three of the new pages repeated the pattern. Two safe shapes
  exist in-tree to copy: a `data-` attribute read via `dataset`
  (`client_manage.html`) or `{{ value|tojson }}` concatenated in
  (`events.html`).
- **The duplicated `app = FastAPI(...)` in `central/main.py:54-55`**, a merge
  artefact from `78f3345`. Harmless today, a trap tomorrow.

Two of these deserve their sequencing called out:

- **D4 (destructive downgrades)** must be fixed before any 1.0 claim. A downgrade
  that deletes operator settings including SMTP credentials is data loss with a
  documented trigger.
- **S3 (CSRF + backup download)** is the largest remaining security item and
  touches every form; do it as one coherent pass rather than per-route.

### 6b. Hardware verification — all three rigs available — **NOT STARTED, and not startable from a container**

> **`deploy/HARDWARE-VERIFICATION.md` is the single operator-followable brief for
> everything in this subsection**, written 2026-08-03. It carries the oracle
> spec, the three guards, the premise check, an ordering of the 31 macOS items
> by setup cost (with the two Developer-ID ones grouped last), and the exit-2
> decision table. Everything below is the summary it expands.
>
> **Fixed 2026-08-04:** `python3 scripts/ipp_bisect.py --contract` — the bare
> form given below and in both `deploy/` docs — used to exit 2 and print usage,
> because argparse enforces required arguments before the `--contract` early
> return. The three arguments are now enforced by hand after it. The bare form
> works; the placeholder form still works; a run missing an argument still fails
> exactly as before.

**Windows IPP attribute search.** `scripts/ipp_bisect.py` is built, unit-tested
and has **never been run**. Read `deploy/WINDOWS-IPP-BISECT-PROMPT.md` and
`deploy/WINDOWS-MSI-TESTING.md` first — both record what has already been tried
and killed.

- The tool decides nothing itself; it needs an **oracle** the operator supplies,
  because a verdict requires a physical print job. The oracle is deliberately not
  in the repo — it spans two machines and the access path is site-specific.
- Expect **~38 physical print jobs** for a two-attribute cause (p90 44), ~70 for
  three. Memoised and journalled, so an interrupted run resumes.
- **Do not re-propose** the routed hop, the missing `application/pdf`, or
  `ipp-features-supported`. All three are tested and dead.
- `INDETERMINATE` stops the run; it is never coerced to FAIL. A trial where the
  client never queried the replay (`QUERIES: 0`) is forced inconclusive however
  it voted — that coercion is what invalidated the earlier attempt.
- The premise check runs first: importing everything must print, importing
  nothing must not. If a donor attribute *breaks* the effect, the search is
  measuring something else and one trial says so.
- Three separate harness bugs each produced a confident wrong answer before this
  worked. A result obtained without the documented guards is worthless.

**macOS — 31 open checklist items** in `deploy/MACOS-CLIENT-TESTING.md` (recounted
2026-08-03: 23 `[x]`, 31 `[ ]`): queue repair and re-addressing, login window,
fast user switching, non-English locale, directory-bound Macs, the three
vendor-driver shapes (`system`/`ppd`/`pkg`) with their path-escape refusals,
notarization (needs a Developer ID certificate), MDM push, and
uninstall/reinstall preserving the machine GUID.
`deploy/HARDWARE-VERIFICATION.md` §2 orders them into seven tiers by setup cost
— eight items need nothing beyond the Mac that already ran, and only two need
the Developer ID certificate.

**The exit-2 trap is DECIDED (2026-08-04) and half-closed.** `workstation_cli`
returned exit 2 for a refused key so a service manager would not loop, but the
plist's `KeepAlive{SuccessfulExit=false}` restarts on any non-zero exit and
**launchd cannot express "restart unless the exit code is 2"** — so the comment
described a behaviour that did not occur. Resolved with a **refused-key
sentinel** (option 3 of the four in `deploy/HARDWARE-VERIFICATION.md` Part 3),
refined so its one documented cost — a state file an operator must know to
delete — is designed out: it keys on a SHA-256 of the key, so re-minting clears
it, and it expires after 6h so an un-revoked key is not poisoned by an unchanged
fingerprint. The first refusal still exits 2 truthfully; only the pointless
repeat exits 0. Eight unit tests in `tests/test_workstation_service.py`.

**What is NOT closed is launchd's half.** Nothing here asserts that
`SuccessfulExit=false` really stops after one restart — that is exactly the shape
of proxy this codebase has mistaken for proof four times. The four observations
are now a checklist in `deploy/MACOS-CLIENT-TESTING.md`, and a restart is
**counted, not inferred**.

**The one sufficient check is a printed page.** Every proxy for "it works" has now
failed at least once, four times in this codebase. A queue that exists, lists and
converges is not a queue that prints.

### 6c. Version — **cut, on half its gate**

Currently **central 1.0.0 / agent 1.0.2**. 1.0.0 was gated on §6a **and** §6b.
§6a is done, D4 included -- the item named by name as blocking any 1.0 claim.
§6b has not run, so the version is a claim about the code and not about the
product. §0 says so plainly rather than leaving it to be discovered.

**1.0.0** for central. Agent to 1.0.0 only if `agent/` changed during the program
(F10/F11/F12 will change it; if somehow none land, the agent keeps its 0.x line —
the two version lines move independently by design). *`agent/` did change, for
F10 and F11, so the agent qualifies on that criterion.*

Bump all three central locations in lockstep: `pyproject.toml`,
`central/__init__.py`, `central/main.py` `FastAPI(version=…)`, and confirm the
dashboard footer renders it.

---

## 7. Verification protocol

**Per item, before it is considered done** — this is CLAUDE.md's standing rule,
restated because a program this long will be tempted to skip it:

1. `ruff check central agent tests scripts migrations` — the same paths CI lints.
2. `pytest` — **unpiped**. `addopts = "-q"` is already set in `pyproject.toml`, so
   adding another `-q` makes it `-qq` and suppresses the summary line; and piping
   to `tail` replaces pytest's exit code with `tail`'s. Both were hit while
   preparing this plan. Redirect to a file and read `$?` directly.
3. An end-to-end smoke on a **freshly seeded throwaway DB** — `python -m
   central.seed`, then exercise the actual feature and check real values, not
   just a 200.

~~**Baseline as of this document:** ruff clean; 1756 passed / 21 skipped / 0
failed (1777 total), exit 0; seed → worker → HTTP smoke all green on 0.27.2.~~

**Baseline re-measured 2026-08-03** on `4c68f40` (central 0.30.0 / agent 0.17.0),
Apple M2 Max (12 cores), macOS 26.5.2, Python 3.9.6, SQLite, `pytest` unpiped:

> **2534 passed / 35 skipped / 0 failed** (2569 collected) **in 189.00s (3:08)**,
> exit **0**. `ruff check central agent tests scripts migrations` clean.

Quote the machine with the number — that is CLAUDE.md's rule, and the figure
above is on a 12-core M2 Max with nothing else running. A run on a busy box has
already been misread as a regression once.

The suite grew ~45% during phase 2 (1777 → 2569 collected). The
GitHub-runner and 4-vCPU-container figures elsewhere in this repo were measured
at the old size and have **not** been re-measured; treat them as stale.

### Reproduction recipes for the three confirmed bugs

*All three are fixed; each now has a regression test that fails without the fix.
The recipes are kept because they are the cheapest way to re-prove a fix is still
in place.*

**C2 — alembic percent:**
```
DATABASE_URL="sqlite:////tmp/pct%test.sqlite3" alembic upgrade head
# ValueError: invalid interpolation syntax ... at position 60
```

**S1 — FreeScout injection:** build a `Notification` whose `printer_label` is
`<img src=x onerror=alert(1)>` and call `FreeScoutChannel.build_payload`; the
script survives unescaped in `threads[0]["text"]`.

**C1 — portal blindness:** seed two clients; give client B one open critical
alert five hours old, then give client A thirty newer open alerts. Client B's
portal renders **0** open issues. Any fix must make this recipe show 1.

Each of the three needs a regression test that fails before the fix. *All three
have one:* `tests/test_alembic_env.py`, `tests/test_channel_injection.py`,
`tests/test_portal_tenant_scoping.py`.

---

## 8. Risks — *how each one actually played out*

- **Long-lived single branch.** The chosen delivery model against a
  multi-phase program. Mitigated by weekly rebase and per-item commits; the risk
  is real and was accepted deliberately.
  *Outcome: it held, and the cost showed up as merge artefacts rather than as
  lost work — a line-union in `models.py` that spliced two classes'
  `__table_args__` together and left a `mapped_column` unclosed (caught and
  resolved by appending instead), a dropped `events` router import that 404'd a
  whole page while the suite stayed green, `csv_safe.py` written twice, and a
  duplicated `app = FastAPI(...)` still in the tree. The lesson: a merge tool
  cannot see that a conflict boundary fell mid-statement, so **resolve
  structurally, then run the app, not just the suite**.*
- **`agent/` changes retarget every running worktree** (CLAUDE.md). `central/`
  changes are safe to land while other agents work; `agent/` changes are not.
  Relevant to F10/F11/F12.
  *Outcome: F10 and F11 both moved `agent/`; the agent line went 0.16.3 →
  0.17.0.*
- **Tailwind is tree-shaken against the templates.** Any new class in a new
  template (F6, F7, F9 all add UI) is simply *absent* from the vendored CSS until
  `scripts/build-assets.sh` runs — the element renders unstyled, with nothing in
  the console and nothing to grep. Run it and commit the result.
  *Outcome: real. It needed a dedicated regeneration commit (`31e1d71`,
  "Regenerate the vendored CSS for the merged feature templates") once the
  feature templates were merged, because each agent's individual regeneration
  was tree-shaken against only its own templates.*
- ~~**The suite is ~2.5 min here** (M-series Mac; ~2.5 min on a GitHub runner,
  ~6–7 min on a 4-vCPU container).~~ **Re-measured 2026-08-03: 189s (3:08) for
  2534 passed / 35 skipped on a 12-core M2 Max.** The runner and container
  figures were measured at ~1770 tests and are stale. Quote the machine with the
  number — a run on a busy box has already been misread as a regression once.
- **F12 is the only blast-radius widener.** If the program has to be cut short,
  cut F12 first: everything before it leaves the product strictly read-only, which
  is the posture the audit called strategically safe.
  *Outcome (corrected 2026-08-04): **F12 WAS built** — `central/remote.py`,
  `central/dashboard/remote.py`, migration `0042_remote_hands`,
  `tests/test_remote_hands.py`. So was F8 (`central/supply_yield.py`, migration
  `0043_supply_yield`). The paragraph that stood here said both were unbuilt and
  that the product was therefore still read-only. It was written from the same
  stale §0 status block this document opens by warning about, and it was the
  LAST thing a reader saw — the failure mode committed one more time, in the
  section about risks.*
