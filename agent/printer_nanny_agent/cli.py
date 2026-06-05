"""Command-line interface for the site agent.

  printer-nanny-agent run [--once]      # main loop (or one cycle)
  printer-nanny-agent poll <ip>         # poll one printer over SNMP, print JSON
  printer-nanny-agent discover          # sweep configured subnets, print devices
  printer-nanny-agent selftest          # verify connectivity to central
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Optional

from printer_nanny_agent import __version__
from printer_nanny_agent.client import CentralClient
from printer_nanny_agent.config import AgentConfig, load_config
from printer_nanny_agent.discovery import discover_subnet
from printer_nanny_agent.poller import poll_printer
from printer_nanny_agent.runner import run_forever, run_once


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
    parser.add_argument("--config", help="path to agent.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="run the agent loop")
    run_p.add_argument("--once", action="store_true", help="run one cycle and exit")
    poll_p = sub.add_parser("poll", help="poll one printer and print its reading")
    poll_p.add_argument("ip")
    sub.add_parser("discover", help="sweep configured subnets and print devices")
    sub.add_parser("selftest", help="check connectivity to the central server")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        config = load_config(args.config)
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
    if args.command == "selftest":
        return asyncio.run(_cmd_selftest(config))
    return 2


if __name__ == "__main__":
    sys.exit(main())
