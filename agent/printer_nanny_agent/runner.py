"""Agent orchestration: heartbeat, poll approved targets, discover subnets,
and act on commands pulled from central. Backend is injectable for testing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import List, Optional

import httpx

from printer_nanny_agent import __version__
from printer_nanny_agent.client import CentralClient
from printer_nanny_agent.config import AgentConfig, merge_remote
from printer_nanny_agent.discovery import discover_subnet
from printer_nanny_agent.mdns import assign_subnet_cidr, discover_mdns, mdns_available
from printer_nanny_agent.poller import poll_printer
from printer_nanny_agent.snmp import PysnmpBackend, SnmpBackend, SnmpError, SnmpParams
from printer_nanny_agent.spool import ReadingSpool

log = logging.getLogger("printer_nanny_agent.runner")

_POLL_CONCURRENCY = 16


def _spool_for(config: AgentConfig) -> ReadingSpool:
    return ReadingSpool(config.spool_path(), max_readings=config.spool_max_readings)


async def drain_spool(client: CentralClient, spool: ReadingSpool) -> int:
    """Replay any readings buffered during a prior central outage.

    FIFO, ack-then-remove. A failure mid-drain leaves the remainder spooled
    (drain() handles that); we just swallow the error here so a still-down
    central doesn't abort the rest of the cycle. Returns readings replayed.
    """
    try:
        return await spool.drain(client.post_readings)
    except (httpx.HTTPError, OSError) as exc:
        # Central still unreachable (or came back then dropped again). The
        # un-replayed readings stay on disk; try again next connectivity.
        log.warning("spool: replay interrupted, will retry next cycle: %s", exc)
        return 0


async def push_readings(
    client: CentralClient, spool: ReadingSpool, readings: List[dict]
) -> int:
    """Push freshly-polled readings, spooling them durably if the push fails.

    Replays any previously-spooled readings first (so order across cycles is
    preserved), then sends this cycle's batch. On a connection error / non-2xx
    the batch is written to the spool instead of being silently dropped, and we
    return 0 applied -- the readings are safe on disk for the next successful
    connection. Returns the number central reported applied.
    """
    if not readings:
        # Even with nothing fresh, a reachable central is a chance to drain.
        await drain_spool(client, spool)
        return 0
    # Drain the backlog first so central receives readings roughly in the order
    # they were taken. If the drain is interrupted it spools the remainder and
    # we still try this cycle's batch (which will also spool on failure).
    await drain_spool(client, spool)
    try:
        result = await client.post_readings(readings)
    except (httpx.HTTPError, OSError) as exc:
        spool.append(readings)
        log.warning(
            "readings push failed (%s) -- %d reading(s) spooled for retry",
            exc, len(readings),
        )
        return 0
    return result.get("applied", 0)


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


async def poll_targets(
    client: CentralClient,
    backend: SnmpBackend,
    config: AgentConfig,
    spool: Optional[ReadingSpool] = None,
) -> dict:
    spool = spool if spool is not None else _spool_for(config)
    targets = await client.get_targets()
    if not targets:
        log.info("no approved targets to poll")
        # No fresh readings, but a reachable central is a chance to flush any
        # backlog from a prior outage.
        await drain_spool(client, spool)
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
    # push_readings drains the backlog first, then either applies this cycle's
    # batch or spools it durably on failure -- a failed push never drops the
    # cycle.
    applied = await push_readings(client, spool, readings)
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
    client: CentralClient,
    backend: SnmpBackend,
    config: AgentConfig,
    ip: str,
    spool: Optional[ReadingSpool] = None,
) -> dict:
    """Poll one printer by IP and push its reading. Used by the poll_printer command.

    Looks the target up in /targets so the per-printer SNMP creds are honored;
    falls back to the agent's defaults if the IP isn't in the approved list
    (e.g. the operator clicked Poll-now on a printer they just unignored).
    """
    spool = spool if spool is not None else _spool_for(config)
    targets = await client.get_targets()
    target = next((t for t in targets if t.get("ip") == ip), {"ip": ip})
    try:
        reading = await poll_printer(backend, ip, _params_for_target(target, config))
    except SnmpError as exc:
        log.warning("poll_printer failed for %s: %s", ip, exc)
        return {"polled": 1, "applied": 0, "unreachable": 1}
    # Spool-on-failure path: a central outage during an on-demand poll buffers
    # the reading instead of dropping it.
    applied = await push_readings(client, spool, [reading])
    log.info("poll_printer %s -> applied=%d", ip, applied)
    return {"polled": 1, "applied": applied, "unreachable": 0}


async def handle_commands(
    client: CentralClient,
    backend: SnmpBackend,
    config: AgentConfig,
    commands: List[dict],
    spool: Optional[ReadingSpool] = None,
) -> None:
    # Don't build a spool eagerly here: only the poll branches use it, and
    # poll_targets/poll_one_target already default-construct one from None. An
    # update_agent / rescan / update_config command must not touch config.spool_*
    # (the self-update path is invoked with a minimal config).
    for cmd in commands:
        ctype = cmd.get("type")
        log.info("handling command #%s: %s", cmd.get("id"), ctype)
        if ctype == "rescan":
            await discover_all(client, backend, config)
        elif ctype == "poll_now":
            await poll_targets(client, backend, config, spool)
        elif ctype == "poll_printer":
            payload = cmd.get("payload") or {}
            ip = payload.get("ip")
            if ip:
                await poll_one_target(client, backend, config, ip, spool)
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


def _startup_diagnostics() -> tuple[str, Optional[dict]]:
    """Read once-per-process diagnostic fields the next heartbeat should carry.

    Returns (install_path, last_update_result). install_path is where the
    package was loaded from (helpful when self-update lands in the wrong
    site-packages). last_update_result is the marker file the updater
    writes after every attempt -- success or specific failure.
    """
    import printer_nanny_agent as _pkg
    from printer_nanny_agent.updater import read_last_update_result

    install_path = str(Path(_pkg.__file__).resolve().parent)
    return install_path, read_last_update_result()


async def run_once(config: AgentConfig, backend: Optional[SnmpBackend] = None) -> dict:
    """One full cycle: heartbeat, commands, poll, discover. Returns a summary."""
    backend = backend or PysnmpBackend()
    client = CentralClient(
        config.central_url, config.agent_id, config.api_key, verify_tls=config.verify_tls
    )
    install_path, update_result = _startup_diagnostics()
    spool = _spool_for(config)
    try:
        await client.heartbeat(
            __version__,
            install_path=install_path,
            last_update_result=update_result,
        )
        # Central answered the heartbeat -> it's reachable. Flush any readings
        # spooled during a prior outage before doing anything else this cycle.
        await drain_spool(client, spool)
        config = await _effective_config(client, config)
        commands = await client.get_commands()
        await handle_commands(client, backend, config, commands, spool)
        poll = await poll_targets(client, backend, config, spool)
        disc = await discover_all(client, backend, config)
        return {"commands": len(commands), "spooled": spool.count(), **poll, **disc}
    finally:
        await client.aclose()
        await backend.close()


async def run_forever(config: AgentConfig, backend: Optional[SnmpBackend] = None) -> None:
    """Long-running loop. Heartbeats every interval; polls/discovers on their cadence."""
    backend = backend or PysnmpBackend()
    client = CentralClient(
        config.central_url, config.agent_id, config.api_key, verify_tls=config.verify_tls
    )
    install_path, update_result = _startup_diagnostics()
    # Send install_path on every heartbeat (cheap, helps diagnose stale
    # installs), but last_update_result only on the FIRST heartbeat after
    # startup -- after that, the dashboard already has it and re-sending
    # would just churn the row.
    pending_update_result: Optional[dict] = update_result
    last_poll = None
    last_discovery = None
    effective = config
    # One spool for the process lifetime. Its max comes from the local config;
    # central's merge_remote does not (and should not) override where local
    # state lives, so the cap is stable across config refreshes.
    spool = _spool_for(config)
    log.info(
        "agent %d started -> %s (install: %s, version: %s)",
        config.agent_id, config.central_url, install_path, __version__,
    )
    try:
        while True:
            now = time.monotonic()
            try:
                await client.heartbeat(
                    __version__,
                    install_path=install_path,
                    last_update_result=pending_update_result,
                )
                pending_update_result = None  # only on the first heartbeat
                # Heartbeat succeeded -> central is up. Flush the outage backlog
                # first so spooled readings catch up before fresh ones pile on.
                await drain_spool(client, spool)
                effective = await _effective_config(client, config)
                commands = await client.get_commands()
                await handle_commands(client, backend, effective, commands, spool)
                if _due(last_poll, effective.poll_interval_seconds, now):
                    await poll_targets(client, backend, effective, spool)
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
