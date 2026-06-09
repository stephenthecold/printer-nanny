"""mDNS / DNS-SD discovery -- the hands-off auto-discovery channel.

Most modern printers advertise themselves on the LAN via DNS-SD (Apple
Bonjour / Avahi). When the agent's network can hear multicast packets to
``224.0.0.251:5353``, this module emits one probe result per advertised
service in three printer service types:

  _ipp._tcp.local.            -- Internet Printing Protocol (modern default)
  _printer._tcp.local.        -- LPD/LPR
  _pdl-datastream._tcp.local. -- raw 9100 (JetDirect / RawTCP)

Hits are deduplicated by IP and fed back to the central server as candidate
printer devices, where they're then SNMP-probed (if the agent's subnet
config covers their IP) or surfaced as pending-discovery devices for the
operator to handle.

zeroconf is declared as an **optional dependency**: when it's missing,
``mdns_available()`` returns False and ``discover_mdns()`` is a no-op. This
keeps the agent installable on legacy Pythons / environments that can't
build the zeroconf wheel, without holding up SNMP-only deployments.

Multicast caveat: mDNS only crosses subnets when an mDNS reflector is in
play. For tunnel-terminated remote sites the agent serves via SNMP only,
which is the default story already.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

log = logging.getLogger("printer_nanny_agent.mdns")

# Service types we look for. Most modern printers advertise all three; we
# dedupe by IP downstream so a single device showing up under all three
# names is reported once.
MDNS_PRINTER_SERVICES = (
    "_ipp._tcp.local.",
    "_ipps._tcp.local.",
    "_printer._tcp.local.",
    "_pdl-datastream._tcp.local.",
)


def mdns_available() -> bool:
    """True iff the zeroconf optional dep is installed."""
    try:
        import zeroconf  # noqa: F401
    except ImportError:
        return False
    return True


def _format_addr(addr_bytes: bytes) -> Optional[str]:
    """Convert a raw 4-byte IPv4 packed address into dotted-quad."""
    if not addr_bytes or len(addr_bytes) != 4:
        return None
    return ".".join(str(b) for b in addr_bytes)


async def discover_mdns(timeout_seconds: float = 4.0) -> List[dict]:
    """Browse mDNS for printer services on the local subnet for ``timeout_seconds``.

    Returns a list of device dicts compatible with the central /discovered API:
      {"ip": "10.0.0.5", "hostname": "HP-LaserJet.local", "brand": None,
       "model": None, "serial": None, "subnet_cidr": None}

    The caller (the agent runner) tags each result with the matching subnet's
    CIDR before pushing to central, since mDNS doesn't tell us which CIDR a
    device falls into -- we infer it from the agent's subnet list.

    A no-zeroconf install or a multicast-unreachable network returns []
    without raising, so this can be folded into every discovery cycle.
    """
    if not mdns_available():
        log.debug("zeroconf not installed -- skipping mDNS discovery")
        return []
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []

    found: dict[str, dict] = {}  # by IP, last-write-wins

    class _Listener:
        """zeroconf ServiceListener; one method per state. We only care about adds."""

        def add_service(self, zc: "Zeroconf", service_type: str, name: str) -> None:
            info = zc.get_service_info(service_type, name, timeout=1000)
            if info is None:
                return
            for raw in info.addresses or []:
                ip = _format_addr(raw)
                if not ip:
                    continue
                # Extract a friendly hostname from the service name. The form
                # is "<friendly>._ipp._tcp.local."; the friendly part is what
                # the printer advertises (e.g. "HP LaserJet M404 [B1A2C3]").
                friendly = name.rsplit(service_type, 1)[0].rstrip(".")
                # Some advertisements URL-encode characters; pass through as-is
                # so the operator can read what the printer told the network.
                entry = found.setdefault(ip, {
                    "ip": ip,
                    "hostname": friendly or None,
                    "brand": None,
                    "model": None,
                    "serial": None,
                    "subnet_cidr": None,
                    "_mdns_services": [],
                })
                entry["_mdns_services"].append(service_type.rstrip("."))

        def remove_service(self, *_args, **_kwargs) -> None:
            return None

        def update_service(self, *_args, **_kwargs) -> None:
            return None

    # Run the browser in a worker thread; ServiceBrowser uses its own loop.
    def run_browser() -> None:
        zc = Zeroconf()
        try:
            listener = _Listener()
            browsers = [
                ServiceBrowser(zc, st, listener) for st in MDNS_PRINTER_SERVICES
            ]
            # Sleep on this thread; the ServiceBrowsers run on zeroconf's own
            # threads and dispatch back into the listener.
            import time
            time.sleep(timeout_seconds)
            for b in browsers:
                b.cancel()
        finally:
            zc.close()

    try:
        await asyncio.to_thread(run_browser)
    except Exception as exc:  # noqa: BLE001 - mDNS failures must never stop polls
        log.debug("mDNS browse failed: %s", exc)
        return []

    devices = list(found.values())
    log.info(
        "mDNS discovered %d printer-advertising device(s) on the local subnet",
        len(devices),
    )
    return devices


def assign_subnet_cidr(device: dict, known_cidrs: list[str]) -> Optional[str]:
    """Best-effort: pick which of the agent's configured CIDRs the device's IP
    belongs to. Returns the matching CIDR string, or None when no match.

    Pure function so it's unit-testable. The mDNS device dict is mutated by
    the caller (we don't do it here so the assignment logic stays explicit).
    """
    import ipaddress

    try:
        ip = ipaddress.ip_address(device["ip"])
    except (KeyError, ValueError):
        return None
    for cidr in known_cidrs:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if ip in net:
            return cidr
    return None
