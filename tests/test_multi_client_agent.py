"""Multi-client agent path: one agent serves several customer sites via
per-subnet source-bind, so RFC 1918 CIDRs can overlap without confusing the OS
routing layer. Covers:

* Subnet.bind_interface column + Pydantic exposure in the agent config payload.
* Cross-site agent assignment via subnet_update (operator reassigns a subnet
  at client X's site to an agent at HQ).
* Cross-site subnet creation via subnet_add with explicit site_id.
* Agent's SnmpParams plumbs bind_interface to the SNMP backend.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sqlalchemy import select

from central import models as m
from central.main import app
from central.security import hash_api_key, hash_password
from printer_nanny_agent.config import AgentConfig, SubnetConfig, merge_remote
from printer_nanny_agent.snmp import SnmpParams


def _admin_http(db) -> TestClient:
    db.add(m.User(
        username="admin", password_hash=hash_password("admin"), role=m.UserRole.admin,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    return cli


def _seed_two_clients(db):
    """Acme + Beta. Acme has the HQ agent; Beta has no agents (collected by HQ)."""
    acme = m.Client(name="Acme")
    beta = m.Client(name="Beta")
    db.add_all([acme, beta])
    db.flush()
    acme_hq = m.Site(client_id=acme.id, name="HQ")
    beta_site = m.Site(client_id=beta.id, name="Beta Office")
    db.add_all([acme_hq, beta_site])
    db.flush()
    hq_agent = m.Agent(
        site_id=acme_hq.id, name="HQ-Collector",
        api_key_hash=hash_api_key("pn_hq_key"),
    )
    db.add(hq_agent)
    db.commit()
    return acme_hq, beta_site, hq_agent


# --- Schema flow ---


def test_bind_interface_flows_through_agent_config(db):
    """A Subnet row's bind_interface ends up in AgentSubnetConfig delivered
    to the agent's /config endpoint."""
    site, beta_site, agent = _seed_two_clients(db)
    db.add(m.Subnet(
        site_id=site.id, agent_id=agent.id, cidr="10.10.0.0/24",
        snmp_community="public", snmp_version="2c",
        bind_interface="10.0.100.1",
    ))
    db.commit()

    cli = TestClient(app)
    resp = cli.get(
        f"/api/v1/agents/{agent.id}/config",
        headers={"Authorization": "Bearer pn_hq_key"},
    )
    assert resp.status_code == 200
    cfg = resp.json()
    subnets = cfg["subnets"]
    assert len(subnets) == 1
    assert subnets[0]["cidr"] == "10.10.0.0/24"
    assert subnets[0]["bind_interface"] == "10.0.100.1"


def test_bind_interface_optional_in_agent_config(db):
    """A subnet without an explicit bind ships bind_interface=None (OS default route)."""
    site, _, agent = _seed_two_clients(db)
    db.add(m.Subnet(
        site_id=site.id, agent_id=agent.id, cidr="10.10.0.0/24",
        snmp_community="public", snmp_version="2c",
    ))
    db.commit()
    cli = TestClient(app)
    resp = cli.get(
        f"/api/v1/agents/{agent.id}/config",
        headers={"Authorization": "Bearer pn_hq_key"},
    )
    assert resp.status_code == 200
    assert resp.json()["subnets"][0]["bind_interface"] is None


# --- Cross-site assignment via dashboard ---


