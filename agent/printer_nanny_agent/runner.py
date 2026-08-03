"""Agent orchestration: heartbeat, poll approved targets, discover subnets,
and act on commands pulled from central. Backend is injectable for testing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import List, Optional

import httpx

from printer_nanny_agent import __version__
from printer_nanny_agent.client import CentralClient
from printer_nanny_agent.config import AgentConfig, SubnetConfig, merge_remote
from printer_nanny_agent.discovery import discover_subnet
from printer_nanny_agent.driver_probe import DriverProbeCache, attach as attach_driver_probe
from printer_nanny_agent.mdns import assign_subnet_cidr, discover_mdns, mdns_available
from printer_nanny_agent.poller import poll_printer
from printer_nanny_agent.providers.definitions import (
    DefinitionStore,
    active,
    key_fingerprint,
    set_active,
)
from printer_nanny_agent.definitions import signature_ok
from printer_nanny_agent.snmp import PysnmpBackend, SnmpBackend, SnmpError, SnmpParams
from printer_nanny_agent.spool import ReadingSpool

log = logging.getLogger("printer_nanny_agent.runner")

_POLL_CONCURRENCY = 16

# Process-lifetime IPP probe throttle. Shared across cycles so a device is
# probed roughly daily rather than on every poll -- see driver_probe.
_DRIVER_PROBE_CACHE = DriverProbeCache()


def _spool_for(config: AgentConfig) -> ReadingSpool:
    return ReadingSpool(config.spool_path(), max_readings=config.spool_max_readings)


class CollectorLeases:
    """The collection leases central granted, expiring on this agent's own clock.

    A subnet with a standby collector is swept under a *lease*, and this is the
    half of the mechanism that central cannot enforce. Central refuses to hand
    the subnet to anyone else while the lease is live, and refuses readings from
    anyone who does not hold it -- but neither reaches an agent that has lost its
    link to central and is still reaching the printers perfectly well. Without a
    deadline the agent enforces on itself, that agent keeps sweeping, a standby
    is eventually handed the subnet, and both collect: duplicate readings, and a
    page-count series that dips and recovers, which is billed twice.

    So the deadline is measured here, and the ordering is the whole point:

        anchor = time.monotonic()          # BEFORE the request is sent
        ... heartbeat ...                  # central stamps expiry = ITS now + ttl
        deadline = anchor + ttl

    Central's ``now`` is necessarily later than the instant we sampled, because
    the request had to travel, so ``deadline <= central's expiry`` always. This
    agent therefore stops collecting *before* central will consider the lease
    free. Note what that does NOT assume: nothing about the two clocks agreeing,
    or being synchronised at all. Both sides measure a duration -- ours on a
    monotonic clock, so a step from NTP or a suspend/resume cannot lengthen it.

    ``record`` REPLACES the held set rather than merging into it. A lease this
    agent lost must disappear on the heartbeat that says so, and a merge would
    leave it in place until it timed out -- turning a clean, immediate handover
    into the maximum-length overlap the design exists to avoid.
    """

    def __init__(self) -> None:
        self._until: dict = {}

    def record(self, cidrs, ttl_seconds: float, *, anchor: float) -> None:
        """Store the deadline for every CIDR central says we hold."""
        self._until = {str(c): anchor + float(ttl_seconds) for c in (cidrs or [])}

    def holds(self, cidr: str, now: Optional[float] = None) -> bool:
        until = self._until.get(str(cidr))
        if until is None:
            return False
        return (time.monotonic() if now is None else now) < until

    def held_cidrs(self) -> List[str]:
        return sorted(self._until)


def _may_collect(subnet: SubnetConfig, leases: Optional[CollectorLeases], now: float) -> bool:
    """Whether this subnet may be swept right now.

    An unleased subnet -- every subnet with no standby, which is the shipped
    default everywhere -- is always collectable, exactly as before. A leased one
    needs a lease that has not run out. With no tracker at all (a one-shot run
    that never heartbeated) a leased subnet is refused: "I was never told I hold
    this" and "my lease expired" have the same correct answer, and guessing the
    permissive one is how both collectors end up sweeping.
    """
    if not getattr(subnet, "leased", False):
        return True
    return leases is not None and leases.holds(subnet.cidr, now)


class TargetCache:
    """The last poll list central served, kept so an outage can't stop polling.

    ``/targets`` is only readable while central is up, but readings taken during
    an outage are exactly the ones the spool exists to preserve -- with no target
    list there is nothing to poll, and the outage window becomes a permanent hole
    in the meters that nothing back-fills. One instance per process hands back the
    last successful list for as long as central is unreachable.

    Deliberately in-memory only: the list carries SNMP credentials, and an agent
    restart mid-outage is a much smaller hole than writing community strings into
    the data dir.
    """

    def __init__(self) -> None:
        self._targets: Optional[List[dict]] = None

    async def fetch(self, client: CentralClient) -> List[dict]:
        """Fresh targets from central, falling back to the last successful list."""
        try:
            targets = await client.get_targets()
        except (httpx.HTTPError, OSError) as exc:
            if self._targets is None:
                log.warning("could not fetch targets and none are cached yet: %s", exc)
                return []
            log.warning(
                "could not fetch targets (%s) -- polling the %d cached target(s)",
                exc, len(self._targets),
            )
            return list(self._targets)
        self._targets = list(targets)
        return targets


class DefinitionSync:
    """Keeps the active device/model definitions in step with central.

    Three properties this has to have, and each is a way the feature could
    otherwise make things worse rather than better:

    * **An unreachable central changes nothing.** A failed fetch keeps whatever
      is already active, exactly like ``TargetCache`` keeps the last poll list.
      The definitions describe how to read the fleet; losing them during an
      outage would degrade every reading at precisely the moment nobody can see
      it happening.
    * **A refused feed changes nothing either.** Signature mismatch, a payload
      over the byte cap, malformed JSON -- all keep the current set. Refusing
      forward into "no definitions" would let anyone who can break the response
      turn the feature off fleet-wide.
    * **A feed that legitimately went empty DOES take effect.** An operator who
      disables a definition needs it to stop being applied, and that is the
      whole reason the changed case sends the full set rather than a delta:
      there is no tombstone to miss.

    Deliberately not persisted beyond the cache file: the version is re-read
    from the cache at startup so a restart does not force a re-download.
    """

    def __init__(self, store: "DefinitionStore", api_key: str):
        self._store = store
        self._fingerprint = key_fingerprint(api_key)
        self._version = ""

    @property
    def version(self) -> str:
        return self._version

    def load_cache(self) -> int:
        """Activate the cached feed. Returns how many definitions are active."""
        payload = self._store.load(self._fingerprint)
        if payload is None:
            return 0
        count = set_active(payload.get("definitions") or [])
        self._version = str(payload.get("version") or "")
        if count:
            log.info(
                "loaded %d device definition(s) from cache (version %s)",
                count, self._version or "?",
            )
        return count

    async def refresh(self, client: CentralClient) -> Optional[int]:
        """Fetch and apply. Returns the new active count, or None for no change."""
        try:
            payload = await client.get_device_definitions(self._version)
        except Exception as exc:  # noqa: BLE001 - a feed failure must not cost a cycle
            log.warning(
                "could not fetch device definitions, keeping the %d already "
                "active: %s", len(active()), exc,
            )
            return None
        if not payload.get("changed"):
            return None

        version = payload.get("version")
        raw = payload.get("definitions")
        if not isinstance(version, str) or not isinstance(raw, list):
            log.warning("device definition feed is malformed; keeping current set")
            return None
        if not signature_ok(self._fingerprint, version, raw, payload.get("signature")):
            # Fail closed onto what we already have. The signature binds the
            # feed to this agent's own credential, so a mismatch means the
            # payload was not produced for this agent -- which is exactly the
            # case where believing it would be worst.
            log.warning(
                "device definition feed failed its signature check; keeping the "
                "%d already active", len(active()),
            )
            return None

        count = set_active(raw)
        self._version = version
        self._store.save(payload)
        log.info(
            "device definitions updated: %d active (version %s)", count, version
        )
        return count


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
        # append() reports what it could not keep (cap drops, or the whole batch
        # when the data dir is full / read-only) so this line never claims a
        # reading is safe on disk when it isn't.
        lost = spool.append(readings)
        log.warning(
            "readings push failed (%s) -- %d reading(s) spooled for retry%s",
            exc, len(readings),
            f" ({lost} could not be retained)" if lost else "",
        )
        return 0
    return result.get("applied", 0)


def _due(last: Optional[float], interval: float, now: float) -> bool:
    """True if an action is due. last=None means 'never run' -> due immediately,
    independent of the monotonic clock's epoch (not zero-based on all platforms)."""
    return last is None or (now - last) >= interval


