"""Agent discovery: host enumeration and subnet sweep filtering."""

from __future__ import annotations

from printer_nanny_agent import oids
from printer_nanny_agent.discovery import discover_subnet, hosts_in
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend, canned_printer


def test_hosts_in_cidr():
    assert hosts_in("10.0.0.0/30") == ["10.0.0.1", "10.0.0.2"]
    assert len(hosts_in("192.168.1.0/24")) == 254


async def test_discover_only_returns_printers():
    backend = FakeSnmpBackend()
    # A real printer (has fingerprint).
    backend.add("10.0.0.1", canned_printer(name="hp-1"))
    # A non-printer SNMP device: answers sysDescr but no printer fingerprint.
    backend.devices["10.0.0.2"] = {
        "scalars": {oids.SYS_DESCR: "Cisco Switch", oids.SYS_NAME: "sw1"},
        "walks": {},
    }
    # 10.0.0.3 is absent → times out.

    devices = await discover_subnet(backend, "10.0.0.0/29", SnmpParams(), concurrency=8)
    ips = {d["ip"] for d in devices}
    assert ips == {"10.0.0.1"}
    assert devices[0]["brand"] == "HP"
    assert devices[0]["subnet_cidr"] == "10.0.0.0/29"
