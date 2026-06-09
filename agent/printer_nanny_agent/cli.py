"""Command-line interface for the site agent.

  printer-nanny-agent run [--once]      # main loop (or one cycle)
  printer-nanny-agent poll <ip>         # poll one printer over SNMP, print JSON
  printer-nanny-agent discover          # sweep configured subnets, print devices
  printer-nanny-agent probe <ip>        # raw SNMP probe of one host (diagnostics)
  printer-nanny-agent selftest          # verify connectivity to central
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Optional

from printer_nanny_agent import __version__, oids
from printer_nanny_agent.client import CentralClient
from printer_nanny_agent.config import AgentConfig, load_config
from printer_nanny_agent.discovery import discover_subnet
from printer_nanny_agent.poller import poll_printer
from printer_nanny_agent.runner import run_forever, run_once
from printer_nanny_agent.snmp import SnmpError


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _backend():
    from printer_nanny_agent.snmp import PysnmpBackend

    return PysnmpBackend()


async def _cmd_poll(config: AgentConfig, ip: str) -> int:
    backend = _backend()
    try:
        reading = await poll_printer(backend, ip, config.snmp)
        print(json.dumps(reading, indent=2))
        return 0
    finally:
        await backend.close()


async def _cmd_discover(config: AgentConfig) -> int:
    backend = _backend()
    try:
        all_devices = []
        for subnet in config.subnets:
            devices = await discover_subnet(backend, subnet.cidr, config.snmp_for(subnet))
            all_devices.extend(devices)
        print(json.dumps(all_devices, indent=2))
        return 0
    finally:
        await backend.close()


async def _cmd_probe(config: AgentConfig, ip: str) -> int:
    """Dump raw SNMP responses for one host -- the diagnostic operators paste into
    a support thread when "discovery finds nothing" turns out to be specific to
    a printer that doesn't expose the OIDs we expect."""
    backend = _backend()
    p = config.snmp
    print(
        f"probing {ip}  community={p.community!r}  v{p.version}  "
        f"port={p.port}  timeout={p.timeout}s  retries={p.retries}"
    )
    try:
        try:
            ident = await backend.get(
                ip, [oids.SYS_DESCR, oids.SYS_NAME, oids.SYS_OBJECT_ID], p
            )
        except SnmpError as exc:
            print(f"  identity GET failed: {exc}", file=sys.stderr)
            return 1
        for oid in (oids.SYS_DESCR, oids.SYS_NAME, oids.SYS_OBJECT_ID):
            print(f"  GET  {oid}\n         = {ident.get(oid)!r}")
        for label, base in (
            ("prtGeneralPrinterName", oids.PRT_GENERAL_PRINTER_NAME_BASE),
            ("prtGeneralSerialNumber", oids.PRT_GENERAL_SERIAL_NUMBER_BASE),
            ("hrDeviceDescr", oids.HR_DEVICE_DESCR_BASE),
        ):
            try:
                rows = await backend.walk(ip, base, p)
            except SnmpError as exc:
                print(f"  WALK {label}: error {exc}")
                continue
            if not rows:
                print(f"  WALK {label}: (empty -- this printer does not expose it)")
                continue
            for oid, value in rows.items():
                print(f"  WALK {label}  {oid}\n         = {value!r}")
        # Vendor-specific private MIB dump. Standard Printer-MIB reports many
        # printers' toner level as -3 (some remaining) because the cartridge has
        # no continuous sensor; the real percentages live under the vendor's
        # private enterprise OID. We walk SPECIFIC sub-trees (not the whole
        # vendor root) because Brother in particular exposes a 600+-row binary
        # status table at .2.3.9.2.1.1.x that drowns out the actual toner data
        # at .2.3.9.4.2.1.5.5.x. A single tree walk would max-rows out before
        # ever reaching the useful values.
        sys_oid = ident.get(oids.SYS_OBJECT_ID) or ""
        # Per-vendor list of (label, oid, max_rows). max_rows is the cap for
        # that specific sub-tree -- give Brother's maintenance tables enough
        # room (~256 rows each is generous) without uncapping ordinary polling.
        _BROTHER_SUBTREES = [
            ("IEEE 1284 DeviceID",       "1.3.6.1.4.1.2435.2.3.9.1.1.7", 8),
            ("brStatus / brInfoStatus",  "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4", 128),
            ("brInfoMaintenance",        "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5", 128),
            ("brInfoNextCare",           "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.6", 128),
            ("brInfoCounter",            "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.7", 128),
            ("brInfoCommands",           "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.8", 64),
        ]
        _HP_SUBTREES = [
            ("hpDeviceStatusInfo",       "1.3.6.1.4.1.11.2.3.9.4.2.1.5",   256),
        ]
        _LEXMARK_SUBTREES = [
            ("prtSupply / lexInfo",      "1.3.6.1.4.1.641.2.1.5",          256),
            ("lexSuppliesInformation",   "1.3.6.1.4.1.641.6.5.7",          256),
        ]
        _GENERIC_SUBTREES = {
            "Canon":   [("private", "1.3.6.1.4.1.1602.1.11.1.3", 256)],
            "Kyocera": [("private", "1.3.6.1.4.1.1347.43",       256)],
            "Ricoh":   [("private", "1.3.6.1.4.1.367.3.2.1.2.24",256)],
            "Samsung": [("private", "1.3.6.1.4.1.236.11.5.11",   256)],
            "Xerox":   [("private", "1.3.6.1.4.1.253.8.53.13",   256)],
        }
        vendor_subtrees = None
        for vendor_prefix, vendor_name, trees in (
            ("2435", "Brother", _BROTHER_SUBTREES),
            ("11.",  "HP",      _HP_SUBTREES),
            ("641",  "Lexmark", _LEXMARK_SUBTREES),
            ("1602", "Canon",   _GENERIC_SUBTREES["Canon"]),
            ("1347", "Kyocera", _GENERIC_SUBTREES["Kyocera"]),
            ("367",  "Ricoh",   _GENERIC_SUBTREES["Ricoh"]),
            ("236",  "Samsung", _GENERIC_SUBTREES["Samsung"]),
            ("253",  "Xerox",   _GENERIC_SUBTREES["Xerox"]),
        ):
            if f"enterprises.{vendor_prefix}" in sys_oid or \
               f".1.3.6.1.4.1.{vendor_prefix}" in sys_oid:
                vendor_subtrees = (vendor_name, trees)
                break
        if vendor_subtrees:
            vendor_name, trees = vendor_subtrees
            for label, root, max_rows in trees:
                print(f"\n  -- {vendor_name} {label} ({root}) --")
                try:
                    rows = await backend.walk_max(ip, root, p, max_rows)
                except SnmpError as exc:
                    print(f"  WALK {label}: error {exc}")
                    continue
                if not rows:
                    print(f"  WALK {label}: (empty)")
                    continue
                for oid, value in rows.items():
                    print(f"  {oid} = {value!r}")
            # Brother: also decode the maintenance/nextcare binary blobs the
            # same way the polling provider does, so the operator can compare
            # the decoded percentages against the printer's own EWS gauge
            # before trusting the dashboard numbers.
            if vendor_name == "Brother":
                from printer_nanny_agent.providers.brother_maintenance import (
                    OID_MAINTENANCE,
                    OID_NEXTCARE,
                    decode_maintenance,
                    decode_nextcare,
                )
                print("\n  -- Brother maintenance blob (decoded) --")
                try:
                    blobs = await backend.get(ip, [OID_MAINTENANCE, OID_NEXTCARE], p)
                except SnmpError as exc:
                    print(f"  GET maintenance blobs: error {exc}")
                else:
                    levels = decode_maintenance(blobs.get(OID_MAINTENANCE))
                    unknown = levels.pop("_unknown", None)
                    if levels:
                        for part, pct in sorted(levels.items()):
                            print(f"  {part:<18} = {pct:.1f}%")
                    else:
                        print("  (no decodable percentage records)")
                    if unknown:
                        print(f"  unknown record IDs: {unknown}  <- send these to the developers")
                    pages = decode_nextcare(blobs.get(OID_NEXTCARE))
                    for part, remaining in sorted(pages.items()):
                        print(f"  {part:<18} ~ {remaining:,} pages left")
        return 0
    finally:
        await backend.close()


