# Printer Nanny — Completion Playbook

*Written 2026-08-03 against `524136b` (central 0.27.2 / agent 0.16.3). This is the
execution plan for finishing the product: every decision needed to build it is
recorded here, so the work can proceed without further interviewing.*

The two older strategy documents in this directory are **snapshots, not backlogs**.
`audit-2026-07.md` cites fixes at 0.6.0–0.12.0 and `competitive-playbook.md` was
written before several of the features it asks for existed. Both were re-verified
against current code before this plan was written, and roughly a third of what
they list is already shipped. **Read this file, not those, for what is left.**

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
| Compliance (SCIM/SIEM/FIPS/KMS) | **Deferred entirely** |
| Version at completion | **1.0.0** |

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

**Security-posture reporting is half done**: `printers.firmware` is collected and
the schema comments reference a posture report, but no route or template renders
one. Treated below as a view-only build, not a data build.

---

## 3. Verified still open

Every item below was confirmed against current code. Three were reproduced
outright; those are marked **[reproduced]** and each has a failing-condition
recipe in §7.

### Security — device-controlled input reaching operator surfaces

| # | Item | Location | Size |
|---|---|---|---|
| S1 | **[reproduced]** FreeScout ticket body is raw HTML built from SNMP strings. A printer label of `<img src=x onerror=alert(1)>` survives unescaped into the ticket | `channels/freescout.py:36-42` | XS |
| S2 | CSV formula injection. `_csv_response` neutralises RFC 4180 quoting only — its own docstring says so — and nothing neutralises a leading `= + - @`. Affects `/api/v1/reports/export/*` and the emailed billing CSV, whose `model`/`hostname`/`serial` cells are device-controlled | `api/exports.py:64`, `reports.py:190` | XS |
| S3 | No CSRF tokens anywhere. `SameSite=lax` is the only mitigation and it does **not** cover `GET /admin/backup/download`, which exfiltrates the whole database via top-level navigation | `main.py:49`, `backup_routes.py` | M |
| S4 | `assert_secure()` only fires when `DATABASE_URL` is not SQLite, so the copy-`.env.example` path boots on a published signing key: forgeable sessions, decryptable stored secrets | `config.py:52-60` | S |
| S5 | No login rate limiting; `admin`/`admin` recreated on container start with no forced rotation | `dashboard/routes.py`, `seed.py` | S |

### Correctness

| # | Item | Location | Size |
|---|---|---|---|
| C1 | **[reproduced]** Customer portal hides live faults. `open_alerts(db, 30)` takes the newest 30 **fleet-wide**, then filters by tenant in Python — so a customer with an open critical fault sees "no open issues" whenever 30 newer alerts belong to other tenants | `dashboard/routes.py:191-194`, `queries.py:179` | S |
| C2 | **[reproduced]** `alembic upgrade head` dies on any `%` in `DATABASE_URL` — `ValueError: invalid interpolation syntax`. The offending line is redundant; line 36 already injects the URL bypassing interpolation. A percent-encoded password gives a permanently unmigrated schema while the app itself boots fine | `migrations/env.py:16` | XS |
| C3 | Monthly billing CSV emits **lifetime cumulative** meters. The reset-safe delta helper exists and is called only by the ESG rollup | `reports.py:203-219`, `queries.py:537` | M |
| C4 | `monthly_day` of 29/30/31 silently skips months — `now.day == want_dom` never matches in February. Setting 31 loses 5 of 12 billing reports a year | `reports.py:286` | XS |
| C5 | One out-of-range supply level 422s an entire ingest batch; with the spool the poisoned batch re-sends every cycle and never drains | `schemas.py:42` | S |
| C6 | "Mark serviced" on a schedule with no `interval_days` sets `next_due = None` while reporting success — the schedule silently stops recurring | `dashboard/manage.py:1531` | S |

### Deployment / infrastructure

| # | Item | Location | Size |
|---|---|---|---|
| D1 | Dockerfile fallback install list omits `authlib`, `cryptography` **and** `tzdata`, and `pip install -e . \|\| true` swallows failure — the image builds green and 500s on SSO, or silently loses tz data that quiet hours depend on | `deploy/Dockerfile:17-22` | XS |
| D2 | **CI has zero Postgres.** The BRIN index, advisory-lock leader election and `pg_dump`/`pg_restore` paths run only in production and are tested nowhere | `.github/workflows/` | M |
| D3 | `0001_baseline` is `create_all` against *current* metadata, so migration drift cannot be detected — the check passes by construction | `migrations/versions/` | M |
| D4 | Migration downgrades drop tables their upgrade did not create (`0003`, `0007`, `0012`, `0014`, `0019`); on a pre-Alembic install `downgrade 0002` deletes every operator setting including SMTP credentials | `migrations/versions/` | S |

### Scale / observability / UX