def _subnet_for_ip(ip: Optional[str], config: AgentConfig) -> Optional[SubnetConfig]:
    """The configured subnet whose CIDR contains ``ip``, or None if it's outside
    all of them. Uses the same containment test the mDNS path uses, so a device
    is attributed to one subnet no matter which code path asks."""
    if not ip:
        return None
    cidr = assign_subnet_cidr({"ip": ip}, [s.cidr for s in config.subnets])
    return next((s for s in config.subnets if s.cidr == cidr), None)


def _params_for_target(target: dict, config: AgentConfig) -> SnmpParams:
    """SNMP params for one poll target: its subnet's credentials, with the
    printer row's own community (and version, unless the subnet speaks v3)
    layered on top.

    Discovery resolves creds through ``config.snmp_for(subnet)``; polling has to
    resolve them the same way or the two disagree -- a v3 subnet sweeps fine and
    then every poll authenticates as an empty USM user, so the printer reads
    unreachable forever. The USM credentials and the per-subnet source bind
    (which one agent needs to serve clients with overlapping CIDRs) live only on
    the subnet, so they always come from there.
    """
    subnet = _subnet_for_ip(target.get("ip"), config)
    base = config.snmp_for(subnet) if subnet is not None else config.snmp
    version = str(target.get("snmp_version") or base.version)
    if base.version == "3":
        # A printer row has no column that can carry USM credentials, so its
        # version is central's "2c" column default for everything discovered on
        # a v3 subnet. Honoring it would swap the only creds that can
        # authenticate here for a community string the device will refuse.
        version = "3"
    if version == "3" and not base.v3_user:
        log.warning(
            "%s is set to SNMPv3 but no USM user is configured for it -- set the "
            "v3 credentials on its subnet under Agents", target.get("ip"),
        )
    return replace(
        base,
        community=target.get("snmp_community") or base.community,
        version=version,
    )


