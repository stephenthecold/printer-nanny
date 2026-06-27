# Printer Nanny — Competitive Playbook

*Generated from a 12-agent analysis: 5 agents mapped the codebase internals, 5 researched
enterprise print / MPS / RMM competitors, then synthesis + an adversarial critic distilled the
result. This is a strategy document, not a commitment — the interview decisions at the bottom
shape what actually gets built.*

## How Printer Nanny stacks up

**What it already does well** (and most rivals charge a lot for): self-hosted, no inbound site
ports (outbound-only push agent), genuine multi-tenant (client → site → subnet → printer),
shared-agent cross-client collection, brand-agnostic SNMP with a vendor-provider plugin model
(Brother reads exact component-life % from the private MIB), encryption-at-rest, audit trail,
dedupe + auto-resolve alerting, a customer portal, and DB backup/restore.

**Where the enterprise field is ahead** — the recurring gaps the research surfaced:

| Gap | Who does it | Printer Nanny today |
|---|---|---|
| Closed-loop supply **ordering** (forecast → PO/auto-ship) | HP ATR, Xerox ASR, MPS Monitor, ECI Printanista/e-automate | Forecasts days-to-empty, then *only counts* them — no alert, no order |
| **Cost-per-page contract billing** (mono/color, tiers, invoices) | ECI e-automate, Printanista, MPS Monitor, Xerox MPS | Raw monthly meter CSV, no rate cards |
| **PSA/ITSM** closed-loop ticketing | Auvik, Domotz, Datto, N-able (ConnectWise/Autotask/Halo) | None; alerts hit FreeScout/webhook but never become tracked tickets |
| Per-tenant **alert routing + escalation + SLA** | N-able, ScienceLogic, NOC tooling | One alert → every global channel; routing columns exist but are dead code |
| **Predictive** yield-gap / failure / non-OEM detection | MPS Monitor, HP SDS, Lexmark Predictive | Naive two-point-slope forecast; Brother component-life collected but unused for triggers |
| **Remote actions** (EWS proxy, reboot) — cut truck rolls | Printanista RDL, Print Tracker | Read-only; command queue does rescan/poll/update only |
| **ESG / sustainability** reporting | PaperCut, Xerox Analytics, PrintReleaf | None |
| Per-client **white-label**, **SCIM**, **device security posture** | Auvik, Domotz; enterprise SSO norms | Global branding only; 3 hardcoded roles, no SCIM; no posture reporting |

**Two confirmed correctness/security issues the audit found** (not just "missing features"):
- **Cross-tenant leak:** the JSON management/reporting endpoints (`/api/v1/printers`,
  `/api/v1/reports/fleet`) require login but don't scope by the caller's `client_id` — a
  `client_readonly` session can read *every* tenant. Tenant filtering only exists in CSV exports
  and the HTML portal.
- **Silent alert drops:** a failed channel send is recorded but never retried, and dedupe
  suppresses re-notification while the alert stays open — so a transient SMTP/Slack outage drops
  the alert entirely.

## Themes

1. **Close the supplies loop** (forecast → order) — biggest economic gap, natural fit.
2. **Meter-to-money** — contract billing engine + ESG analytics over the readings series.
3. **Become a real MSP citizen** — PSA ticketing + typed event bus + partner API.
4. **Alerting maturity** — per-tenant routing, escalation, ack-resolve fix, delivery retry.
5. **Predictive depth** — yield-gap, component-life maintenance, non-OEM detection.
6. **Remote hands** — EWS reverse-proxy + (optional) SNMP-write actions.
7. **Grey-label & tenant hardening** — per-client branding + enforced API isolation.
8. **Scale, retention, compliance** — pagination, rate limits, retention/downsampling, SIEM egress.

## Prioritized features

**Quick wins (high impact / low effort)**
- Enforce tenant isolation on JSON endpoints **(S — also a security fix)**
- Persisted predicted-depletion alerts + forecast columns on `Supply` (M)
- Closed-loop PSA ticketing — ship one PSA end-to-end first (L)

**Foundational fixes the critic flagged as load-bearing**
- Channel delivery **retry + dead-letter** (fixes silent alert drops) (M)
- Per-rule/per-tenant alert **routing + escalation + ack-resolve fix** (M)
- **Regression-based forecasting** (full-segment least-squares + pages-remaining + confidence) —
  sequence *before* auto-ordering (M)
- Poller capture of **color/mono + per-function counters** — standalone enabler that unblocks
  billing, ESG nudges, and yield-gap at once (M)

**Big bets (high impact / high effort)**
- Cost-per-page **contract billing** engine with mono/color split (XL)
- **Auto-PO** push via typed order events + ERP/distributor connectors (XL)
- **EWS reverse-proxy + SNMP-write** remote actions (XL)
- Typed signed **outbound event bus + scoped partner API tokens** (L)
- **Supplies catalog/SKU + on-hand inventory** netting (L)

**Differentiated mid-effort plays**
- Component-life predictive maintenance (Brother fuser/drum/belt/PF-kit %) (M)
- Device **security-posture reporting** (firmware / insecure-SNMP / cert-TLS) (M)
- **ESG/sustainability** dashboard + duplex/color nudges (M)
- Yield-gap & **non-OEM detection** (M–L)
- Per-client **white-label** branding (M)

## What the critic said the synthesis under-weighted

- **FIPS-validated crypto is a hard ceiling.** Fernet is *not* FIPS-validated; CMMC L2/FedRAMP
  require validated modules. Keys derive from one `SECRET_KEY` env var, no KMS/rotation. The
  "regulated buyer" story is oversold without naming this.
- **SCIM** auto-deprovisioning is a named enterprise gate at 50+ users — absent.
- **Worker spine is single-instance** (one `BlockingScheduler`, no locking) and the **agent has no
  store-and-forward** — a central outage loses a cycle's readings, which silently corrupts any
  billing/meter pipeline stacked on top. Harden the spine before stacking revenue features.
- **Color/mono meter split** should be its own enabling feature — three revenue features depend on it.
- **Forecasting noise is load-bearing,** not a footnote — the slope fits only the first & last point.
- **"Closed-loop" PSA needs an inbound webhook** to reconcile PSA-side manual closes, not just outbound.
- **Device right-sizing / TCO analytics** (a top reason buyers adopt MPS) is missing entirely.

## Strategic decision points (the interview)

1. **Category ambition** — integrate-first monitoring tool vs. full MPS business platform vs. phased.
2. **Supplies loop** — build native ordering vs. emit events for the MSP's ERP vs. recommend-only.
3. **Compliance** — pursue regulated posture now (SCIM/SIEM/FIPS/KMS) vs. defer vs. posture-reporting wedge.
4. **Forecast sequencing** — upgrade the math before any ordering vs. ship the loop now vs. gate auto-ordering only.
5. **Worker spine** — harden (leader election + agent buffering) before stacking features vs. accept single-instance.
6. **Resale model** — tenant isolation/RBAC first vs. white-label first vs. bundle as one release.
7. **Remote hands** — read-only EWS proxy vs. full remote actions (SNMP-write) vs. stay strictly read-only.
