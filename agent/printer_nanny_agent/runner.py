"""Agent orchestration: heartbeat, poll approved targets, discover subnets,
and act on commands pulled from central. Backend is injectable for testing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from printer_nanny_agent import __version__
from printer_nanny_agent.client import CentralClient
from printer_nanny_agent.config import AgentConfig, merge_remote
from printer_nanny_agent.discovery import discover_subnet
from printer_nanny_agent.mdns import assign_subnet_cidr, discover_mdns, mdns_available
from printer_nanny_agent.poller import poll_printer
from printer_nanny_agent.snmp import PysnmpBackend, SnmpBackend, SnmpError, SnmpParams

log = logging.getLogger("printer_nanny_agent.runner")

_POLL_CONCURRENCY = 16


def _due(last: Optional[float], interval: float, now: float) -> bool:
    """True if an action is due. last=None means 'never run' -> due immediately,
    independent of the monotonic clock's epoch (not zero-based on all platforms)."""
    return last is None or (now - last) >= interval


def _params_for_target(target: dict, config: AgentConfig) -> SnmpParams:
    base = config.snmp
    return SnmpParams(
        community=target.get("snmp_community") or base.community,
        version=target.get("snmp_version") or base.version,
        port=base.port,
        timeout=base.timeout,
        retries=base.retries,
    )


async def poll_targets(client: CentralClient, backend: SnmpBackend, config: AgentConfig) -> dict:
    targets = await client.get_targets()
    if not targets:
        log.info("no approved targets to poll")
        return {"polled": 0, "applied": 0, "unreachable": 0}

    sem = asyncio.Semaphore(_POLL_CONCURRENCY)
    unreachable = 0

    async def poll_one(target: dict) -> Optional[dict]:
        nonlocal unreachable
        async with sem:
            try:
                return await poll_printer(backend, target["ip"], _params_for_target(target, config))
            except SnmpError as exc:
                unreachable += 1
                log.warning("poll failed for %s: %s", target["ip"], exc)
                return None

    readings = [r for r in await asyncio.gather(*(poll_one(t) for t in targets)) if r]
    applied = 0
    if readings:
        result = await client.post_readings(readings)
        applied = result.get("applied", 0)
    log.info("polled %d target(s), %d applied, %d unreachable", len(targets), applied, unreachable)
    return {"polled": len(targets), "applied": applied, "unreachable": unreachable}


async def discover_all(client: CentralClient, backend: SnmpBackend, config: AgentConfig) -> dict:
    """Run every discovery channel in parallel:

      * SNMP sweep across each configured subnet (works cross-VLAN through
        tunnels because it's unicast).
      * mDNS browse on the local subnet (hands-off; picks up printers that
        Bonjour/Avahi advertise without any configuration). Only emits hits
        whose IP falls inside one of the configured subnets, so a printer
        advertised by some unrelated device on the agent's LAN doesn't
        accidentally land in central as "pending".

    mDNS results are merged into the same /discovered batch -- central
    dedupes by (site_id, ip), so a printer seen by both channels lands once.
    """
    new_pending = 0
    known_cidrs = [subnet.cidr for subnet in config.subnets]
    snmp_devices: List[dict] = []
    for subnet in config.subnets:
        devices = await discover_subnet(backend, subnet.cidr, config.snmp_for(subnet))
        snmp_devices.extend(devices)

    mdns_devices: List[dict] = []
    if mdns_available():
        for device in await discover_mdns():
            cidr = assign_subnet_cidr(device, known_cidrs)
            if cidr is None:
                # Outside any configured subnet -- ignore (likely the agent's own
                # WAN-facing interface or a printer the operator hasn't asked us
                # to monitor).
                log.debug(
                    "mDNS device %s is outside configured subnets, skipping",
                    device.get("ip"),
                )
                continue
            device["subnet_cidr"] = cidr
            mdns_devices.append(device)
    else:
        log.debug(
            "mDNS unavailable (zeroconf not installed) -- relying on SNMP sweep only"
        )

    # Dedupe by IP within this batch -- SNMP wins where both channels saw the
    # device (richer brand/model fingerprint).
    by_ip: dict[str, dict] = {}
    for d in mdns_devices:
        by_ip[d["ip"]] = d
    for d in snmp_devices:
        by_ip[d["ip"]] = d  # SNMP overrides mDNS on conflict
    devices = list(by_ip.values())
    # Strip internal-only fields before pushing -- central's DiscoveredIn
    # schema doesn't know about ``_mdns_services``.
    for d in devices:
        d.pop("_mdns_services", None)

    if devices:
        result = await client.post_discovered(devices)
        new_pending += result.get("new_pending", 0)
    log.info(
        "discovery added %d new pending device(s) (snmp=%d, mdns=%d, total=%d)",
        new_pending, len(snmp_devices), len(mdns_devices), len(devices),
    )
    return {"new_pending": new_pending}


