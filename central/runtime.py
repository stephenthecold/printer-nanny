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
         "1-28 -- sent on the first worker cycle after the send hour that day"),
    Spec("reports.send_hour", "int", "Reports", "Send hour (UTC, 0-23)", 7),
    Spec("reports.recipients", "str", "Reports", "Report recipients", "",
         "Comma-separated. Leave blank to use the alert email recipients."),
    # Alerts
    Spec("alerts.low_supply_pct", "float", "Alerts", "Low-supply threshold (%)", 20.0,
         "Default supply level that counts as 'low' in the dashboard"),
    Spec("alerts.offline_grace_seconds", "int", "Alerts", "Agent offline grace (seconds)",
         _env.agent_offline_grace_seconds, "Mark an agent offline after this long without a heartbeat"),
    # Polling (pushed to agents)
    Spec("polling.poll_interval_seconds", "int", "Polling", "Poll interval (seconds)", 300),
    Spec("polling.discovery_interval_seconds", "int", "Polling", "Discovery interval (seconds)", 3600),
    Spec("polling.heartbeat_interval_seconds", "int", "Polling", "Heartbeat interval (seconds)", 60),
    # SNMP defaults (pushed to agents)
    Spec("snmp.community", "str", "SNMP defaults", "Community", "public"),
    Spec("snmp.version", "str", "SNMP defaults", "Version (1 / 2c)", "2c"),
    Spec("snmp.timeout", "float", "SNMP defaults", "Timeout (seconds)", 2.0),
    Spec("snmp.retries", "int", "SNMP defaults", "Retries", 1),
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
    # Agent install (used to build the one-line install command shown on the Agents page)
    Spec("agent.pip_source", "str", "Agent install", "pip install source",
         "git+https://github.com/stephenthecold/printer-nanny.git#subdirectory=agent",
         "Where install-agent.sh pip-installs the agent from — set to your repo after publishing"),
    Spec("agent.docker_image", "str", "Agent install", "Docker image",
         "ghcr.io/stephenthecold/printer-nanny-agent:latest",
         "Image used by the Docker install option"),
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
        ["Email (SMTP)", "Microsoft Teams", "Slack", "Webhook (generic)", "FreeScout"],
    ),
    "alerts": ("Alerts & Reports", ["Alerts", "Reports"]),
    "polling": ("Polling & SNMP", ["Polling", "SNMP defaults"]),
    "auth": ("Authentication", ["Single sign-on (OIDC)"]),
    "agents": ("Agents", ["Agent install"]),
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
    """Upsert settings from a settings-form submission.

    ``sections`` scopes the write to specs whose Spec.section is in the set --
    required for the grouped settings page, where an absent checkbox must mean
    "unchecked within THIS group", never "reset every bool on the system".
    With sections=None (programmatic callers, tests) every spec is eligible
    and an absent checkbox means False, as before.

    Secret fields left as the placeholder keep their stored value. Secrets are
    encrypted at rest (central.secrets); every save also sweeps legacy
    plaintext secret rows into encrypted form -- the lazy half of the
    encryption migration.
    """
    from central.secrets import encrypt_value, is_encrypted

    existing = {row.key: row for row in db.scalars(select(m.AppSetting))}
    for spec in SPECS:
        if sections is not None and spec.section not in sections:
            continue
        if spec.type == "bool":
            value: Any = spec.key in form  # checkbox present → checked
        elif spec.key not in form:
            continue
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
    """
    full = load_settings(db)
    return {
        key.split(".", 1)[1]: value
        for key, value in full.items()
        if key.startswith("app.")
    }


def masked_for_form(values: Dict[str, Any]) -> Dict[str, Any]:
    """Replace secret values with the placeholder so they aren't echoed to the page."""
    out = dict(values)
    for spec in SPECS:
        if spec.type == "secret" and out.get(spec.key):
            out[spec.key] = SECRET_PLACEHOLDER
    return out