def _withhold_unleased(
    targets: List[dict], config: AgentConfig, leases: Optional[CollectorLeases]
) -> tuple:
    """Split the poll list into what we may collect and what we may not.

    Only ever removes targets sitting inside a subnet this agent's config marks
    as leased and whose lease has run out. Central already filters ``/targets``
    the same way, so on a fresh fetch this is a no-op -- it earns its keep on the
    CACHED list, which is exactly the case central cannot reach: link down,
    printers still reachable, standby about to be handed the subnet.

    A target outside every configured subnet is polled, unchanged. Attributing
    it to a lease we cannot identify would silently stop collecting printers
    nobody put in a subnet row.
    """
    now = time.monotonic()
    keep, withheld = [], []
    for target in targets:
        subnet = _subnet_for_ip(target.get("ip"), config)
        if subnet is not None and not _may_collect(subnet, leases, now):
            withheld.append(target)
        else:
            keep.append(target)
    return keep, withheld


async def poll_targets(
    client: CentralClient,
    backend: SnmpBackend,
    config: AgentConfig,
    spool: Optional[ReadingSpool] = None,
    target_cache: Optional[TargetCache] = None,
    leases: Optional[CollectorLeases] = None,
) -> dict:
    spool = spool if spool is not None else _spool_for(config)
    # A default-constructed cache is empty, so it just fetches from central --
    # only the long-running loop passes a shared one that survives an outage.
    cache = target_cache if target_cache is not None else TargetCache()
    targets = await cache.fetch(client)
    targets, withheld = _withhold_unleased(targets, config, leases)
    if withheld:
        # Stated, not silent. A collector that quietly stopped sweeping half a
        # site looks identical to half a site that went down.
        log.warning(
            "not polling %d target(s): this agent no longer holds the collection "
            "lease for their subnet", len(withheld),
        )
    if not targets:
        log.info("no approved targets to poll")
        # No fresh readings, but a reachable central is a chance to flush any
        # backlog from a prior outage.
        await drain_spool(client, spool)
        return {"polled": 0, "applied": 0, "unreachable": 0, "withheld": len(withheld)}

    sem = asyncio.Semaphore(_POLL_CONCURRENCY)
    unreachable = 0

    async def poll_one(target: dict) -> Optional[dict]:
        nonlocal unreachable
        async with sem:
            try:
                reading = await poll_printer(
                    backend, target["ip"], _params_for_target(target, config)
                )
                # Ride the IPP capability probe along with the SNMP poll, at
                # most once per device per interval. Never raises: a probe
                # failure must not cost the reading it travels with.
                return await asyncio.to_thread(
                    attach_driver_probe, reading, _DRIVER_PROBE_CACHE, target["ip"]
                )
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
    return {
        "polled": len(targets),
        "applied": applied,
        "unreachable": unreachable,
        "withheld": len(withheld),
    }


