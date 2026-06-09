"""mDNS discovery -- subnet assignment + graceful degradation when zeroconf missing.

The real ServiceBrowser path needs network multicast and isn't worth wiring
through CI. What we test:
  * assign_subnet_cidr correctly bucks devices into configured CIDRs
  * mdns_available() reflects the import state
  * discover_all merges mDNS hits with SNMP hits and dedupes by IP
"""

from __future__ import annotations

from printer_nanny_agent.config import AgentConfig, SubnetConfig
from printer_nanny_agent.mdns import assign_subnet_cidr, mdns_available


def test_assign_subnet_cidr_picks_matching_network():
    cidrs = ["10.0.0.0/24", "10.0.1.0/24", "192.168.50.0/24"]
    assert assign_subnet_cidr({"ip": "10.0.0.5"}, cidrs) == "10.0.0.0/24"
    assert assign_subnet_cidr({"ip": "10.0.1.250"}, cidrs) == "10.0.1.0/24"
    assert assign_subnet_cidr({"ip": "192.168.50.200"}, cidrs) == "192.168.50.0/24"


def test_assign_subnet_cidr_returns_none_for_outside_ip():
    cidrs = ["10.0.0.0/24"]
    # WAN-side / unrelated network -- agent shouldn't push these to central.
    assert assign_subnet_cidr({"ip": "8.8.8.8"}, cidrs) is None
    # Same /24 prefix but different /24 -- still outside.
    assert assign_subnet_cidr({"ip": "10.0.1.5"}, cidrs) is None


def test_assign_subnet_cidr_handles_garbage_input():
    assert assign_subnet_cidr({"ip": "not-an-ip"}, ["10.0.0.0/24"]) is None
    assert assign_subnet_cidr({}, ["10.0.0.0/24"]) is None
    # A bad CIDR in the list shouldn't crash the others.
    assert assign_subnet_cidr({"ip": "10.0.0.5"}, ["nonsense", "10.0.0.0/24"]) == "10.0.0.0/24"


def test_mdns_available_matches_import_state():
    """Should not raise either way -- and should agree with whether zeroconf
    is importable in this test environment."""
    try:
        import zeroconf  # noqa: F401
        expected = True
    except ImportError:
        expected = False
    assert mdns_available() is expected


# ---------- discover_all merges sources ----------

async def test_discover_all_merges_snmp_and_mdns(monkeypatch):
    """SNMP+mDNS hits are merged + deduped by IP; SNMP fingerprint wins on
    conflict because the SNMP probe collected richer brand/model data."""
    from printer_nanny_agent import runner

    config = AgentConfig(
        central_url="http://c", agent_id=1, api_key="k",
        subnets=[SubnetConfig(cidr="10.0.0.0/24")],
    )

    async def fake_discover_subnet(backend, cidr, params):
        return [
            {"ip": "10.0.0.10", "brand": "HP", "model": "M404", "serial": "S1",
             "hostname": "hp.local", "subnet_cidr": cidr},
        ]

    async def fake_discover_mdns(timeout_seconds=4.0):
        return [
            # Same IP as SNMP -- should be deduped (SNMP wins).
            {"ip": "10.0.0.10", "brand": None, "model": None, "serial": None,
             "hostname": "different-hostname", "subnet_cidr": None,
             "_mdns_services": ["_ipp._tcp"]},
            # New device only seen via mDNS.
            {"ip": "10.0.0.50", "brand": None, "model": None, "serial": None,
             "hostname": "Brother-MFC.local", "subnet_cidr": None,
             "_mdns_services": ["_ipp._tcp", "_pdl-datastream._tcp"]},
            # Outside the configured subnet -- should be filtered out.
            {"ip": "172.16.0.5", "brand": None, "subnet_cidr": None},
        ]

    pushed = {}

    class FakeClient:
        async def post_discovered(self, devices):
            pushed["devices"] = devices
            return {"new_pending": len(devices)}

    monkeypatch.setattr(runner, "discover_subnet", fake_discover_subnet)
    monkeypatch.setattr(runner, "discover_mdns", fake_discover_mdns)
    monkeypatch.setattr(runner, "mdns_available", lambda: True)

    result = await runner.discover_all(FakeClient(), backend=None, config=config)

    # Two unique devices: the deduped 10.0.0.10 + the mDNS-only 10.0.0.50.
    pushed_ips = sorted(d["ip"] for d in pushed["devices"])
    assert pushed_ips == ["10.0.0.10", "10.0.0.50"]
    # SNMP's richer fingerprint won the merge on 10.0.0.10.
    merged_10 = next(d for d in pushed["devices"] if d["ip"] == "10.0.0.10")
    assert merged_10["brand"] == "HP"
    assert merged_10["model"] == "M404"
    # The mDNS-only device kept its hostname and got tagged with the matching subnet.
    new_50 = next(d for d in pushed["devices"] if d["ip"] == "10.0.0.50")
    assert new_50["hostname"] == "Brother-MFC.local"
    assert new_50["subnet_cidr"] == "10.0.0.0/24"
    # Internal field stripped before push.
    assert "_mdns_services" not in new_50
    assert result["new_pending"] == 2


async def test_discover_all_skips_mdns_when_unavailable(monkeypatch):
    """Pure-SNMP environments shouldn't crash because zeroconf isn't installed."""
    from printer_nanny_agent import runner

    config = AgentConfig(
        central_url="http://c", agent_id=1, api_key="k",
        subnets=[SubnetConfig(cidr="10.0.0.0/24")],
    )

    async def fake_discover_subnet(backend, cidr, params):
        return [{"ip": "10.0.0.10", "brand": "HP", "subnet_cidr": cidr}]

    mdns_called = False

    async def fake_discover_mdns(timeout_seconds=4.0):
        nonlocal mdns_called
        mdns_called = True
        return []

    class FakeClient:
        async def post_discovered(self, devices):
            return {"new_pending": len(devices)}

    monkeypatch.setattr(runner, "discover_subnet", fake_discover_subnet)
    monkeypatch.setattr(runner, "discover_mdns", fake_discover_mdns)
    monkeypatch.setattr(runner, "mdns_available", lambda: False)

    await runner.discover_all(FakeClient(), backend=None, config=config)
    assert mdns_called is False  # mDNS path short-circuited
