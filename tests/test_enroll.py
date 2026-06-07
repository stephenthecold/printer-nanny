"""Server-side agent enrollment CLI."""

from __future__ import annotations

from central import enroll as enroll_mod
from central import models as m
from central.security import hash_api_key


def test_enroll_creates_agent_and_subnet(db):
    result = enroll_mod.enroll(
        client_name="Acme", site_name="HQ", agent_name="HQ agent",
        subnet="10.0.3.0/24", community="floor-ro",
    )
    assert result["api_key"].startswith("pn_")
    agent = db.get(m.Agent, result["agent_id"])
    assert agent is not None
    assert agent.api_key_hash == hash_api_key(result["api_key"])  # only the hash is stored
    subnet = db.query(m.Subnet).filter_by(agent_id=agent.id).one()
    assert subnet.cidr == "10.0.3.0/24"
    assert subnet.snmp_community == "floor-ro"


def test_enroll_reuses_client_and_site(db):
    enroll_mod.enroll(client_name="Acme", site_name="HQ", agent_name="a1")
    enroll_mod.enroll(client_name="Acme", site_name="HQ", agent_name="a2")
    assert db.query(m.Client).filter_by(name="Acme").count() == 1
    assert db.query(m.Site).count() == 1
    assert db.query(m.Agent).count() == 2  # a fresh agent each time