async def discover_all(
    client: CentralClient,
    backend: SnmpBackend,
    config: AgentConfig,
    leases: Optional[CollectorLeases] = None,
) -> dict:
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
    # Sweeping a subnet whose lease we no longer hold would flood /discovered
    # with devices the actual collector is already reporting, and (on a trusted
    # subnet) auto-approve them a second time. The mDNS containment test below
    # is scoped to the same list, so a Bonjour hit inside a subnet we do not
    # hold is likewise not ours to report.
    now = time.monotonic()
    collectable = [s for s in config.subnets if _may_collect(s, leases, now)]
    if len(collectable) != len(config.subnets):
        log.warning(
            "skipping discovery on %d subnet(s): collection lease not held",
            len(config.subnets) - len(collectable),
        )
    known_cidrs = [subnet.cidr for subnet in collectable]
    snmp_devices: List[dict] = []
    for subnet in collectable:
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


async def _effective_config(
    client: CentralClient,
    config: AgentConfig,
    last_good: Optional[AgentConfig] = None,
) -> AgentConfig:
    """Fetch central-managed config and overlay it; fall back to last-known-good.

    ``last_good`` is the previously-merged config (None on the first cycle, where
    the local file is all we have). Reverting to the bare local file whenever
    central is unreachable would drop the central-managed subnets and intervals,
    and with them the per-subnet SNMP creds the poll cycle needs to keep
    collecting through the outage.
    """
    try:
        remote = await client.get_config()
        return merge_remote(config, remote)
    except Exception as exc:  # noqa: BLE001 - never let a config fetch stop a cycle
        log.warning("could not fetch central config, using last known good: %s", exc)
        return last_good if last_good is not None else config


async def poll_one_target(
    client: CentralClient,
    backend: SnmpBackend,
    config: AgentConfig,
    ip: str,
    spool: Optional[ReadingSpool] = None,
    leases: Optional[CollectorLeases] = None,
) -> dict:
    """Poll one printer by IP and push its reading. Used by the poll_printer command.

    Looks the target up in /targets so the per-printer SNMP creds are honored;
    falls back to the agent's defaults if the IP isn't in the approved list
    (e.g. the operator clicked Poll-now on a printer they just unignored).
    """
    spool = spool if spool is not None else _spool_for(config)
    subnet = _subnet_for_ip(ip, config)
    if subnet is not None and not _may_collect(subnet, leases, time.monotonic()):
        log.warning(
            "not polling %s: this agent no longer holds the collection lease for %s",
            ip, subnet.cidr,
        )
        return {"polled": 0, "applied": 0, "unreachable": 0, "withheld": 1}
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


async def handle_remote(
    client: CentralClient,
    backend: SnmpBackend,
    config: AgentConfig,
    ctype: str,
    payload: dict,
) -> None:
    """Carry out one remote-hands command and report the outcome, always.

    EVERY PATH REPORTS. A refusal, a transport failure, an unexpected exception
    -- each comes back as a result with a stated reason. An operator watching a
    request that never resolves cannot tell "the agent declined" from "the agent
    is gone", and this codebase has paid for absence-read-as-something before.

    Deliberately NOT gated on the collection lease. The lease governs who
    *collects*, because two collectors interleaving readings corrupt the page
    series that billing reads. A remote request produces no reading, and central
    names exactly one agent per request, so there is nothing here for a lease to
    prevent -- gating on it would only mean a displaced primary silently
    ignoring an operator's button.
    """
    from printer_nanny_agent import remote as rh

    request_id = payload.get("request_id")
    if not request_id:
        log.warning("%s command with no request_id; nothing to report to", ctype)
        return

    try:
        if ctype == "remote_fetch":
            result = await rh.fetch_page(payload)
        else:
            ip = payload.get("ip")
            # Resolve this printer's own SNMP credentials the same way an
            # on-demand poll does, so a v3 subnet or a per-printer community is
            # honoured here exactly as it is during polling. Falling back to the
            # agent defaults would make a probe report "read-only" for a device
            # that is merely authenticating differently.
            try:
                targets = await client.get_targets()
            except Exception as exc:  # noqa: BLE001 - central down; use local config
                log.warning("could not fetch targets for remote command: %s", exc)
                targets = []
            target = next((t for t in targets if t.get("ip") == ip), {"ip": ip})
            params = _params_for_target(target, config)
            if ctype == "remote_probe":
                result = await rh.probe_writable(backend, ip, params)
            elif ctype == "remote_write":
                result = await rh.perform_write(backend, payload, params)
            else:  # pragma: no cover - guarded by the dispatcher above
                result = {"ok": False, "error": "unknown remote command %s" % ctype}
    except Exception as exc:  # noqa: BLE001 - a refusal or a bug must still report
        result, why = rh.result_for_exception(exc)
        log.warning("remote command %s %s: %s", ctype, why, exc)

    try:
        await client.post_remote_result(int(request_id), result)
    except Exception as exc:  # noqa: BLE001 - central unreachable; the request expires
        log.warning("could not report remote result #%s: %s", request_id, exc)