async def _effective_config(client: CentralClient, config: AgentConfig) -> AgentConfig:
    """Fetch central-managed config and overlay it; fall back to local on error."""
    try:
        remote = await client.get_config()
        return merge_remote(config, remote)
    except Exception as exc:  # noqa: BLE001 - never let a config fetch stop a cycle
        log.warning("could not fetch central config, using local: %s", exc)
        return config


async def poll_one_target(
    client: CentralClient, backend: SnmpBackend, config: AgentConfig, ip: str
) -> dict:
    """Poll one printer by IP and push its reading. Used by the poll_printer command.

    Looks the target up in /targets so the per-printer SNMP creds are honored;
    falls back to the agent's defaults if the IP isn't in the approved list
    (e.g. the operator clicked Poll-now on a printer they just unignored).
    """
    targets = await client.get_targets()
    target = next((t for t in targets if t.get("ip") == ip), {"ip": ip})
    try:
        reading = await poll_printer(backend, ip, _params_for_target(target, config))
    except SnmpError as exc:
        log.warning("poll_printer failed for %s: %s", ip, exc)
        return {"polled": 1, "applied": 0, "unreachable": 1}
    result = await client.post_readings([reading])
    applied = result.get("applied", 0)
    log.info("poll_printer %s -> applied=%d", ip, applied)
    return {"polled": 1, "applied": applied, "unreachable": 0}


async def handle_commands(
    client: CentralClient, backend: SnmpBackend, config: AgentConfig, commands: List[dict]
) -> None:
    for cmd in commands:
        ctype = cmd.get("type")
        log.info("handling command #%s: %s", cmd.get("id"), ctype)
        if ctype == "rescan":
            await discover_all(client, backend, config)
        elif ctype == "poll_now":
            await poll_targets(client, backend, config)
        elif ctype == "poll_printer":
            payload = cmd.get("payload") or {}
            ip = payload.get("ip")
            if ip:
                await poll_one_target(client, backend, config, ip)
            else:
                log.warning("poll_printer command #%s missing 'ip' in payload", cmd.get("id"))
        elif ctype == "update_config":
            # Config is file-managed; log and rely on the operator/automation to apply.
            log.info("update_config requested (payload: %s) -- apply via config file", cmd.get("payload"))
        elif ctype == "update_agent":
            # Self-update: pip install the new agent package then exit so the
            # service manager (systemd / NSSM) restarts us against the
            # freshly-installed code. Imported lazily so unit tests that don't
            # exercise update don't pull in subprocess/asyncio machinery.
            from printer_nanny_agent.updater import perform_self_update
            payload = cmd.get("payload") or {}
            await perform_self_update(payload.get("pip_source"))
        else:
            log.warning("unknown command type: %s", ctype)


async def run_once(config: AgentConfig, backend: Optional[SnmpBackend] = None) -> dict:
    """One full cycle: heartbeat, commands, poll, discover. Returns a summary."""
    backend = backend or PysnmpBackend()
    client = CentralClient(
        config.central_url, config.agent_id, config.api_key, verify_tls=config.verify_tls
    )
    try:
        await client.heartbeat(__version__)
        config = await _effective_config(client, config)
        commands = await client.get_commands()
        await handle_commands(client, backend, config, commands)
        poll = await poll_targets(client, backend, config)
        disc = await discover_all(client, backend, config)
        return {"commands": len(commands), **poll, **disc}
    finally:
        await client.aclose()
        await backend.close()


async def run_forever(config: AgentConfig, backend: Optional[SnmpBackend] = None) -> None:
    """Long-running loop. Heartbeats every interval; polls/discovers on their cadence."""
    backend = backend or PysnmpBackend()
    client = CentralClient(
        config.central_url, config.agent_id, config.api_key, verify_tls=config.verify_tls
    )
    # None = "due now" so the first cycle always polls + discovers, regardless of
    # the monotonic clock's epoch (which is not zero-based on every platform).
    last_poll = None
    last_discovery = None
    effective = config
    log.info("agent %d started -> %s", config.agent_id, config.central_url)
    try:
        while True:
            now = time.monotonic()
            try:
                await client.heartbeat(__version__)
                # Pull central-managed config each cycle so UI changes apply live.
                effective = await _effective_config(client, config)
                commands = await client.get_commands()
                await handle_commands(client, backend, effective, commands)
                if _due(last_poll, effective.poll_interval_seconds, now):
                    await poll_targets(client, backend, effective)
                    last_poll = now
                if _due(last_discovery, effective.discovery_interval_seconds, now):
                    await discover_all(client, backend, effective)
                    last_discovery = now
            except Exception:  # noqa: BLE001 - keep the agent alive across transient errors
                log.exception("cycle error; retrying next interval")
            await asyncio.sleep(effective.heartbeat_interval_seconds)
    finally:
        await client.aclose()
        await backend.close()
