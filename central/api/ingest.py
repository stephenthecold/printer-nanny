"""Agent -> central ingest endpoints (push readings/discovery, pull commands)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central import schemas as s
from central import services
from central.db import get_db
from central.deps import authenticated_agent, touch_heartbeat

router = APIRouter(prefix="/api/v1/agents/{agent_id}", tags=["ingest"])


def _decrypt_v3_blob(blob):
    """SNMPv3 USM passwords are encrypted at rest; the agent needs the real
    values to authenticate against printers. Decrypt them for delivery to
    the (Bearer-authenticated, HTTPS) agent. Plaintext legacy blobs pass
    through unchanged."""
    if not blob:
        return None
    from central.secrets import decrypt_value

    out = dict(blob)
    for key in ("auth_password", "priv_password"):
        if key in out:
            out[key] = decrypt_value(out[key])
    return out


@router.post("/heartbeat", response_model=s.AgentOut)
def heartbeat(
    payload: s.HeartbeatIn,
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    touch_heartbeat(
        agent,
        payload.version,
        install_path=payload.install_path,
        last_update_result=payload.last_update_result,
    )
    db.commit()
    db.refresh(agent)
    return agent


@router.post("/readings")
def post_readings(
    batch: s.ReadingsBatchIn,
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    touch_heartbeat(agent)
    served = services.sites_served_by_agent(db, agent)
    applied, skipped = 0, []
    for reading in batch.readings:
        # Look up the printer across ALL sites this agent collects for, not
        # just its home site -- the multi-client agent path.
        printer = services.apply_reading(db, served, reading)
        if printer is None:
            skipped.append(reading.ip)
        else:
            applied += 1
    db.commit()
    return {"applied": applied, "skipped_unknown": skipped}


@router.post("/discovered")
def post_discovered(
    batch: s.DiscoveredBatchIn,
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    touch_heartbeat(agent)
    created, known = 0, 0
    # Aggregate per-subnet counts so we can write each Subnet row once per batch
    # instead of N times. Devices that don't carry a subnet_cidr (legacy agents)
    # still contribute to created/known but skip the subnet-stats update.
    per_subnet: dict[str, dict[str, int]] = {}
    for device in batch.devices:
        _, was_created = services.record_discovered(db, agent, device)
        created += int(was_created)
        known += int(not was_created)
        if device.subnet_cidr:
            bucket = per_subnet.setdefault(device.subnet_cidr, {"found": 0, "new": 0})
            bucket["found"] += 1
            bucket["new"] += int(was_created)
    for cidr, counts in per_subnet.items():
        # Resolve the right (site_id, cidr) Subnet row by going via the
        # agent's assignment -- otherwise stats land on the agent's home site
        # even when the subnet actually belongs to a different customer site.
        subnet_row = services.find_subnet_for_agent_cidr(db, agent.id, cidr)
        target_site = subnet_row.site_id if subnet_row else agent.site_id
        services.update_subnet_discovery_stats(
            db, target_site, cidr, found=counts["found"], new=counts["new"]
        )
    db.commit()
    return {"new_pending": created, "already_known": known}


@router.get("/config", response_model=s.AgentConfigOut)
def get_agent_config(
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    """Central-managed config for this agent: intervals, SNMP defaults, and subnets.

    Lets the agent's local file hold only the central URL + API key -- everything
    else is set in the site UI and delivered here.
    """
    from central.runtime import load_settings

    touch_heartbeat(agent)
    rt = load_settings(db)
    subnets = db.scalars(select(m.Subnet).where(m.Subnet.agent_id == agent.id))
    db.commit()
    return s.AgentConfigOut(
        poll_interval_seconds=rt["polling.poll_interval_seconds"],
        discovery_interval_seconds=rt["polling.discovery_interval_seconds"],
        heartbeat_interval_seconds=rt["polling.heartbeat_interval_seconds"],
        snmp={
            "community": rt["snmp.community"],
            "version": rt["snmp.version"],
            "timeout": rt["snmp.timeout"],
            "retries": rt["snmp.retries"],
        },
        subnets=[
            s.AgentSubnetConfig(
                cidr=sub.cidr,
                snmp_community=sub.snmp_community,
                snmp_version=sub.snmp_version,
                bind_interface=sub.bind_interface,
                snmp_v3=_decrypt_v3_blob(sub.snmp_v3),
            )
            for sub in subnets
        ],
    )


@router.get("/targets", response_model=list[s.PollTargetOut])
def get_targets(
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    """Approved printers from every site this agent collects for, with SNMP
    params -- the poll list. Covers both the simple case (one agent per site)
    and the multi-client pattern (HQ agent serving several customer sites)."""
    touch_heartbeat(agent)
    served = services.sites_served_by_agent(db, agent)
    targets = list(
        db.scalars(
            select(m.Printer).where(
                m.Printer.site_id.in_(served),
                m.Printer.discovery_state == m.DiscoveryState.approved,
            )
        )
    )
    db.commit()
    return targets


@router.get("/device-definitions")
def get_device_definitions(
    since: str = "",
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    """The device/model definitions this agent should hold.

    VERSIONED SO NOTHING MOVES WHEN NOTHING CHANGED
    -----------------------------------------------
    ``version`` is a content digest of the scoped feed. An agent sends the
    version it holds; if it still matches, the response is
    ``{"version": v, "changed": false}`` with no definitions at all -- which is
    the steady state on every poll of every agent forever, so it is the case
    worth making free.

    WHY THE CHANGED CASE SENDS THE WHOLE SET, NOT A DELTA
    -----------------------------------------------------
    A row-level delta needs tombstones, and a missed tombstone leaves a
    *withdrawn* definition running on an agent indefinitely -- which is
    precisely the "a definition made a working printer stop working" failure the
    whole design is arranged to prevent. The set is small (bounded at
    MAX_DEFINITIONS) and only travels when it actually changed, so
    correct-by-construction costs nothing worth having.

    SCOPING
    -------
    ``device_definition_feed`` serves global definitions plus those scoped to
    clients this agent actually collects for, so one tenant's custom definition
    never reaches another's site.

    SIGNATURE
    ---------
    HMAC-SHA256 keyed on this agent's API-key hash. See
    ``central.device_definitions`` for exactly what that does and does not
    prove -- in short, it binds the feed to this credential so a cache file
    lifted from another agent fails closed. It does not excuse the agent from
    re-validating every definition, and the agent does.
    """
    from central.device_definitions import feed_version, payload_signature

    touch_heartbeat(agent)
    rows = services.device_definition_feed(db, agent)
    db.commit()

    # Rows were validated on the way in, so this is a read of stored canonical
    # form rather than a second parse. A row whose spec is somehow not a dict
    # (hand-edited database) is dropped rather than served: the agent would
    # refuse it anyway, and refusing here keeps the version digest honest.
    definitions = [row.spec for row in rows if isinstance(row.spec, dict)]
    version = feed_version(definitions)
    if since and since == version:
        return {"version": version, "changed": False}
    return {
        "version": version,
        "changed": True,
        "definitions": definitions,
        "signature": payload_signature(agent.api_key_hash or "", version, definitions),
    }


@router.get("/commands", response_model=list[s.CommandOut])
def get_commands(
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    """Return pending commands and mark them sent (at-least-once delivery)."""
    touch_heartbeat(agent)
    commands = list(
        db.scalars(
            select(m.Command).where(
                m.Command.agent_id == agent.id,
                m.Command.status == m.CommandStatus.pending,
            )
        )
    )
    now = datetime.now(timezone.utc)
    for cmd in commands:
        cmd.status = m.CommandStatus.sent
        cmd.sent_at = now
    db.commit()
    return commands
