"""Trusted subnets: the one path that puts a device in a fleet with no human.

Two things are under test and they fail in opposite directions. The gate itself
must not be too loose -- only a subnet an operator explicitly marked trusted may
auto-approve, and anything of unknown provenance still queues. The operator
control must not be too eager -- an unchecked checkbox posts nothing, so a form
that never carried the field must not read as "turn auto-approval off".
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import schemas as s
from central import services
from central.main import app
from central.security import hash_password


def _mk_user(db, username: str, role: m.UserRole, password: str = "pw12345678") -> m.User:
    user = m.User(username=username, password_hash=hash_password(password), role=role)
    db.add(user)
    db.commit()
    return user


def _login(username: str, password: str = "pw12345678") -> TestClient:
    cli = TestClient(app)
    cli.post("/login", data={"username": username, "password": password},
             follow_redirects=False)
    return cli


def _fleet(db, *, trusted: bool = False):
    """One client/site/agent plus a subnet with the requested trust setting."""
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    agent = m.Agent(site_id=site.id, name="HQ agent", api_key_hash="x" * 64)
    db.add(agent)
    db.flush()
    subnet = m.Subnet(site_id=site.id, agent_id=agent.id, cidr="10.0.1.0/24", trusted=trusted)
    db.add(subnet)
    db.commit()
    return client, site, agent, subnet


# ---------- the gate ----------

def test_trusted_subnet_auto_approves(db):
    _, _, agent, _ = _fleet(db, trusted=True)
    printer, created = services.record_discovered(
        db, agent, s.DiscoveredIn(ip="10.0.1.10", subnet_cidr="10.0.1.0/24")
    )
    db.commit()
    assert created
    # Re-read rather than trusting the in-session object.
    db.expire_all()
    assert db.get(m.Printer, printer.id).discovery_state == m.DiscoveryState.approved


def test_untrusted_subnet_still_queues(db):
    _, _, agent, _ = _fleet(db, trusted=False)
    printer, _ = services.record_discovered(
        db, agent, s.DiscoveredIn(ip="10.0.1.10", subnet_cidr="10.0.1.0/24")
    )
    db.commit()
    db.expire_all()
    assert db.get(m.Printer, printer.id).discovery_state == m.DiscoveryState.pending


def test_unknown_provenance_never_auto_approves(db):
    """A CIDR nobody enrolled, and a legacy agent reporting none at all.

    Both resolve to no Subnet row. Falling back to the agent's home site is
    correct for *placement*, but it must not inherit trust from a subnet the
    device was never seen on.
    """
    _, _, agent, _ = _fleet(db, trusted=True)
    unenrolled, _ = services.record_discovered(
        db, agent, s.DiscoveredIn(ip="10.9.9.10", subnet_cidr="10.9.9.0/24")
    )
    no_cidr, _ = services.record_discovered(db, agent, s.DiscoveredIn(ip="10.0.1.77"))
    db.commit()
    db.expire_all()
    assert db.get(m.Printer, unenrolled.id).discovery_state == m.DiscoveryState.pending
    assert db.get(m.Printer, no_cidr.id).discovery_state == m.DiscoveryState.pending


def test_auto_approval_is_audited(db):
    _, _, agent, subnet = _fleet(db, trusted=True)
    services.record_discovered(
        db, agent, s.DiscoveredIn(ip="10.0.1.10", subnet_cidr="10.0.1.0/24")
    )
    db.commit()
    rows = list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "printer.auto_approve")
    ))
    assert len(rows) == 1
    assert "10.0.1.10" in rows[0].target
    assert f"subnet:{subnet.id}" in rows[0].detail
    # The actor is the agent, not a stale logged-in user.
    assert rows[0].user_id is None


def test_untrusted_discovery_writes_no_auto_approve_audit(db):
    _, _, agent, _ = _fleet(db, trusted=False)
    services.record_discovered(
        db, agent, s.DiscoveredIn(ip="10.0.1.10", subnet_cidr="10.0.1.0/24")
    )
    db.commit()
    assert not list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "printer.auto_approve")
    ))


def test_trusting_a_subnet_does_not_sweep_the_backlog(db):
    """Rows a human already left pending carry a decision. Flipping the subnet
    to trusted must not silently overturn it."""
    _, _, agent, subnet = _fleet(db, trusted=False)
    printer, _ = services.record_discovered(
        db, agent, s.DiscoveredIn(ip="10.0.1.10", subnet_cidr="10.0.1.0/24")
    )
    db.commit()

    subnet.trusted = True
    db.commit()
    again, created = services.record_discovered(
        db, agent, s.DiscoveredIn(ip="10.0.1.10", subnet_cidr="10.0.1.0/24")
    )
    db.commit()
    db.expire_all()
    assert not created and again.id == printer.id
    assert db.get(m.Printer, printer.id).discovery_state == m.DiscoveryState.pending


def test_auto_approval_never_downgrades_an_ignored_device(db):
    """Ignore is a decision too -- a trusted subnet must not resurrect it."""
    _, _, agent, _ = _fleet(db, trusted=True)
    printer, _ = services.record_discovered(
        db, agent, s.DiscoveredIn(ip="10.0.1.10", subnet_cidr="10.0.1.0/24")
    )
    db.commit()
    printer.discovery_state = m.DiscoveryState.ignored
    db.commit()

    services.record_discovered(
        db, agent, s.DiscoveredIn(ip="10.0.1.10", subnet_cidr="10.0.1.0/24")
    )
    db.commit()
    db.expire_all()
    assert db.get(m.Printer, printer.id).discovery_state == m.DiscoveryState.ignored


# ---------- the operator control ----------

def test_subnet_create_honours_the_trusted_checkbox(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _, _, agent, _ = _fleet(db)
    cli = _login("admin")
    cli.post(f"/manage/agents/{agent.id}/subnets",
             data={"cidr": "10.0.5.0/24", "trusted": "1"}, follow_redirects=False)
    db.expire_all()
    created = db.scalar(select(m.Subnet).where(m.Subnet.cidr == "10.0.5.0/24"))
    assert created is not None and created.trusted is True


def test_subnet_create_defaults_to_untrusted(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _, _, agent, _ = _fleet(db)
    cli = _login("admin")
    cli.post(f"/manage/agents/{agent.id}/subnets",
             data={"cidr": "10.0.6.0/24"}, follow_redirects=False)
    db.expire_all()
    created = db.scalar(select(m.Subnet).where(m.Subnet.cidr == "10.0.6.0/24"))
    assert created is not None and created.trusted is False


def test_edit_without_the_checkbox_field_leaves_trust_alone(db):
    """The boolean-wipe guard. An unchecked box posts nothing, so a form that
    omits the control entirely -- the inline rename -- must not read as "off"."""
    _mk_user(db, "admin", m.UserRole.admin)
    _, _, _, subnet = _fleet(db, trusted=True)
    cli = _login("admin")
    cli.post(f"/manage/subnets/{subnet.id}", data={"label": "VLAN 10"},
             follow_redirects=False)
    db.expire_all()
    refreshed = db.get(m.Subnet, subnet.id)
    assert refreshed.label == "VLAN 10"
    assert refreshed.trusted is True, "a rename silently disabled auto-approval"


def test_unchecking_the_box_does_clear_trust(db):
    """The other half: with the marker present, absence of the box is a real
    'off' and must take effect -- otherwise trust could never be revoked."""
    _mk_user(db, "admin", m.UserRole.admin)
    _, _, _, subnet = _fleet(db, trusted=True)
    cli = _login("admin")
    cli.post(f"/manage/subnets/{subnet.id}",
             data={"label": "VLAN 10", "trusted_present": "1"}, follow_redirects=False)
    db.expire_all()
    assert db.get(m.Subnet, subnet.id).trusted is False


def test_trust_flip_is_audited(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _, _, _, subnet = _fleet(db, trusted=False)
    cli = _login("admin")
    cli.post(f"/manage/subnets/{subnet.id}",
             data={"trusted_present": "1", "trusted": "1"}, follow_redirects=False)
    rows = list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "subnet.update")
    ))
    assert rows and "trusted=on" in (rows[-1].detail or "")


def test_agents_page_renders_the_trust_control(db):
    _mk_user(db, "admin", m.UserRole.admin)
    _, _, _, subnet = _fleet(db, trusted=True)
    cli = _login("admin")
    body = cli.get("/manage/agents").text
    assert f'id="subnet-trusted-{subnet.id}"' in body
    assert 'name="trusted_present"' in body
