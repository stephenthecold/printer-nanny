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
    """Dump raw SNMP responses for one host — the diagnostic operators paste into
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
                print(f"  WALK {label}: (empty — this printer does not expose it)")
                continue
            for oid, value in rows.items():
                print(f"  WALK {label}  {oid}\n         = {value!r}")
        return 0
    finally:
        await backend.close()


async def _cmd_selftest(config: AgentConfig) -> int:
    client = CentralClient(
        config.central_url, config.agent_id, config.api_key, verify_tls=config.verify_tls
    )
    try:
        result = await client.heartbeat(__version__)
        print(f"OK — central reachable, agent '{result.get('name')}' (id {config.agent_id})")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED — {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="printer-nanny-agent")
    parser.add_argument("--config", help="path to agent.toml (optional)")
    # Config can come entirely from flags/env — no file needed (used by the installer).
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
