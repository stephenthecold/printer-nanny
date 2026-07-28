"""Discover printers on the local subnets and report what the IPP probe makes of them.

Read-only, by construction. This opens outbound SNMP (UDP/161), mDNS
(UDP/5353) and IPP (TCP/631) connections and prints what came back. It creates
no print queue, installs no driver, writes nothing outside the file passed to
``--json-out``, and touches no Windows setting. Running it changes nothing.

WHY IT EXISTS
-------------
The Windows CI job proves the queue-provisioning seam against a real spooler,
but a hosted runner has no printer on it. So the one thing still unproven is
the part that decides *what to build*: does ``ipp.probe`` classify real
hardware correctly? A device that answers IPP but is misread as
``driver_required`` sends a technician chasing a driver that was never needed;
one misread as ``driverless`` produces a queue that cannot print.

That answer only exists on a network with printers, which is why this is a
hand-run script rather than a test.

READING THE OUTPUT
------------------
``driverless``      Windows can bind its inbox IPP class driver. Nothing to install.
``driver_required`` Speaks IPP but doesn't advertise a driverless-capable format.
``ipp_disabled``    Refused port 631 -- deliberately NOT reported as needing a
                    driver, because it's a checkbox in the device's web UI and
                    saying "needs a driver" sends you to the wrong place.
``unreachable``     Nothing answered. Firewall, wrong address, or device asleep.
``error``           Answered, but not with something decodable as IPP.

Compare each line against what you know the device to be. A wrong answer here
is worth more than a hundred green unit tests.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import sys
from typing import Dict, List, Optional

# Refuse to sweep anything bigger than this many hosts. A machine sitting on a
# /16 would otherwise queue 65k SNMP probes and look like a port scan to
# whatever is watching the network.
DEFAULT_MAX_HOSTS = 1024


def parse_cidrs(raw: List[str], max_hosts: int) -> List[str]:
    """Validate and size-check the CIDRs handed to us.

    The caller gets these from ``Get-NetIPAddress``, so they are OS-supplied
    rather than hostile, but they are still parsed rather than trusted -- a
    malformed one should be a clear message, not a traceback three phases in.
    """
    out: List[str] = []
    for item in raw:
        text = (item or "").strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            print("  skipping {!r}: {}".format(text, exc))
            continue
        if net.version != 4:
            print("  skipping {}: IPv6 sweep not supported".format(net))
            continue
        if net.is_loopback or net.is_link_local:
            print("  skipping {}: loopback/link-local".format(net))
            continue
        if net.num_addresses > max_hosts:
            print(
                "  skipping {}: {} addresses exceeds the {}-host cap "
                "(pass -Cidr to target a smaller range)".format(
                    net, net.num_addresses, max_hosts
                )
            )
            continue
        out.append(str(net))
    return out


async def sweep_snmp(cidrs: List[str], community: str, timeout: float) -> List[dict]:
    """SNMP-sweep each CIDR. Returns the union of discovered devices."""
    from printer_nanny_agent.discovery import discover_subnet
    from printer_nanny_agent.snmp import PysnmpBackend, SnmpParams

    params = SnmpParams(community=community, version="2c", timeout=timeout)
    backend = PysnmpBackend()
    found: List[dict] = []
    try:
        for cidr in cidrs:
            print("  sweeping {} ...".format(cidr), flush=True)
            try:
                devices = await discover_subnet(backend, cidr, params)
            except Exception as exc:  # noqa: BLE001 - one bad subnet must not end the run
                print("    {} failed: {}".format(cidr, exc))
                continue
            print("    {} printer(s)".format(len(devices)))
            found.extend(devices)
    finally:
        await backend.close()
    return found


async def sweep_mdns(timeout: float) -> List[dict]:
    """Browse mDNS, if zeroconf is installed. Absence is not an error."""
    try:
        from printer_nanny_agent.mdns import discover_mdns, mdns_available
    except ImportError:
        print("  mdns module unavailable; skipping")
        return []
    if not mdns_available():
        print("  zeroconf not installed; skipping mDNS")
        return []
    try:
        devices = await discover_mdns(timeout_seconds=timeout)
    except Exception as exc:  # noqa: BLE001
        print("  mDNS browse failed: {}".format(exc))
        return []
    print("  {} device(s) advertised over mDNS".format(len(devices)))
    return devices


def merge_devices(*groups: List[dict]) -> Dict[str, dict]:
    """Key devices by IP so a device found twice is reported once.

    Later groups fill in fields the earlier ones lacked rather than replacing
    the record -- SNMP knows the model, mDNS often knows the friendly name.
    """
    merged: Dict[str, dict] = {}
    for group in groups:
        for dev in group or []:
            ip = (dev.get("ip") or dev.get("address") or "").strip()
            if not ip:
                continue
            slot = merged.setdefault(ip, {"ip": ip})
            for key, value in dev.items():
                if value not in (None, "") and not slot.get(key):
                    slot[key] = value
    return merged


def probe_one(ip: str, timeout: float) -> dict:
    """Run the IPP probe against one address. Never raises."""
    from printer_nanny_agent import ipp

    try:
        result = ipp.probe(ip, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a probe failure is a result
        return {"status": "error", "reason": "probe raised: {}".format(exc)}
    return {
        "status": result.status,
        "reason": result.reason,
        "endpoint": result.endpoint,
        "attributes": result.as_dict() if result.attributes else None,
    }


def describe(dev: dict) -> str:
    """A one-line identity for a device, from whatever fields we got.

    Both discovery paths return the same shape (ip / hostname / brand / model /
    serial), but they fill in different parts of it: SNMP usually knows the
    model, mDNS usually knows the friendly name. So this walks them in order of
    how identifying they are rather than assuming either source won.
    """
    for key in ("model", "hostname", "brand", "serial"):
        value = dev.get(key)
        if value:
            return str(value)[:48]
    return "(unidentified)"


#: Default NAT ranges for the common desktop hypervisors. A guest on one of
#: these cannot see the physical LAN at all, so an empty sweep is expected
#: rather than diagnostic -- and saying so saves someone debugging the wrong
#: layer entirely.
VIRTUAL_NAT_RANGES = (
    "10.0.2.0/24",     # VirtualBox NAT, and QEMU/KVM user-mode networking
    "192.168.56.0/24",  # VirtualBox host-only (no LAN route either)
)


def is_virtual_nat(cidr: str) -> bool:
    """True when this subnet is a hypervisor NAT range with no route to the LAN."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    if net.version != 4:
        # subnet_of raises TypeError across address families, and every range
        # below is IPv4 anyway.
        return False
    for known in VIRTUAL_NAT_RANGES:
        if net.subnet_of(ipaddress.ip_network(known)):
            return True
    return False