| # | Item | Location | Size |
|---|---|---|---|
| O1 | **No readings retention, rollup or partitioning.** ~52M rows/year at 500 printers. Two docstrings claim monthly partitioning that `0002` explicitly deferred | `models.py:394`, `db.py:52` | L |
| O2 | API container configures no logging at all — only `worker/run.py` calls `basicConfig`, so every `log.info()` in the API is discarded | `main.py` | XS |
| O3 | No fleet-wide printer list and no printer search anywhere. A tech handed "10.4.7.23 is jamming" must already know the client | `dashboard/` | M |
| O4 | Audit log caps at 200 rows with no offset — at 100k rows, 99,800 are permanently unreachable | `dashboard/manage.py:1620` | S |
| O5 | Unbounded dashboard queries; `per_client_rollup` is 5N+1 (~1,001 queries at 200 clients) | `queries.py` | M |
| O6 | `retry_due` has no LIMIT and no ordering — one SMTP outage can stall a whole worker cycle under the leader lock | `channels/delivery.py:450` | S |
| O7 | Nothing live-updates and there is no "data as of" timestamp — a NOC screen shows a green fleet that has been on fire for hours, authoritatively | `dashboard/templates/` | M |

---

## 4. Phase 1 — light bug pass

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

## 5. Phase 2 — the feature program

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

### F6 — Device security-posture report *(M, view-only)*

The data is already collected (`printers.firmware`, SNMP version in use, TLS/cert
state). This is the missing route and template: firmware currency, insecure-SNMP
exposure, cert/TLS state per device. Honest `None` where the device exposes
nothing parseable — the schema comments already commit to that.

### F7 — Fleet-wide printer search *(M)*

**O3** promoted into the feature phase because it is the single most-cited
operational gap: search by IP, serial, asset tag, hostname, model and display
name across all tenants for staff, scoped for `client_readonly`.

### F8 — Yield-gap and non-OEM detection *(M–L)*

Gated on F1. Compares observed pages-per-cartridge against expected yield; a
persistent gap is the non-OEM/refill signal. LibreNMS's cheap trick is worth
copying: log *"Toner X was replaced"* when a level **rises**, which is what makes
yield measurable at all.

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

### F12 — Remote hands: EWS proxy, writes where supported *(XL, last)*

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

---

## 6. Phase 3 — complete bug pass + hardware verification

### 6a. Every remaining item from §3

S3, S4, S5, C5, C6, D3, D4, O4, O5, O6, O7 — plus anything the feature phase
uncovers. O1/O3 are already consumed by F1/F7; C3/C4 by F3.

Two of these deserve their sequencing called out:

- **D4 (destructive downgrades)** must be fixed before any 1.0 claim. A downgrade
  that deletes operator settings including SMTP credentials is data loss with a
  documented trigger.
- **S3 (CSRF + backup download)** is the largest remaining security item and
  touches every form; do it as one coherent pass rather than per-route.

### 6b. Hardware verification — all three rigs available

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

**macOS — 31 open checklist items** in `deploy/MACOS-CLIENT-TESTING.md`: queue
repair and re-addressing, login window, fast user switching, non-English locale,
directory-bound Macs, the three vendor-driver shapes (`system`/`ppd`/`pkg`) with
their path-escape refusals, notarization (needs a Developer ID certificate),
MDM push, and uninstall/reinstall preserving the machine GUID.

**Also resolve the exit-2 trap**, currently documented as unresolved:
`workstation_cli` returns exit 2 for a refused key specifically so a service
manager will not loop — but the plist's `KeepAlive{SuccessfulExit=false}`
restarts on any non-zero exit, so on macOS it loops anyway.

**The one sufficient check is a printed page.** Every proxy for "it works" has now
failed at least once, four times in this codebase. A queue that exists, lists and
converges is not a queue that prints.

### 6c. Version

**1.0.0** for central. Agent to 1.0.0 only if `agent/` changed during the program
(F10/F11/F12 will change it; if somehow none land, the agent keeps its 0.x line —
the two version lines move independently by design).

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

**Baseline as of this document:** ruff clean; 1756 passed / 21 skipped / 0 failed
(1777 total), exit 0; seed → worker → HTTP smoke all green on 0.27.2.

### Reproduction recipes for the three confirmed bugs

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

Each of the three needs a regression test that fails before the fix.

---

## 8. Risks

- **Long-lived single branch.** The chosen delivery model against a
  multi-phase program. Mitigated by weekly rebase and per-item commits; the risk
  is real and was accepted deliberately.
- **`agent/` changes retarget every running worktree** (CLAUDE.md). `central/`
  changes are safe to land while other agents work; `agent/` changes are not.
  Relevant to F10/F11/F12.
- **Tailwind is tree-shaken against the templates.** Any new class in a new
  template (F6, F7, F9 all add UI) is simply *absent* from the vendored CSS until
  `scripts/build-assets.sh` runs — the element renders unstyled, with nothing in
  the console and nothing to grep. Run it and commit the result.
- **The suite is ~2.5 min here** (M-series Mac; ~2.5 min on a GitHub runner,
  ~6–7 min on a 4-vCPU container). Quote the machine with the number — a run on a
  busy box has already been misread as a regression once.
- **F12 is the only blast-radius widener.** If the program has to be cut short,
  cut F12 first: everything before it leaves the product strictly read-only, which
  is the posture the audit called strategically safe.