async def handle_commands(
    client: CentralClient,
    backend: SnmpBackend,
    config: AgentConfig,
    commands: List[dict],
    spool: Optional[ReadingSpool] = None,
    leases: Optional[CollectorLeases] = None,
) -> None:
    # Don't build a spool eagerly here: only the poll branches use it, and
    # poll_targets/poll_one_target already default-construct one from None. An
    # update_agent / rescan / update_config command must not touch config.spool_*
    # (the self-update path is invoked with a minimal config).
    for cmd in commands:
        ctype = cmd.get("type")
        log.info("handling command #%s: %s", cmd.get("id"), ctype)
        # An operator-triggered sweep or poll is subject to the same lease as a
        # scheduled one: "the operator asked" is not a reason for two collectors
        # to sweep one subnet, and the operator pressed the button on central,
        # which already knows who holds it.
        if ctype == "rescan":
            await discover_all(client, backend, config, leases)
        elif ctype == "poll_now":
            await poll_targets(client, backend, config, spool, leases=leases)
        elif ctype == "poll_printer":
            payload = cmd.get("payload") or {}
            ip = payload.get("ip")
            if ip:
                await poll_one_target(client, backend, config, ip, spool, leases)
            else:
                log.warning("poll_printer command #%s missing 'ip' in payload", cmd.get("id"))
        elif ctype in ("remote_fetch", "remote_probe", "remote_write"):
            await handle_remote(client, backend, config, ctype, cmd.get("payload") or {})
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


async def _try_heartbeat(
    client: CentralClient,
    install_path: str,
    update_result: Optional[dict],
    leases: Optional[CollectorLeases] = None,
) -> bool:
    """Heartbeat, reporting reachability instead of raising. True = central answered.

    The heartbeat is a liveness ping, not a precondition for the rest of the
    cycle. Letting its failure escape used to skip the poll below it, so a
    central restart produced no readings at all for the whole outage -- not even
    spooled ones -- which is precisely the hole in the meters and the supply
    forecasts that the spool exists to prevent.

    It is also where collection leases are granted. The monotonic anchor is
    sampled BEFORE the request goes out, so the deadline derived from it is
    always earlier than the expiry central recorded on receipt -- see
    ``CollectorLeases``. A failed heartbeat deliberately leaves the existing
    deadlines alone rather than clearing them: a lease that was granted is good
    until it elapses, and one missed heartbeat is not a lost lease. It is also
    not extended, so a central that stays unreachable runs the leases out.
    """
    anchor = time.monotonic()
    try:
        response = await client.heartbeat(
            __version__, install_path=install_path, last_update_result=update_result
        )
    except Exception as exc:  # noqa: BLE001 - anything raised here means "central is down"
        log.warning("heartbeat failed, treating central as down: %s", exc)
        return False
    if leases is not None:
        # An older central sends no collector block. Recording an empty grant is
        # the right reading of that: it has no leases to give, so it has no
        # leased subnets, so nothing is withheld either (`leased` is false on
        # every subnet it serves).
        block = (response or {}).get("collector") or {}
        leases.record(
            block.get("held") or [],
            block.get("lease_seconds") or 0,
            anchor=anchor,
        )
    return True


