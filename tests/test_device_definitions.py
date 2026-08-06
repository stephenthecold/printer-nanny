"""Central's half of server-pushed definitions: the feed, its scoping, the UI.

Three things this has to get right, and each is a way the feature could leak or
misbehave rather than merely be inconvenient:

* **Tenancy.** A client-scoped definition must never reach an agent that does
  not collect for that client.
* **Versioning.** The steady state -- every agent, every poll, forever -- is
  "nothing changed", and it must cost nothing. A version that moved on its own
  would mean every agent re-downloading on every poll.
* **The stored row is the validator's output**, never an operator's text, so
  what an agent receives is what was checked.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import device_definitions as dd
from central import models as m
from central import services
from central.main import app
from central.security import hash_api_key, hash_password

GOOD_SPEC = {
    "key": "acme-9000-series",
    "match": {"enterprise": "12345", "model": "AC-9000"},
    "supplies": [
        {"oid": "1.3.6.1.4.1.12345.1.4.1.1", "type": "toner", "color": "black",
         "decode": {"kind": "percent"}},
    ],
}


# --------------------------------- helpers --------------------------------- #
def _admin(db, username="admin"):
    user = m.User(username=username, password_hash=hash_password("pw12345678"),
                  role=m.UserRole.admin)
    db.add(user)
    db.commit()
    return user


def _login(username="admin"):
    cli = TestClient(app)
    cli.post("/login", data={"username": username, "password": "pw12345678"},
             follow_redirects=False)
    return cli


def _client_site_agent(db, name, key):
    client = m.Client(name=name)
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name=f"{name} HQ")
    db.add(site)
    db.flush()
    agent = m.Agent(site_id=site.id, name=f"{name}-agent", api_key_hash=hash_api_key(key))
    db.add(agent)
    db.commit()
    return client, site, agent


def _definition(db, key, *, client_id=None, enabled=True, **overrides):
    spec = dd.validate_definition(dict(GOOD_SPEC, key=key, **overrides))
    row = m.DeviceDefinition(
        key=spec["key"], name=key, client_id=client_id, enabled=enabled, spec=spec,
    )
    db.add(row)
    db.commit()
    return row


def _fetch(key, agent_id, since=""):
    cli = TestClient(app)
    return cli.get(
        f"/api/v1/agents/{agent_id}/device-definitions",
        params={"since": since},
        headers={"Authorization": f"Bearer {key}"},
    )


# ------------------------------- the feed ---------------------------------- #
def test_an_agent_receives_global_definitions(db):
    _, _, agent = _client_site_agent(db, "Acme", "k-acme")
    _definition(db, "acme-9000-series")

    resp = _fetch("k-acme", agent.id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is True
    assert [d["key"] for d in body["definitions"]] == ["acme-9000-series"]
    assert body["version"] and len(body["version"]) == 16


def test_a_client_scoped_definition_never_reaches_another_tenants_agent(db):
    acme, _, acme_agent = _client_site_agent(db, "Acme", "k-acme")
    _beta, _, beta_agent = _client_site_agent(db, "Beta", "k-beta")
    _definition(db, "acme-only", client_id=acme.id)
    _definition(db, "everyones-definition")

    acme_keys = [d["key"] for d in _fetch("k-acme", acme_agent.id).json()["definitions"]]
    beta_keys = [d["key"] for d in _fetch("k-beta", beta_agent.id).json()["definitions"]]
    assert acme_keys == ["acme-only", "everyones-definition"]
    assert beta_keys == ["everyones-definition"]


def test_an_hq_agent_collecting_for_another_client_gets_that_clients_definitions(db):
    """The multi-client pattern: one agent at HQ over per-client tunnels. Scope
    follows the subnets it actually serves, not just its home site."""
    _acme, acme_site, hq_agent = _client_site_agent(db, "Acme", "k-hq")
    beta, beta_site, _ = _client_site_agent(db, "Beta", "k-beta")
    db.add(m.Subnet(site_id=beta_site.id, cidr="10.9.0.0/24", agent_id=hq_agent.id))
    db.commit()
    _definition(db, "beta-only", client_id=beta.id)

    keys = [d["key"] for d in _fetch("k-hq", hq_agent.id).json()["definitions"]]
    assert keys == ["beta-only"]
    assert acme_site.id in services.sites_served_by_agent(db, hq_agent)


def test_a_disabled_definition_is_not_served(db):
    _, _, agent = _client_site_agent(db, "Acme", "k-acme")
    row = _definition(db, "acme-9000-series")
    assert _fetch("k-acme", agent.id).json()["definitions"]

    row.enabled = False
    db.commit()
    assert _fetch("k-acme", agent.id).json()["definitions"] == []


def test_a_client_scoped_definition_overrides_the_global_one_with_the_same_key(db):
    acme, _, agent = _client_site_agent(db, "Acme", "k-acme")
    _definition(db, "shared-key", match={"model": "GLOBAL-1"})
    _definition(db, "shared-key", client_id=acme.id, match={"model": "SCOPED-1"})

    served = _fetch("k-acme", agent.id).json()["definitions"]
    assert len(served) == 1
    assert served[0]["match"]["model"] == "scoped-1"


def test_two_clients_scoping_one_key_for_one_agent_is_refused_not_guessed(db, caplog):
    """One agent at HQ collecting for two customers who both scoped the same
    key. Serving either is a coin flip over what that fleet reads."""
    _acme, _acme_site, hq = _client_site_agent(db, "Acme", "k-hq")
    beta, beta_site, _ = _client_site_agent(db, "Beta", "k-beta")
    gamma, gamma_site, _ = _client_site_agent(db, "Gamma", "k-gamma")
    db.add(m.Subnet(site_id=beta_site.id, cidr="10.1.0.0/24", agent_id=hq.id))
    db.add(m.Subnet(site_id=gamma_site.id, cidr="10.2.0.0/24", agent_id=hq.id))
    db.commit()
    _definition(db, "contested", client_id=beta.id)
    _definition(db, "contested", client_id=gamma.id)
    _definition(db, "uncontested")

    with caplog.at_level("WARNING"):
        keys = [d["key"] for d in _fetch("k-hq", hq.id).json()["definitions"]]
    assert keys == ["uncontested"]
    assert "refusing to guess" in caplog.text


# ------------------------------- versioning -------------------------------- #
def test_an_unchanged_feed_carries_no_definitions_at_all(db):
    """The steady state on every poll of every agent forever."""
    _, _, agent = _client_site_agent(db, "Acme", "k-acme")
    _definition(db, "acme-9000-series")

    first = _fetch("k-acme", agent.id).json()
    again = _fetch("k-acme", agent.id, since=first["version"]).json()
    assert again == {"version": first["version"], "changed": False}
    assert "definitions" not in again
    assert "signature" not in again


def test_the_version_is_stable_across_calls_but_moves_on_a_real_change(db):
    _, _, agent = _client_site_agent(db, "Acme", "k-acme")
    row = _definition(db, "acme-9000-series")

    v1 = _fetch("k-acme", agent.id).json()["version"]
    assert _fetch("k-acme", agent.id).json()["version"] == v1

    row.spec = dd.validate_definition(dict(GOOD_SPEC, match={"model": "AC-9500"}))
    db.commit()
    v2 = _fetch("k-acme", agent.id).json()["version"]
    assert v2 != v1


def test_deleting_a_definition_moves_the_version_so_agents_drop_it(db):
    """A full set on change means there is no tombstone to miss -- which is why
    a withdrawn definition actually stops being applied."""
    _, _, agent = _client_site_agent(db, "Acme", "k-acme")
    row = _definition(db, "acme-9000-series")
    v1 = _fetch("k-acme", agent.id).json()["version"]

    db.delete(row)
    db.commit()
    after = _fetch("k-acme", agent.id, since=v1).json()
    assert after["version"] != v1
    assert after["changed"] is True
    assert after["definitions"] == []


def test_the_version_does_not_depend_on_row_order(db):
    """An unordered query would change the version on every call and make every
    agent re-download on every poll."""
    _, _, agent = _client_site_agent(db, "Acme", "k-acme")
    for key in ("zulu-definition", "alpha-definition", "mike-definition"):
        _definition(db, key)
    served = _fetch("k-acme", agent.id).json()["definitions"]
    assert [d["key"] for d in served] == sorted(d["key"] for d in served)
    assert dd.feed_version(served) == dd.feed_version(list(reversed(served)))


# ------------------------------- signature --------------------------------- #
def test_the_feed_is_signed_against_the_requesting_agents_credential(db):
    """The binding that makes a cache lifted from another agent fail closed."""
    _, _, one = _client_site_agent(db, "Acme", "k-acme")
    _, _, two = _client_site_agent(db, "Beta", "k-beta")
    _definition(db, "acme-9000-series")

    a = _fetch("k-acme", one.id).json()
    b = _fetch("k-beta", two.id).json()
    assert a["definitions"] == b["definitions"]
    assert a["signature"] != b["signature"]
    assert dd.signature_ok(hash_api_key("k-acme"), a["version"], a["definitions"], a["signature"])
    assert not dd.signature_ok(hash_api_key("k-acme"), b["version"], b["definitions"], b["signature"])


def test_the_feed_needs_an_agent_credential(db):
    _, _, agent = _client_site_agent(db, "Acme", "k-acme")
    _definition(db, "acme-9000-series")
    cli = TestClient(app)
    assert cli.get(f"/api/v1/agents/{agent.id}/device-definitions").status_code == 401
    assert _fetch("wrong-key", agent.id).status_code == 401
    # ...and another agent's key does not open this agent's feed.
    _, _, other = _client_site_agent(db, "Beta", "k-beta")
    assert _fetch("k-beta", agent.id).status_code == 401
    assert other.id != agent.id


def test_fetching_the_feed_counts_as_a_heartbeat(db):
    """Every agent-authenticated read does, so an agent whose only traffic is
    this one is not reported offline."""
    _, _, agent = _client_site_agent(db, "Acme", "k-acme")
    assert agent.last_heartbeat is None
    _fetch("k-acme", agent.id)
    db.refresh(agent)
    assert agent.last_heartbeat is not None
    assert agent.status == m.AgentStatus.online


# ------------------------------ the operator UI ---------------------------- #
def test_an_operator_can_add_edit_and_delete_a_definition(db):
    _admin(db)
    cli = _login()

    resp = cli.post("/manage/definitions/save", data={
        "name": "Acme AC-9000", "spec": json.dumps(GOOD_SPEC),
        "enabled": "1", "enabled_present": "1", "client_id": "", "notes": "from a probe",
    }, follow_redirects=False)
    assert resp.status_code == 303

    row = db.scalar(select(m.DeviceDefinition))
    assert row.key == "acme-9000-series"
    assert row.name == "Acme AC-9000"
    assert row.client_id is None
    assert row.revision == 1
    # The STORED value is the validator's normalised output, not what was typed:
    # the model tag is lowercased and the decode default is explicit.
    assert row.spec["match"]["model"] == "ac-9000"
    assert row.spec["supplies"][0]["decode"] == {"kind": "percent"}
    assert row.spec["override_builtin"] is False

    page = cli.get("/manage/definitions").text
    assert "acme-9000-series" in page

    edited = dict(GOOD_SPEC, match={"enterprise": "12345", "model": "AC-9500"})
    cli.post("/manage/definitions/save", data={
        "definition_id": str(row.id), "name": "Acme AC-9500",
        "spec": json.dumps(edited), "enabled": "1", "enabled_present": "1",
        "client_id": "",
    }, follow_redirects=False)
    db.refresh(row)
    assert row.spec["match"]["model"] == "ac-9500"
    assert row.revision == 2  # content changed

    cli.post(f"/manage/definitions/{row.id}/delete", follow_redirects=False)
    assert db.scalar(select(m.DeviceDefinition)) is None


def test_a_definition_the_validator_refuses_is_never_stored(db):
    _admin(db)
    cli = _login()
    bad = dict(GOOD_SPEC)
    bad["supplies"] = [{"oid": "1.3.6; rm -rf /", "type": "toner"}]
    resp = cli.post("/manage/definitions/save", data={
        "name": "Bad", "spec": json.dumps(bad), "enabled": "1", "enabled_present": "1",
        "client_id": "",
    }, follow_redirects=True)
    assert db.scalar(select(m.DeviceDefinition)) is None
    assert "Refused" in resp.text


def test_editing_does_not_silently_re_enable_a_disabled_definition(db):
    """The checkbox presence marker. An unchecked box posts nothing, so without
    it an edit cannot tell 'unticked' from 'this form did not carry the field'
    -- the same failure class as subnets.trusted."""
    _admin(db)
    cli = _login()
    row = _definition(db, "acme-9000-series")
    assert row.enabled is True

    cli.post("/manage/definitions/save", data={
        "definition_id": str(row.id), "name": "Acme", "spec": json.dumps(GOOD_SPEC),
        "enabled_present": "1", "client_id": "",   # 'enabled' absent = unticked
    }, follow_redirects=False)
    db.refresh(row)
    assert row.enabled is False


def test_a_duplicate_key_in_one_scope_is_refused_with_a_sentence(db):
    _admin(db)
    cli = _login()
    _definition(db, "acme-9000-series")
    resp = cli.post("/manage/definitions/save", data={
        "name": "Second", "spec": json.dumps(GOOD_SPEC), "enabled": "1",
        "enabled_present": "1", "client_id": "",
    }, follow_redirects=True)
    assert len(list(db.scalars(select(m.DeviceDefinition)))) == 1
    assert "already exists" in resp.text


def test_a_scope_naming_a_client_that_does_not_exist_is_refused(db):
    _admin(db)
    cli = _login()
    resp = cli.post("/manage/definitions/save", data={
        "name": "Orphan", "spec": json.dumps(GOOD_SPEC), "enabled": "1",
        "enabled_present": "1", "client_id": "4242",
    }, follow_redirects=True)
    assert db.scalar(select(m.DeviceDefinition)) is None
    assert "no longer exists" in resp.text


def test_override_builtin_is_a_checkbox_and_is_audited(db):
    """The flag that lets a definition displace hardware-proven code. 'Never
    silently' has to be true in the record as well as on the printer."""
    _admin(db)
    cli = _login()
    cli.post("/manage/definitions/save", data={
        "name": "Acme", "spec": json.dumps(GOOD_SPEC), "enabled": "1",
        "enabled_present": "1", "client_id": "", "override_builtin": "1",
    }, follow_redirects=False)

    row = db.scalar(select(m.DeviceDefinition))
    assert row.spec["override_builtin"] is True
    entry = db.scalar(
        select(m.AuditLog).where(m.AuditLog.action == "device_definition.create")
    )
    assert entry is not None
    assert "override_builtin=True" in entry.detail
    assert "acme-9000-series" in entry.detail


