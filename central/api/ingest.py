"""Agent -> central ingest endpoints (push readings/discovery, pull commands)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from central import collector, models as m
from central import schemas as s
from central import services
from central.db import get_db
from central.deps import authenticated_agent, touch_heartbeat

log = logging.getLogger("printer_nanny.ingest")

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


def _subnet_config(sub: m.Subnet, agent_id: int) -> s.AgentSubnetConfig:
    """One subnet's delivered config. Credentials only for the agent collecting it.

    A leased subnet is named to both eligible agents so neither is left guessing
    (and so a displaced primary is not silently pushed back onto its local
    config -- see the query above), but the SNMP community and the SNMPv3 USM
    passwords travel only to the agent that currently holds the lease. Being
    NOMINATED as a standby therefore grants no credential at all; taking over
    does, on the very heartbeat that grants the lease. Least privilege enforced
    by the same row that prevents the split brain, rather than by a second rule
    that could disagree with it.
    """
    if not collector.may_collect(sub, agent_id):
        return s.AgentSubnetConfig(cidr=sub.cidr, leased=True)
    return s.AgentSubnetConfig(
        cidr=sub.cidr,
        leased=collector.is_redundant(sub),
        snmp_community=sub.snmp_community,
        snmp_version=sub.snmp_version,
        bind_interface=sub.bind_interface,
        snmp_v3=_decrypt_v3_blob(sub.snmp_v3),
    )


@router.post("/heartbeat", response_model=s.HeartbeatOut)
def heartbeat(
    payload: s.HeartbeatIn,
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    """Liveness ping, and the one place a collection lease is renewed.

    The renewal belongs here rather than on ``/config`` for two reasons. It is a
    write, and this is the request that already means "I am alive" -- renewing a
    lease is precisely that claim, restated with consequences. And it settles
    ownership in ONE round trip, which is what lets the agent anchor its local
    deadline to a monotonic instant sampled before this request: one request,
    one grant, one provably-earlier deadline.

    ``renew_all`` cannot take a lease from another agent by construction, so
    however many agents heartbeat at once, and however stale any of them is, no
    heartbeat can displace a working collector. Only the worker moves a lease,
    and only when the holder really looks gone.
    """
    from central.runtime import load_settings

    touch_heartbeat(
        agent,
        payload.version,
        install_path=payload.install_path,
        last_update_result=payload.last_update_result,
    )
    ttl, _takeover_after, _auto = collector.lease_settings(load_settings(db))
    held = collector.renew_all(db, agent, now=datetime.now(timezone.utc), ttl_seconds=ttl)
    db.commit()
    db.refresh(agent)
    out = s.HeartbeatOut.model_validate(agent)
    out.collector = s.CollectorLeaseOut(lease_seconds=ttl, held=held)
    return out


@router.post("/readings")
def post_readings(
    batch: s.ReadingsBatchIn,
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    """Apply a batch of poll results, refusing any this agent may not collect.

    The lease is enforced here as well as at ``/targets`` because this is the
    table billing reads. ``/targets`` stops a correct agent from polling what it
    does not own; this stops an agent too old to know about leases, one running
    on a stale cached target list, and any future bug above this line from
    appending a second reading series for the same printers. Two collectors
    interleaving readings do not merely duplicate rows -- ``ts`` is stamped by
    the agent, so with any clock skew the page-count series dips and recovers,
    and ``queries._printed_pages_for_printer`` bills the recovery as fresh pages.

    A refusal is reported (``refused_not_collector``), never silent: a reading
    that vanished with no explanation is indistinguishable from a printer that
    stopped answering.
    """
    touch_heartbeat(agent)
    served = services.sites_served_by_agent(db, agent)
    # One indexed probe. Every install that has never named a standby -- which
    # is every install by default -- takes this branch and pays nothing more.
    guarded = collector.any_redundancy(db, served)
    by_site = collector.subnets_for_sites(db, served) if guarded else {}
    applied, skipped, refused = 0, [], []
    refused_subnets: set = set()
    for reading in batch.readings:
        if guarded:
            printer = services.find_printer_in_sites(
                db, served, reading.ip, reading.serial
            )
            if printer is not None:
                subnet = collector.subnet_for_ip(by_site.get(printer.site_id, []), printer.ip)
                if not collector.admits(subnet, agent.id, reading.ts):
                    refused.append(reading.ip)
                    if subnet is not None:
                        refused_subnets.add(subnet.id)
                    continue
        # Look up the printer across ALL sites this agent collects for, not
        # just its home site -- the multi-client agent path.
        printer = services.apply_reading(db, served, reading)
        if printer is None:
            skipped.append(reading.ip)
        else:
            applied += 1
    if refused:
        # One audit row per subnet per batch, not per reading: a handover can
        # refuse a whole fleet's worth at once and the trail has to stay
        # readable. Which agent pushed, and how much, is the security-relevant
        # part -- an agent pushing for a subnet it does not hold is either a
        # stale collector or a misdirected credential.
        from central.audit import record

        for subnet_id in sorted(refused_subnets):
            record(
                db, None, None, "reading.refused_not_collector",
                target=f"agent:{agent.id} subnet:{subnet_id}",
                detail=f"{len(refused)} reading(s) refused: agent does not hold the lease",
            )
        log.warning(
            "agent %s pushed %d reading(s) for subnet(s) it does not collect",
            agent.id, len(refused),
        )
    db.commit()
    return {
        "applied": applied,
        "skipped_unknown": skipped,
        "refused_not_collector": refused,
    }


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
    # A subnet with no standby is served to its assigned agent exactly as
    # before. A leased one is served to every agent ELIGIBLE for it -- primary
    # and standby -- but only the holder is given its credentials.
    #
    # Listing the ones it does not hold is not a leak, it is the fix for one:
    # ``merge_remote`` falls back to the agent's LOCAL subnet list when central
    # returns none, so serving a displaced primary an empty list would send it
    # back to its ``agent.toml`` and it would go on sweeping the subnet it had
    # just lost. Naming the subnet and marking it ``leased`` tells the agent it
    # exists and is not currently its to collect, which is exactly what it needs
    # to know -- and it needs no credentials to not sweep something.
    subnets = list(db.scalars(
        select(m.Subnet).where(
            or_(
                (m.Subnet.standby_agent_id.is_(None)) & (m.Subnet.agent_id == agent.id),
                (m.Subnet.standby_agent_id.is_not(None))
                & or_(
                    m.Subnet.agent_id == agent.id,
                    m.Subnet.standby_agent_id == agent.id,
                ),
            )
        )
    ))
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
        subnets=[_subnet_config(sub, agent.id) for sub in subnets],
    )


@router.get("/targets", response_model=list[s.PollTargetOut])
def get_targets(
    agent: m.Agent = Depends(authenticated_agent),
    db: Session = Depends(get_db),
):
    """Approved printers from every site this agent collects for, with SNMP
    params -- the poll list. Covers both the simple case (one agent per site)
    and the multi-client pattern (HQ agent serving several customer sites).

    Site scoping alone is not enough once a subnet has a standby: two agents at
    one site would each be handed the same printers and both would poll them.
    So a printer sitting in a LEASED subnet is served only to the agent holding
    that lease. A printer in an unleased subnet, or in no configured subnet at
    all, is served exactly as before.
    """
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
    if collector.any_redundancy(db, served):
        by_site = collector.subnets_for_sites(db, served)
        targets = [
            p
            for p in targets
            if collector.may_collect(
                collector.subnet_for_ip(by_site.get(p.site_id, []), p.ip), agent.id
            )
        ]
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