async def run_once(config: AgentConfig, backend: Optional[SnmpBackend] = None) -> dict:
    """One full cycle: heartbeat, commands, poll, discover. Returns a summary.

    An unreachable central degrades the cycle instead of aborting it: the poll
    still runs and its readings are spooled for replay. Discovery is skipped
    while central is down -- ``/discovered`` is the only place its results can
    go, so a sweep would just be thrown away. Note a one-shot run has no cached
    target list to fall back on, so an offline cycle has nothing to poll; the
    long-running loop is where the spool earns its keep.
    """
    backend = backend or PysnmpBackend()
    client = CentralClient(
        config.central_url, config.agent_id, config.api_key, verify_tls=config.verify_tls
    )
    install_path, update_result = _startup_diagnostics()
    spool = _spool_for(config)
    definitions = DefinitionSync(DefinitionStore(config.definitions_path()), config.api_key)
    definitions.load_cache()
    leases = CollectorLeases()
    commands: List[dict] = []
    disc = {"new_pending": 0}
    try:
        online = await _try_heartbeat(client, install_path, update_result, leases)
        if online:
            # Central answered the heartbeat -> it's reachable. Flush any readings
            # spooled during a prior outage before doing anything else this cycle.
            await drain_spool(client, spool)
            config = await _effective_config(client, config)
            # Before the poll, so a definition an operator saved a minute ago is
            # in force on this cycle rather than the next one.
            await definitions.refresh(client)
            commands = await client.get_commands()
            await handle_commands(client, backend, config, commands, spool, leases)
        poll = await poll_targets(client, backend, config, spool, leases=leases)
        if online:
            disc = await discover_all(client, backend, config, leases)
        return {
            "commands": len(commands),
            "spooled": spool.count(),
            "definitions": len(active()),
            **poll,
            **disc,
        }
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
    # Likewise one target cache: it is what lets the poll below keep running
    # (and spooling) while central is unreachable.
    target_cache = TargetCache()
    # Definitions come off the local cache before the first heartbeat, so an
    # agent that boots into an outage still reads its fleet the way it did
    # before the reboot rather than falling back to the standard MIB alone.
    definitions = DefinitionSync(DefinitionStore(config.definitions_path()), config.api_key)
    definitions.load_cache()
    # And one lease tracker, for the process lifetime, because its deadlines are
    # what STOP that cached poll once a lease runs out -- the cache and the
    # tracker are two halves of the same trade. A restart resets it, which is
    # the safe direction: a fresh process holds nothing until central says so.
    leases = CollectorLeases()
    log.info(
        "agent %d started -> %s (install: %s, version: %s, definitions: %d)",
        config.agent_id, config.central_url, install_path, __version__, len(active()),
    )
    try:
        while True:
            now = time.monotonic()
            online = await _try_heartbeat(
                client, install_path, pending_update_result, leases
            )
            if online:
                pending_update_result = None  # only on the first heartbeat
            try:
                # Central-side work: meaningful only while central answers, and
                # guarded on its own so a failure here cannot take the poll with
                # it. Flush the outage backlog first so spooled readings catch up
                # before fresh ones pile on.
                if online:
                    await drain_spool(client, spool)
                    effective = await _effective_config(client, config, effective)
                    await definitions.refresh(client)
                    commands = await client.get_commands()
                    await handle_commands(
                        client, backend, effective, commands, spool, leases
                    )
            except Exception:  # noqa: BLE001 - a bad sync must not cost us the poll
                log.exception("central sync failed; continuing on last known good config")
            try:
                if _due(last_poll, effective.poll_interval_seconds, now):
                    # Runs whether or not central answered: poll_targets falls
                    # back to the cached target list and spools what it collects.
                    await poll_targets(
                        client, backend, effective, spool, target_cache, leases
                    )
                    last_poll = now
                # Discovery has no store-and-forward path -- /discovered is the
                # only place its results can go -- so skip the sweep entirely
                # while central is down rather than burn one we'd throw away.
                if online and _due(last_discovery, effective.discovery_interval_seconds, now):
                    await discover_all(client, backend, effective, leases)
                    last_discovery = now
            except Exception:  # noqa: BLE001 - keep the agent alive across transient errors
                log.exception("cycle error; retrying next interval")
            await asyncio.sleep(effective.heartbeat_interval_seconds)
    finally:
        await client.aclose()
        await backend.close()
