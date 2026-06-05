"""End-to-end agent demo against a running central server, using the fake SNMP
backend so no real printers are needed. Exercises the whole loop: create agent →
heartbeat → fetch targets → poll (fake SNMP) → push readings → discover a subnet.

Usage (with central running and demo data seeded):
    python scripts/e2e_agent_demo.py http://localhost:8137
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from printer_nanny_agent.config import AgentConfig, SubnetConfig
from printer_nanny_agent.runner import run_once
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend, canned_printer

# A subnet with one fake printer, to exercise discovery → pending device.
DISCOVERY_CIDR = "10.200.0.0/30"
DISCOVERY_IP = "10.200.0.1"


def setup_agent(base: str) -> tuple[int, str, list]:
    """Log in, create an agent at site 1, and return (agent_id, api_key, target_ips)."""
    with httpx.Client(base_url=base) as c:
        c.post("/login", data={"username": "admin", "password": "admin"})
        agent = c.post("/api/v1/agents", json={"site_id": 1, "name": "e2e-demo-agent"}).json()
        aid, key = agent["id"], agent["api_key"]
        targets = c.get(
            f"/api/v1/agents/{aid}/targets", headers={"Authorization": f"Bearer {key}"}
        ).json()
    return aid, key, [t["ip"] for t in targets]


def build_backend(target_ips: list) -> FakeSnmpBackend:
    backend = FakeSnmpBackend()
    # Canned data for each approved target (vary toner so the dashboard shows range).
    for i, ip in enumerate(target_ips):
        backend.add(ip, canned_printer(name=f"poll-{ip}", black_level=900 - i * 50, black_max=1000))
    # One undiscovered device on the discovery subnet.
    backend.add(DISCOVERY_IP, canned_printer(name="newly-found", model="Brother HL-L2400"))
    return backend


async def main(base: str) -> int:
    aid, key, target_ips = setup_agent(base)
    print(f"created agent id={aid}, {len(target_ips)} approved target(s) to poll")

    config = AgentConfig(
        central_url=base,
        agent_id=aid,
        api_key=key,
        subnets=[SubnetConfig(cidr=DISCOVERY_CIDR)],
        snmp=SnmpParams(),
    )
    summary = await run_once(config, backend=build_backend(target_ips))
    print("run_once summary:", summary)
    assert summary["applied"] >= 1, "expected at least one reading applied"
    assert summary["new_pending"] >= 1, "expected discovery to find the fake printer"
    print("E2E OK ✅")
    return 0


if __name__ == "__main__":
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8137"
    raise SystemExit(asyncio.run(main(base_url)))