def test_delete_is_audited(db):
    _admin(db)
    cli = _login()
    row = _definition(db, "acme-9000-series")
    cli.post(f"/manage/definitions/{row.id}/delete", follow_redirects=False)
    entry = db.scalar(
        select(m.AuditLog).where(m.AuditLog.action == "device_definition.delete")
    )
    assert entry is not None and "acme-9000-series" in entry.detail


def test_the_page_is_closed_to_client_readonly_users(db):
    """A definition changes what every agent does to every printer. A customer's
    read-only account has no business here, in either direction."""
    db.add(m.User(username="cust", password_hash=hash_password("pw12345678"),
                  role=m.UserRole.client_readonly))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "cust", "password": "pw12345678"},
             follow_redirects=False)
    # 403: signed in, not permitted. Not the sign-in form.
    assert cli.get("/manage/definitions", follow_redirects=False).status_code == 403
    resp = cli.post("/manage/definitions/save", data={
        "name": "x", "spec": json.dumps(GOOD_SPEC), "client_id": "",
    }, follow_redirects=False)
    assert resp.status_code == 403
    assert db.scalar(select(m.DeviceDefinition)) is None


def test_logged_out_users_get_nothing(db):
    """Anonymous is the case that KEEPS the redirect: there is a real action to
    take and the sign-in page is where it is taken. Only an authenticated user
    who lacks the permission gets a 403."""
    cli = TestClient(app)
    resp = cli.get("/manage/definitions", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_a_hostile_definition_field_is_escaped_in_the_page(db):
    """Notes and labels are operator free-text and land in HTML."""
    _admin(db)
    cli = _login()
    row = _definition(db, "acme-9000-series")
    row.name = "<script>alert('xss')</script>"
    db.commit()
    page = cli.get("/manage/definitions").text
    assert "<script>alert('xss')</script>" not in page
    assert "&lt;script&gt;" in page


# ------------------------------ schema shape ------------------------------- #
def test_two_global_definitions_cannot_share_a_key(db):
    """SQL treats NULLs as distinct in a UNIQUE, so the (key, client_id)
    constraint does NOT cover the global case -- which is every row an agent
    receives. A partial unique index is what enforces it."""
    from sqlalchemy.exc import IntegrityError

    _definition(db, "acme-9000-series")
    db.add(m.DeviceDefinition(
        key="acme-9000-series", name="dupe", client_id=None,
        spec=dd.validate_definition(GOOD_SPEC),
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_definition_scoped_to_a_deleted_client_is_served_to_nobody(db):
    """The guarantee is behavioural, and it has to be, because the schema's is
    not portable here.

    ``ondelete="CASCADE"`` is a *database* action and this project does not turn
    on SQLite's ``PRAGMA foreign_keys`` (there is no listener in ``central/db.py``),
    so on SQLite the row survives its client as an orphan while on Postgres it is
    deleted. That is pre-existing and applies to every table in this codebase
    using ondelete without a matching ORM relationship cascade -- it is not
    specific to definitions.

    What must hold on both is that nobody receives it, and that follows from
    scoping being computed from the clients an agent actually collects for: a
    deleted client is in no agent's set, so the orphan is unreachable rather
    than merely tidy.
    """
    client, _, _ = _client_site_agent(db, "Acme", "k-acme")
    _beta, _, beta_agent = _client_site_agent(db, "Beta", "k-beta")
    _definition(db, "acme-only", client_id=client.id)
    db.delete(client)
    db.commit()

    assert _fetch("k-beta", beta_agent.id).json()["definitions"] == []