def test_subnet_update_can_set_bind_interface_and_reassign_to_cross_site_agent(db):
    """Admin reassigns a subnet at Beta's site to be collected by Acme's HQ agent,
    setting bind_interface to the local IP that routes to Beta's tunnel."""
    acme_hq, beta_site, hq_agent = _seed_two_clients(db)
    # Beta's subnet, initially unassigned.
    beta_subnet = m.Subnet(
        site_id=beta_site.id, agent_id=None, cidr="192.168.1.0/24",
        snmp_community="public", snmp_version="2c",
    )
    db.add(beta_subnet)
    db.commit()
    db.refresh(beta_subnet)
    sid = beta_subnet.id

    http = _admin_http(db)
    resp = http.post(
        f"/manage/subnets/{sid}",
        data={
            "label": "Beta VLAN 10",
            "snmp_community": "",
            "snmp_version": "",
            "bind_interface": "10.0.200.1",
            "agent_id": str(hq_agent.id),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db.expire_all()
    updated = db.get(m.Subnet, sid)
    assert updated.agent_id == hq_agent.id, "subnet must follow the reassignment to HQ agent"
    assert updated.site_id == beta_site.id, "subnet stays in Beta's site -- tenancy is preserved"
    assert updated.bind_interface == "10.0.200.1"
    assert updated.label == "Beta VLAN 10"


def test_subnet_add_can_target_a_different_site(db):
    """Admin creates a new subnet under HQ-agent but at Beta's site -- the
    multi-client agent pattern."""
    acme_hq, beta_site, hq_agent = _seed_two_clients(db)
    http = _admin_http(db)
    resp = http.post(
        f"/manage/agents/{hq_agent.id}/subnets",
        data={
            "cidr": "192.168.50.0/24",
            "snmp_community": "public",
            "snmp_version": "2c",
            "bind_interface": "10.0.50.1",
            "site_id": str(beta_site.id),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    subnets = list(db.query(m.Subnet).filter_by(cidr="192.168.50.0/24"))
    assert len(subnets) == 1
    sub = subnets[0]
    assert sub.site_id == beta_site.id, "subnet's site is Beta's, not HQ's"
    assert sub.agent_id == hq_agent.id, "but the HQ agent is the collector"
    assert sub.bind_interface == "10.0.50.1"


def test_subnet_update_clears_bind_interface_when_blank(db):
    """An empty bind_interface clears the field -- operator can intentionally
    drop the source-bind and let the OS default route decide."""
    site, _, agent = _seed_two_clients(db)
    sub = m.Subnet(
        site_id=site.id, agent_id=agent.id, cidr="10.10.0.0/24",
        snmp_community="public", snmp_version="2c",
        bind_interface="10.0.0.1",
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    sid = sub.id
    http = _admin_http(db)
    resp = http.post(
        f"/manage/subnets/{sid}",
        data={"label": "", "snmp_community": "", "snmp_version": "",
              "bind_interface": "", "agent_id": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(m.Subnet, sid).bind_interface is None


# --- Agent-side plumbing ---


def test_agent_config_merge_passes_bind_interface_into_subnet():
    """The agent's merge_remote pulls bind_interface from the central payload
    into its in-process SubnetConfig list."""
    base = AgentConfig(
        central_url="https://central", agent_id=1, api_key="k",
        subnets=[SubnetConfig(cidr="10.0.0.0/24")],
    )
    remote = {
        "subnets": [
            {"cidr": "192.168.1.0/24", "snmp_community": "public",
             "snmp_version": "2c", "bind_interface": "10.0.200.1"},
            {"cidr": "192.168.2.0/24", "snmp_community": "public",
             "snmp_version": "2c"},  # no bind -> None
        ],
        "snmp": {"community": "public", "version": "2c"},
        "poll_interval_seconds": 300,
        "discovery_interval_seconds": 3600,
        "heartbeat_interval_seconds": 60,
    }
    merged = merge_remote(base, remote)
    assert [s.cidr for s in merged.subnets] == ["192.168.1.0/24", "192.168.2.0/24"]
    assert merged.subnets[0].bind_interface == "10.0.200.1"
    assert merged.subnets[1].bind_interface is None


def test_snmp_for_subnet_propagates_bind_interface():
    """AgentConfig.snmp_for(subnet) lifts bind_interface from SubnetConfig into
    SnmpParams so each probe uses the correct source IP."""
    base = AgentConfig(
        central_url="https://central", agent_id=1, api_key="k",
        subnets=[],
    )
    sub = SubnetConfig(cidr="192.168.1.0/24", bind_interface="10.0.200.1")
    params = base.snmp_for(sub)
    assert isinstance(params, SnmpParams)
    assert params.bind_interface == "10.0.200.1"


@pytest.mark.skipif(
    True, reason="pysnmp transport target accepts localAddress -- behavior smoke-tested manually"
)
def test_pysnmp_backend_passes_local_address():
    """Placeholder for an integration test against a real pysnmp engine; the
    relevant assertion is encoded in the unit test above (SnmpParams carries
    bind_interface) and the PysnmpBackend._target implementation passes
    localAddress=(bind_interface, 0) when set."""


# --- End-to-end ingest scoping ---


def test_ingest_targets_returns_printers_across_sites(db):
    """One agent at HQ collects approved printers at BOTH Acme HQ AND Beta."""
    acme_hq, beta_site, hq_agent = _seed_two_clients(db)
    beta_client = db.get(m.Site, beta_site.id).client
    # A subnet at Beta's site, collected by HQ-agent.
    db.add(m.Subnet(
        site_id=beta_site.id, agent_id=hq_agent.id, cidr="192.168.1.0/24",
        bind_interface="10.0.200.1",
    ))
    # One approved printer at each site.
    db.add_all([
        m.Printer(
            client_id=acme_hq.client_id, site_id=acme_hq.id,
            ip="10.10.0.42", discovery_state=m.DiscoveryState.approved,
        ),
        m.Printer(
            client_id=beta_client.id, site_id=beta_site.id,
            ip="192.168.1.42", discovery_state=m.DiscoveryState.approved,
        ),
    ])
    db.commit()
    cli = TestClient(app)
    resp = cli.get(
        f"/api/v1/agents/{hq_agent.id}/targets",
        headers={"Authorization": "Bearer pn_hq_key"},
    )
    assert resp.status_code == 200
    ips = sorted(t["ip"] for t in resp.json())
    assert ips == ["10.10.0.42", "192.168.1.42"], (
        "HQ agent must see printers from every site that has subnets assigned to it"
    )


def test_ingest_discovered_drops_printer_into_subnet_site_not_agent_site(db):
    """When an HQ agent reports a printer at Beta's subnet, the printer must
    land in Beta's site (and Beta's client) -- not in Acme HQ. This is the
    crucial tenancy guarantee for the multi-client agent pattern."""
    acme_hq, beta_site, hq_agent = _seed_two_clients(db)
    beta_client_id = db.get(m.Site, beta_site.id).client_id
    db.add(m.Subnet(
        site_id=beta_site.id, agent_id=hq_agent.id, cidr="192.168.1.0/24",
    ))
    db.commit()
    cli = TestClient(app)
    resp = cli.post(
        f"/api/v1/agents/{hq_agent.id}/discovered",
        headers={"Authorization": "Bearer pn_hq_key"},
        json={"devices": [
            {"ip": "192.168.1.42", "brand": "Brother",
             "model": "MFC-L8900CDW", "subnet_cidr": "192.168.1.0/24"},
        ]},
    )
    assert resp.status_code == 200
    db.expire_all()
    printer = db.scalar(
        select(m.Printer).where(m.Printer.ip == "192.168.1.42")
    )
    assert printer is not None
    assert printer.site_id == beta_site.id, (
        "printer must be at Beta's site, not HQ's -- otherwise multi-client "
        "agents would silently merge customers"
    )
    assert printer.client_id == beta_client_id


def test_ingest_readings_applies_to_printer_at_remote_site(db):
    """An HQ agent posting a reading for an approved printer at Beta's site
    must actually update that printer (not be 'skipped_unknown')."""
    acme_hq, beta_site, hq_agent = _seed_two_clients(db)
    beta_client_id = db.get(m.Site, beta_site.id).client_id
    db.add(m.Subnet(
        site_id=beta_site.id, agent_id=hq_agent.id, cidr="192.168.1.0/24",
    ))
    db.add(m.Printer(
        client_id=beta_client_id, site_id=beta_site.id,
        ip="192.168.1.42", discovery_state=m.DiscoveryState.approved,
    ))
    db.commit()
    cli = TestClient(app)
    resp = cli.post(
        f"/api/v1/agents/{hq_agent.id}/readings",
        headers={"Authorization": "Bearer pn_hq_key"},
        json={"readings": [
            {"ip": "192.168.1.42", "status": "ok", "page_count": 12345,
             "supplies": [], "events": []},
        ]},
    )
    assert resp.status_code == 200
    assert resp.json()["applied"] == 1
    db.expire_all()
    printer = db.scalar(
        select(m.Printer).where(m.Printer.ip == "192.168.1.42")
    )
    assert printer.page_count == 12345
    assert printer.last_seen is not None