def split_addresses(raw: List[str]) -> "tuple":
    """Separate single addresses from subnets someone passed to --ip by mistake.

    ``-PrinterIp 10.0.3.0/24`` is an easy and costly slip: without this the
    literal string ``10.0.3.0/24`` is handed to the probe as a hostname, which
    cannot resolve, so the run burns the full timeout and then reports
    ``unreachable``. That word means "a device is there and did not answer",
    which is a materially different diagnosis from "you gave me a subnet" -- and
    it sends the reader off checking firewalls and cabling.

    So a value carrying a prefix is rerouted to the sweep, loudly. Rerouting
    rather than rejecting because the intent is unambiguous, and saying so
    rather than doing it silently because a tool that quietly does something
    other than what was asked cannot be trusted on the runs that matter.
    """
    direct: List[str] = []
    misrouted: List[str] = []
    for item in raw or []:
        text = (item or "").strip()
        if not text:
            continue
        if "/" in text:
            print(
                "  '{}' is a subnet, not an address -- sweeping it instead "
                "(that is what --cidr / -Cidr is for)".format(text)
            )
            misrouted.append(text)
        else:
            direct.append(text)
    return direct, misrouted


def sort_key(ip: str):
    """Numeric sort for dotted quads, lexical for anything else.

    Plain string sort puts .10 before .9, which makes a subnet listing
    annoying to scan against a DHCP table.
    """
    parts = ip.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return (0, tuple(int(p) for p in parts))
    return (1, ip)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cidr", action="append", default=[], help="subnet to sweep (repeatable)")
    ap.add_argument("--ip", action="append", default=[], help="probe this address directly (repeatable)")
    ap.add_argument("--community", default="public", help="SNMP v2c community (default: public)")
    ap.add_argument("--snmp-timeout", type=float, default=2.0)
    ap.add_argument("--mdns-timeout", type=float, default=5.0)
    ap.add_argument("--ipp-timeout", type=float, default=5.0)
    ap.add_argument("--max-hosts", type=int, default=DEFAULT_MAX_HOSTS)
    ap.add_argument("--json-out", default=None, help="write the full result as JSON here")
    args = ap.parse_args(argv)

    print("=" * 72)
    print("Printer discovery + IPP driver-tier probe  (read-only)")
    print("=" * 72)

    direct, misrouted = split_addresses(args.ip)
    cidrs = parse_cidrs(list(args.cidr) + misrouted, args.max_hosts)

    if not cidrs and not direct:
        print("\nNo usable subnet or address to probe. Nothing to do.")
        return 0

    snmp_devices: List[dict] = []
    mdns_devices: List[dict] = []

    if cidrs:
        print("\n[1/3] SNMP sweep")
        snmp_devices = asyncio.run(sweep_snmp(cidrs, args.community, args.snmp_timeout))

        print("\n[2/3] mDNS browse")
        mdns_devices = asyncio.run(sweep_mdns(args.mdns_timeout))
    else:
        print("\n[1/3] SNMP sweep  -- skipped (explicit addresses given)")
        print("[2/3] mDNS browse -- skipped")

    merged = merge_devices(snmp_devices, mdns_devices, [{"ip": ip} for ip in direct])

    if not merged:
        print("\nNo devices found.")
        isolated = [c for c in cidrs if is_virtual_nat(c)]
        if isolated:
            # Worth calling out by name rather than leaving to inference: on a
            # NAT'd VM no tool can see the LAN, so "0 printers" says nothing
            # about the probe and everything about the network. Someone
            # debugging this could easily spend an hour on the wrong layer.
            print()
            print("NOTE: {} is the default NAT range used by VirtualBox and".format(isolated[0]))
            print("QEMU/KVM. A guest on NAT is isolated from the physical LAN by")
            print("design, so printers on the real network are unreachable from")
            print("here no matter what tool is used. To reach them, switch the")
            print("VM's adapter to bridged mode, or run this on a host machine.")
        print()
        print("Otherwise this is a real result, not necessarily a failure: a")
        print("segmented management VLAN, a non-default SNMP community, or")
        print("printers that simply aren't on this subnet all look the same.")
        print("Re-run with -Cidr / -PrinterIp to target a known address.")
        return 0

    print("\n[3/3] IPP probe of {} device(s)".format(len(merged)))
    print("-" * 72)

    results = []
    for ip in sorted(merged, key=sort_key):
        dev = merged[ip]
        probe = probe_one(ip, args.ipp_timeout)
        results.append({"ip": ip, "device": dev, "probe": probe})
        print("{:<16} {:<16} {}".format(ip, probe["status"], describe(dev)))
        print("{:<16} {}".format("", probe["reason"]))
        if probe.get("endpoint"):
            print("{:<16} endpoint: {}".format("", probe["endpoint"]))
        print()

    tally: Dict[str, int] = {}
    for row in results:
        status = row["probe"]["status"]
        tally[status] = tally.get(status, 0) + 1

    print("-" * 72)
    print("Summary: " + ", ".join("{} {}".format(v, k) for k, v in sorted(tally.items())))
    print()
    print("Check each line against what you know the device to be. The tier is")
    print("what the workstation client would act on, so a wrong answer here is")
    print("a real bug -- please paste this output back.")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"results": results, "tally": tally}, fh, indent=2, default=str)
        print("\nJSON written to {}".format(args.json_out))

    return 0


if __name__ == "__main__":
    sys.exit(main())
