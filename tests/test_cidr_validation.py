"""A subnet nothing can parse is refused, not stored under a green "ready".

``/manage/onboard`` and the Agents page both took a CIDR and wrote it verbatim:
no ``pattern``, no ``required``, no ``ipaddress`` anywhere. The consequences are
all silent, which is the reason this was worth its own module:

* ``collector._network`` swallows the ``ValueError`` and returns None, so a
  discovered printer never matches its subnet and simply never joins the fleet;
* the agent's ``discovery.hosts_in`` raises before sweeping a single address;
* the onboarding form still answered "Acme / HQ is ready — run the install
  command below", and the operator's next step is a site visit.

So the first evidence of a typo was a technician standing in a comms room with
an agent that discovers nothing. The rule now matches the rest of the app: an
unreadable value is a mistake, it is refused, nothing is written, and the
submission comes back rather than being retyped.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central.dashboard import manage
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


def _onboard(cli, **overrides):
    data = {
        "client_name": "Acme Ltd",
        "site_name": "Head office",
        "cidr": "10.10.0.0/24",
        "snmp_community": "public",
        "snmp_version": "2c",
        "ttl_minutes": 1440,
    }
    data.update(overrides)
    return cli.post("/manage/onboard", data=data, follow_redirects=False)


def _agent(db) -> m.Agent:
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    agent = m.Agent(site_id=site.id, name="hq", api_key_hash="x" * 64)
    db.add(agent)
    db.commit()
    return agent


@pytest.fixture()
def flashes(monkeypatch):
    """Every ``_flash`` call made during a request, in order.

    The refusal redirects and the message is popped by the next render, so the
    level and the carried fields are otherwise unobservable from a test client.
    """
    seen: list[dict] = []
    original = manage._flash

    def spy(request, message, level="success", fields=None, errors=None):
        seen.append({"message": message, "level": level,
                     "fields": fields or {}, "errors": errors or {}})
        return original(request, message, level=level, fields=fields, errors=errors)

    monkeypatch.setattr(manage, "_flash", spy)
    return seen


# --------------------------------------------------------------------------- #
# The parser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("10.10.0.0/24", "10.10.0.0/24"),
    ("  10.10.0.0/24  ", "10.10.0.0/24"),
    # strict=False: the address an operator knows the network by, canonicalised
    # to the network that will actually be swept.
    ("10.10.0.7/24", "10.10.0.0/24"),
    ("192.168.1.5", "192.168.1.5/32"),
    ("2001:db8::/64", "2001:db8::/64"),
])
def test_a_readable_cidr_comes_back_canonical(raw, expected):
    errors: dict = {}
    assert manage._parse_cidr(raw, "cidr", errors) == expected
    assert errors == {}


@pytest.mark.parametrize("raw", [
    "10.10.0.0/33",        # prefix out of range
    "10.10.0.0/",          # no prefix at all
    "300.1.1.0/24",        # octet out of range
    "10.10.0.0-10.10.0.255",  # a range, not a CIDR
    "the office wifi",
    "10.10.0.0/24; DROP TABLE subnets",
])
def test_an_unreadable_cidr_is_an_error_not_a_value(raw):
    errors: dict = {}
    assert manage._parse_cidr(raw, "cidr", errors) is None
    assert "cidr" in errors


def test_blank_is_the_callers_question_not_an_error():
    errors: dict = {}
    assert manage._parse_cidr("   ", "cidr", errors) is None
    assert errors == {}


# --------------------------------------------------------------------------- #
# Onboarding
# --------------------------------------------------------------------------- #
def test_a_bad_cidr_creates_no_client_site_or_claim_code(db, flashes):
    _mk_user(db, "admin", m.UserRole.admin)
    _onboard(_login("admin"), cidr="10.10.0.0/33")
    db.expire_all()
    assert not list(db.scalars(select(m.Client))), "a garbage CIDR built a customer"
    assert not list(db.scalars(select(m.Site)))
    assert not list(db.scalars(select(m.Subnet)))
    assert not list(db.scalars(select(m.AgentClaimToken)))


def test_the_onboarding_refusal_is_red_and_hands_the_form_back(db, flashes):
    _mk_user(db, "admin", m.UserRole.admin)
    _onboard(_login("admin"), cidr="the office wifi", client_name="Acme Ltd")
    assert flashes, "the refusal said nothing at all"
    last = flashes[-1]
    assert last["level"] == "error", "a refusal was reported in the success colour"
    assert last["fields"]["cidr"] == "the office wifi"
    assert last["fields"]["client_name"] == "Acme Ltd"
    assert last["fields"]["site_name"] == "Head office"
    assert "cidr" in last["errors"]


def test_a_good_cidr_still_onboards(db, flashes):
    _mk_user(db, "admin", m.UserRole.admin)
    _onboard(_login("admin"))
    db.expire_all()
    subnet = db.scalar(select(m.Subnet))
    assert subnet is not None and subnet.cidr == "10.10.0.0/24"
    assert flashes[-1]["level"] == "success"


def test_onboarding_stores_the_network_not_the_host_address(db):
    """An operator types the address they know the VLAN by; the row has to say
    what the agent will sweep, or two spellings of one network become two rows."""
    _mk_user(db, "admin", m.UserRole.admin)
    _onboard(_login("admin"), cidr="10.10.0.42/24")
    db.expire_all()
    assert db.scalar(select(m.Subnet)).cidr == "10.10.0.0/24"


def test_a_blank_cidr_is_still_optional_here(db):
    """Blank means "no subnet yet", which onboarding has always allowed."""
    _mk_user(db, "admin", m.UserRole.admin)
    _onboard(_login("admin"), cidr="")
    db.expire_all()
    assert not list(db.scalars(select(m.Subnet)))
    assert list(db.scalars(select(m.AgentClaimToken))), "the rest of the flow was refused too"


def test_a_refused_onboarding_writes_no_audit_trail(db, flashes):
    """Nothing happened, so nothing is claimed to have happened."""
    _mk_user(db, "admin", m.UserRole.admin)
    _onboard(_login("admin"), cidr="300.1.1.0/24")
    actions = {a.action for a in db.scalars(select(m.AuditLog))}
    assert "client.create" not in actions
    assert "subnet.create" not in actions


# --------------------------------------------------------------------------- #
# Assigning a subnet to an agent
# --------------------------------------------------------------------------- #
def test_subnet_add_refuses_a_bad_cidr(db, flashes):
    _mk_user(db, "admin", m.UserRole.admin)
    agent = _agent(db)
    _login("admin").post(f"/manage/agents/{agent.id}/subnets",
                         data={"cidr": "10.0.5.0/99"}, follow_redirects=False)
    db.expire_all()
    assert not list(db.scalars(select(m.Subnet))), "an unusable subnet was assigned"
    assert flashes[-1]["level"] == "error"
    assert flashes[-1]["fields"]["cidr"] == "10.0.5.0/99"


def test_subnet_add_says_so_when_the_field_is_empty(db, flashes):
    """Previously a silent no-op: the page came back with no subnet, no message,
    and nothing distinguishing "refused" from "saved"."""
    _mk_user(db, "admin", m.UserRole.admin)
    agent = _agent(db)
    _login("admin").post(f"/manage/agents/{agent.id}/subnets",
                         data={"cidr": "   "}, follow_redirects=False)
    db.expire_all()
    assert not list(db.scalars(select(m.Subnet)))
    assert flashes and flashes[-1]["level"] == "error"


def test_subnet_add_canonicalises_what_it_stores(db):
    _mk_user(db, "admin", m.UserRole.admin)
    agent = _agent(db)
    _login("admin").post(f"/manage/agents/{agent.id}/subnets",
                         data={"cidr": "10.0.5.9/24", "snmp_community": "public"},
                         follow_redirects=False)
    db.expire_all()
    assert db.scalar(select(m.Subnet)).cidr == "10.0.5.0/24"


def test_a_refused_assignment_is_not_audited_as_a_creation(db, flashes):
    _mk_user(db, "admin", m.UserRole.admin)
    agent = _agent(db)
    _login("admin").post(f"/manage/agents/{agent.id}/subnets",
                         data={"cidr": "not a subnet"}, follow_redirects=False)
    assert not [a for a in db.scalars(select(m.AuditLog)) if a.action == "subnet.create"]


def test_a_good_assignment_still_works(db, flashes):
    _mk_user(db, "admin", m.UserRole.admin)
    agent = _agent(db)
    _login("admin").post(f"/manage/agents/{agent.id}/subnets",
                         data={"cidr": "10.0.5.0/24", "snmp_community": "public",
                               "snmp_version": "2c"},
                         follow_redirects=False)
    db.expire_all()
    created = db.scalar(select(m.Subnet))
    assert created is not None and created.cidr == "10.0.5.0/24"
    assert created.agent_id == agent.id
    assert flashes[-1]["level"] == "success"