async def _cmd_selftest(config: AgentConfig) -> int:
    client = CentralClient(
        config.central_url, config.agent_id, config.api_key, verify_tls=config.verify_tls
    )
    try:
        result = await client.heartbeat(__version__)
        print(f"OK -- central reachable, agent '{result.get('name')}' (id {config.agent_id})")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED -- {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="printer-nanny-agent")
    parser.add_argument("--config", help="path to agent.toml (optional)")
    # Config can come entirely from flags/env -- no file needed (used by the installer).
    parser.add_argument("--central-url", help="central server base URL (or $PN_CENTRAL_URL)")
    parser.add_argument("--agent-id", type=int, help="agent id (or $PN_AGENT_ID)")
    parser.add_argument("--api-key", help="agent API key (or $PN_API_KEY)")
    parser.add_argument("--no-verify-tls", dest="verify_tls", action="store_false", default=None,
                        help="disable TLS verification (self-signed central)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run the agent loop")
    run_p.add_argument("--once", action="store_true", help="run one cycle and exit")
    poll_p = sub.add_parser("poll", help="poll one printer and print its reading")
    poll_p.add_argument("ip")
    sub.add_parser("discover", help="sweep configured subnets and print devices")
    probe_p = sub.add_parser(
        "probe", help="dump raw SNMP responses for one host (diagnostics)"
    )
    probe_p.add_argument("ip")
    sub.add_parser("selftest", help="check connectivity to the central server")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    cli_overrides = {
        "central_url": args.central_url,
        "agent_id": args.agent_id,
        "api_key": args.api_key,
        "verify_tls": args.verify_tls,
    }
    try:
        config = load_config(args.config, cli=cli_overrides)
    except (OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "run":
        if args.once:
            summary = asyncio.run(run_once(config))
            print(summary)
            return 0
        asyncio.run(run_forever(config))
        return 0
    if args.command == "poll":
        return asyncio.run(_cmd_poll(config, args.ip))
    if args.command == "discover":
        return asyncio.run(_cmd_discover(config))
    if args.command == "probe":
        return asyncio.run(_cmd_probe(config, args.ip))
    if args.command == "selftest":
        return asyncio.run(_cmd_selftest(config))
    return 2


if __name__ == "__main__":
    sys.exit(main())
