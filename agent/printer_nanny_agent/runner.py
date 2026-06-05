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
from printer_nanny_agent.poller import poll_printer
from printer_nanny_agent.snmp import PysnmpBackend, SnmpBackend, SnmpError, SnmpParams

log = logging.getLogger("printer_nanny_agent.runner")

_POLL_CONCURRENCY = 16


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
    new_pending = 0
    for subnet in config.subnets:
        devices = await discover_subnet(backend, subnet.cidr, config.snmp_for(subnet))
        if devices:
            result = await client.post_discovered(devices)
            new_pending += result.get("new_pending", 0)
    log.info("discovery added %d new pending device(s)", new_pending)
    return {"new_pending": new_pending}


async def _effective_config(client: CentralClient, config: AgentConfig) -> AgentConfig:
    """Fetch central-managed config and overlay it; fall back to local on error."""
    try:
        remote = await client.get_config()
        return merge_remote(config, remote)
    except Exception as exc:  # noqa: BLE001 - never let a config fetch stop a cycle
        log.warning("could not fetch central config, using local: %s", exc)
        return config


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
        elif ctype == "update_config":
            # Config is file-managed; log and rely on the operator/automation to apply.
            log.info("update_config requested (payload: %s) — apply via config file", cmd.get("payload"))
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
    last_poll = 0.0
    last_discovery = 0.0
    effective = config
    log.info("agent %d started → %s", config.agent_id, config.central_url)
    try:
        while True:
            now = time.monotonic()
            try:
                await client.heartbeat(__version__)
                # Pull central-managed config each cycle so UI changes apply live.
                effective = await _effective_config(client, config)
                commands = await client.get_commands()
                await handle_commands(client, backend, effective, commands)
                if now - last_poll >= effective.poll_interval_seconds:
                    await poll_targets(client, backend, effective)
                    last_poll = now
                if now - last_discovery >= effective.discovery_interval_seconds:
                    await discover_all(client, backend, effective)
                    last_discovery = now
            except Exception:  # noqa: BLE001 - keep the agent alive across transient errors
                log.exception("cycle error; retrying next interval")
            await asyncio.sleep(effective.heartbeat_interval_seconds)
    finally:
        await client.aclose()
        await backend.close()
