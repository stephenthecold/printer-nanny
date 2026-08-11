"""Derived, honest progress for the guided Setup surface.

Setup progress is computed from the objects the application actually uses. It
is not a second set of booleans that can drift away from the fleet. The only
stored state is an explicit bypass, which remains labelled as such and never
turns live agent or printer verification green.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central.channels import routable_channels
from central.runtime import load_settings

logger = logging.getLogger(__name__)

STEP_SUBNET = "subnet"
STEP_AGENT = "agent"
STEP_PRINTER = "printer"
STEP_NOTIFICATIONS = "notifications"
SITE_STEPS = frozenset({STEP_SUBNET, STEP_AGENT, STEP_PRINTER})
GLOBAL_STEPS = frozenset({STEP_NOTIFICATIONS})
ALL_STEPS = SITE_STEPS | GLOBAL_STEPS


def bypass_key(step: str, site_id: Optional[int] = None) -> str:
    """Return the canonical uniqueness key after validating its scope."""
    if step in SITE_STEPS and site_id is not None:
        return f"site:{site_id}:{step}"
    if step in GLOBAL_STEPS and site_id is None:
        return f"global:{step}"
    raise ValueError("That setup step is not valid for this scope.")


def normalize_reason(value: str) -> str:
    """Keep an operator reason single-line and bounded before storing/auditing."""
    return " ".join((value or "").split())[:500]


def _aware_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _step(
    *,
    key: str,
    label: str,
    guidance: str,
    complete: bool,
    bypass: Optional[m.SetupBypass],
    href: str,
    action: str,
) -> dict:
    state = "complete" if complete else ("bypassed" if bypass is not None else "open")
    return {
        "key": key,
        "label": label,
        "guidance": guidance,
        "state": state,
        "complete": complete,
        "bypassed": bypass is not None and not complete,
        "bypass": bypass if not complete else None,
        "href": href,
        "action": action,
    }


def _channel_has_destination(candidate) -> bool:
    """Does a routable channel have the minimum fields needed to send?

    Channel ``send`` methods intentionally support credential-less dry runs.
    Those are useful for demos but are not completed setup: a green checklist
    must mean a message could actually leave the application.
    """
    channel = candidate.channel
    if channel.type == "email":
        recipients = channel.config.get("to") or channel.runtime.get(
            "email.default_recipients"
        )
        return bool(
            recipients
            and channel.setting("smtp.host")
            and channel.setting("smtp.from")
        )
    if channel.type == "freescout":
        return bool(
            channel.setting("freescout.base_url")
            and channel.setting("freescout.api_key")
        )
    if channel.type in {"teams", "slack"}:
        return bool(channel.setting(f"{channel.type}.webhook_url"))
    if channel.type == "webhook":
        return bool(channel.setting("webhook.url"))
    return False


def _subnet_has_polling_credentials(subnet: m.Subnet) -> bool:
    """Whether this subnet contains the minimum credentials for its SNMP mode."""
    version = str(subnet.snmp_version or "").lower().lstrip("v")
    if version in {"1", "2", "2c"}:
        return bool((subnet.snmp_community or "").strip())
    if version != "3":
        return False
    creds = subnet.snmp_v3 or {}
    level = creds.get("security_level") or "noAuthNoPriv"
    if not str(creds.get("user") or "").strip():
        return False
    if level in {"authNoPriv", "authPriv"} and not creds.get("auth_password"):
        return False
    if level == "authPriv" and not creds.get("priv_password"):
        return False
    return level in {"noAuthNoPriv", "authNoPriv", "authPriv"}


def build_setup_status(db: Session, now: Optional[datetime] = None) -> dict:
    """Build the fleet setup checklist with a fixed number of database reads.

    No SNMP communities, v3 credentials, agent API keys, or channel credentials
    are returned. This dictionary is safe to render on staff-only pages.
    """
    now = _aware_utc(now) or datetime.now(timezone.utc)
    runtime = load_settings(db)
    try:
        agent_grace = max(1, int(runtime.get("alerts.offline_grace_seconds", 300)))
    except (TypeError, ValueError):
        agent_grace = 300
    try:
        printer_grace = max(
            60, int(runtime.get("alerts.printer_offline_minutes", 30)) * 60
        )
    except (TypeError, ValueError):
        printer_grace = 1800

    clients = list(db.scalars(select(m.Client).order_by(m.Client.name)))
    sites = list(db.scalars(select(m.Site).order_by(m.Site.name)))
    agents = list(db.scalars(select(m.Agent)))
    subnets = list(db.scalars(select(m.Subnet)))
    printers = list(db.scalars(select(m.Printer)))
    bypasses = list(db.scalars(select(m.SetupBypass)))

    sites_by_client: dict[int, list[m.Site]] = {}
    for site in sites:
        sites_by_client.setdefault(site.client_id, []).append(site)
    agents_by_site: dict[int, list[m.Agent]] = {}
    for agent in agents:
        agents_by_site.setdefault(agent.site_id, []).append(agent)
    subnets_by_site: dict[int, list[m.Subnet]] = {}
    for subnet in subnets:
        subnets_by_site.setdefault(subnet.site_id, []).append(subnet)
    printers_by_site: dict[int, list[m.Printer]] = {}
    for printer in printers:
        printers_by_site.setdefault(printer.site_id, []).append(printer)
    bypass_by_key = {row.key: row for row in bypasses}

    try:
        notifications_ready = any(
            _channel_has_destination(candidate)
            for candidate in routable_channels(db, runtime)
        )
    except Exception:
        # A malformed channel is incomplete setup, not a reason to make the
        # dashboard unavailable. Values are deliberately absent from the log.
        logger.warning("Notification setup could not be evaluated", exc_info=True)
        notifications_ready = False

    notification_bypass = bypass_by_key.get(bypass_key(STEP_NOTIFICATIONS))
    global_steps = [
        _step(
            key=STEP_NOTIFICATIONS,
            label="Send technician notifications",
            guidance=(
                "Configure at least one destination so printer and agent issues "
                "reach a technician."
            ),
            complete=notifications_ready,
            bypass=notification_bypass,
            href="/settings?group=notifications",
            action="Configure notifications",
        )
    ]

    client_rows = []
    site_rows = []
    missing_locations = []
    for client in clients:
        client_sites = sites_by_client.get(client.id, [])
        if not client_sites:
            missing_locations.append({"client_id": client.id, "client_name": client.name})
        rendered_sites = []
        for site in client_sites:
            site_agents = agents_by_site.get(site.id, [])
            site_subnets = subnets_by_site.get(site.id, [])
            ready_subnets = [
                subnet for subnet in site_subnets
                if _subnet_has_polling_credentials(subnet)
            ]
            site_printers = printers_by_site.get(site.id, [])
            approved = [
                p for p in site_printers
                if p.discovery_state == m.DiscoveryState.approved
            ]
            pending = sum(
                p.discovery_state == m.DiscoveryState.pending for p in site_printers
            )

            step_rows = [
                _step(
                    key=STEP_SUBNET,
                    label=(
                        "Finish network credentials"
                        if site_subnets and not ready_subnets
                        else "Define the location network"
                    ),
                    guidance=(
                        "SNMPv3 needs its security name and any authentication or "
                        "privacy credentials before this subnet can be polled."
                        if site_subnets and not ready_subnets
                        else "The subnet tells the agent where to discover and poll printers."
                    ),
                    complete=bool(ready_subnets),
                    bypass=bypass_by_key.get(bypass_key(STEP_SUBNET, site.id)),
                    href="/manage/agents",
                    action=(
                        "Finish credentials"
                        if site_subnets and not ready_subnets
                        else "Add subnet"
                    ),
                ),
                _step(
                    key=STEP_AGENT,
                    label="Enroll the location agent",
                    guidance="The agent polls printers inside this location and reports to central.",
                    complete=bool(site_agents),
                    bypass=bypass_by_key.get(bypass_key(STEP_AGENT, site.id)),
                    href="/manage/agents",
                    action="Open agent setup",
                ),
                _step(
                    key=STEP_PRINTER,
                    label="Approve a printer",
                    guidance="An approved printer is required before monitoring can be verified.",
                    complete=bool(approved),
                    bypass=bypass_by_key.get(bypass_key(STEP_PRINTER, site.id)),
                    href="/approvals" if pending else "/printers",
                    action="Review discoveries" if pending else "Open printers",
                ),
            ]

            live_agents = []
            for agent in site_agents:
                heartbeat = _aware_utc(agent.last_heartbeat)
                age = (now - heartbeat).total_seconds() if heartbeat else None
                if (
                    agent.status == m.AgentStatus.online
                    and age is not None
                    and 0 <= age <= agent_grace
                ):
                    live_agents.append(agent)
            recent_printers = []
            for printer in approved:
                last_seen = _aware_utc(printer.last_seen)
                age = (now - last_seen).total_seconds() if last_seen else None
                if age is not None and 0 <= age <= printer_grace:
                    recent_printers.append(printer)

            verified = bool(live_agents and recent_printers)
            if verified:
                verification = "Agent and printer poll verified"
            elif not live_agents:
                verification = "Waiting for a current agent reply"
            else:
                verification = "Waiting for a successful printer poll"

            row = {
                "client_id": client.id,
                "client_name": client.name,
                "site_id": site.id,
                "site_name": site.name,
                "address": site.address,
                "contact": site.contact,
                "steps": step_rows,
                "verified": verified,
                "verification": verification,
                "pending_printers": pending,
                "optional_complete": bool(site.address and site.contact),
            }
            row["needs_attention"] = bool(
                not verified or any(step["state"] != "complete" for step in step_rows)
            )
            rendered_sites.append(row)
            site_rows.append(row)
        client_rows.append({"client": client, "sites": rendered_sites})

    required_steps = list(global_steps)
    for row in site_rows:
        required_steps.extend(row["steps"])
    # A client with no location cannot acquire site-scoped monitoring objects.
    # It is a real incomplete requirement, but is not bypassable because there
    # is no operational scope to attach the exception to yet.
    structural_open = len(missing_locations) + (1 if not clients else 0)
    open_count = sum(step["state"] == "open" for step in required_steps) + structural_open
    bypassed_count = sum(step["state"] == "bypassed" for step in required_steps)
    complete_count = sum(step["state"] != "open" for step in required_steps)
    total_count = len(required_steps) + structural_open

    return {
        "clients": client_rows,
        "global_steps": global_steps,
        "missing_locations": missing_locations,
        "has_clients": bool(clients),
        "site_count": len(site_rows),
        "attention_sites": [row for row in site_rows if row["needs_attention"]],
        "ready_sites": [row for row in site_rows if not row["needs_attention"]],
        "required_total": total_count,
        "complete_count": complete_count,
        "open_count": open_count,
        "bypassed_count": bypassed_count,
        "verification_attention": sum(not row["verified"] for row in site_rows),
        "all_configured": open_count == 0,
    }
