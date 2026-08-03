"""Remote hands, agent side: the guards, the probe, and the honest write.

The agent is the party with a route into a customer LAN, so the point of most
of what is asserted here is that a *central* it authenticates still cannot make
it connect to something it should not -- the same stance the definition feed
takes about a signature.

The fetch tests drive a real HTTP server on loopback rather than mocking httpx:
the two behaviours that matter (a redirect that must not be followed, a body cut
off mid-stream at the cap) are properties of the transport, and a mock asserts
only that we called the mock.
"""

from __future__ import annotations

import asyncio
import http.server
import threading

import pytest

from printer_nanny_agent import remote as rh
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams

from tests.fakes import FakeSnmpBackend

SYS_LOCATION = "1.3.6.1.2.1.1.6.0"
SYS_CONTACT = "1.3.6.1.2.1.1.4.0"


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "addr",
    ["127.0.0.1", "::1", "::ffff:127.0.0.1", "169.254.169.254", "0.0.0.0",
     "224.0.0.1", "fe80::1", "printer.example.com", "", None],
)
def test_the_agent_refuses_these_addresses_whatever_central_says(addr):
    with pytest.raises(rh.RemoteRefused):
        rh.check_address(addr)


def test_the_agent_accepts_an_ordinary_lan_address():
    assert rh.check_address(" 10.4.7.23 ") == "10.4.7.23"


@pytest.mark.parametrize("port", [22, 9100, 445, 0, "x"])
def test_the_agent_refuses_ports_outside_the_allowlist(port):
    with pytest.raises(rh.RemoteRefused):
        rh.check_port(port)


@pytest.mark.parametrize("path", ["//evil/", "relative", "/a b", "/x\ty", "/" + "a" * 600])
def test_the_agent_refuses_these_paths(path):
    with pytest.raises(rh.RemoteRefused):
        rh.check_path(path)


def test_a_compromised_central_cannot_smuggle_an_oid(monkeypatch):
    """The OID is re-checked here, not trusted from the payload.

    Same character loop the definition validator uses, and for the same reason:
    it is the gate that keeps anything that is not an OID out of an SNMP call.
    """
    backend = FakeSnmpBackend()
    for oid in ("1.3.6; rm -rf /", "1.3.6.1.4.1.x", "", "1..3.6"):
        with pytest.raises(rh.RemoteRefused):
            asyncio.run(rh.perform_write(
                backend, {"ip": "10.0.0.5", "oid": oid, "value": "x", "snmp_type": "string"},
                SnmpParams(),
            ))


def test_an_unsupported_write_type_is_refused_rather_than_guessed():
    with pytest.raises(rh.RemoteRefused):
        asyncio.run(rh.perform_write(
            FakeSnmpBackend(),
            {"ip": "10.0.0.5", "oid": SYS_LOCATION, "value": "x", "snmp_type": "opaque"},
            SnmpParams(),
        ))


def test_the_base_backend_refuses_to_write_rather_than_reporting_success():
    """A backend that cannot write must SAY so.

    Silently returning would make "the device accepted the write" -- which is
    what central records as capability -- true of every device on a backend that
    never sent a packet.
    """
    with pytest.raises(NotImplementedError):
        asyncio.run(SnmpBackend().set("10.0.0.5", SYS_LOCATION, "x", "string", SnmpParams()))


# --------------------------------------------------------------------------- #
# The probe
# --------------------------------------------------------------------------- #
class _WritableBackend(FakeSnmpBackend):
    def __init__(self, values):
        super().__init__({})
        self.values = dict(values)
        self.writes = []

    async def get(self, host, oid_list, params):
        return {oid: self.values.get(oid) for oid in oid_list}

    async def set(self, host, oid, value, value_type, params):
        self.writes.append((host, oid, value, value_type))
        self.values[oid] = value
        return value


class _ReadOnlyBackend(_WritableBackend):
    """A hardened device: reads answer, writes come back noAccess.

    This is HP's Secure-by-Default shape, and on a hardened fleet it is the
    COMMON case rather than the edge one.
    """

    async def set(self, host, oid, value, value_type, params):
        raise SnmpError(f"{host}: noAccess")


def test_a_writable_device_is_proven_by_a_write_that_changes_nothing():
    backend = _WritableBackend({SYS_LOCATION: "Front desk"})
    result = asyncio.run(rh.probe_writable(backend, "10.0.0.5", SnmpParams()))
    assert result["ok"] is True and result["writable"] is True
    # The value written is the value already there, so the device is unchanged.
    assert backend.writes == [("10.0.0.5", SYS_LOCATION, "Front desk", "string")]
    assert backend.values[SYS_LOCATION] == "Front desk"


def test_a_device_that_refuses_the_set_is_read_only_with_the_reason():
    backend = _ReadOnlyBackend({SYS_LOCATION: "Front desk"})
    result = asyncio.run(rh.probe_writable(backend, "10.0.0.5", SnmpParams()))
    assert result["ok"] is True and result["writable"] is False
    assert "noAccess" in result["error"]


def test_the_probe_falls_back_to_sys_contact():
    backend = _WritableBackend({SYS_LOCATION: None, SYS_CONTACT: "helpdesk"})
    result = asyncio.run(rh.probe_writable(backend, "10.0.0.5", SnmpParams()))
    assert result["writable"] is True
    assert backend.writes[0][1] == SYS_CONTACT


def test_an_unreadable_device_yields_NO_verdict_rather_than_read_only():
    """"We could not ask" must never be recorded as "the answer is no"."""
    backend = _WritableBackend({})     # every probe OID absent
    result = asyncio.run(rh.probe_writable(backend, "10.0.0.5", SnmpParams()))
    assert result["ok"] is False
    assert "writable" not in result
    assert backend.writes == []


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def test_a_verifiable_write_is_confirmed_by_reading_it_back():
    backend = _WritableBackend({SYS_LOCATION: "old"})
    result = asyncio.run(rh.perform_write(
        backend,
        {"ip": "10.0.0.5", "oid": SYS_LOCATION, "value": "Front desk",
         "snmp_type": "string", "verify": True},
        SnmpParams(),
    ))
    assert result["ok"] is True and result["verified"] is True
    assert backend.values[SYS_LOCATION] == "Front desk"


def test_a_write_the_device_swallows_is_reported_unverified_not_successful():
    """A SET that returns without error is not evidence the device applied it."""

    class _Swallows(_WritableBackend):
        async def set(self, host, oid, value, value_type, params):
            return value            # accepted, and quietly not applied

    backend = _Swallows({SYS_LOCATION: "old"})
    result = asyncio.run(rh.perform_write(
        backend,
        {"ip": "10.0.0.5", "oid": SYS_LOCATION, "value": "new",
         "snmp_type": "string", "verify": True},
        SnmpParams(),
    ))
    assert result["ok"] is True
    assert result["verified"] is False
    assert "read-back disagrees" in result["error"] or "reports" in result["error"]


def test_a_restart_reports_no_verification_rather_than_claiming_one():
    backend = _WritableBackend({})
    result = asyncio.run(rh.perform_write(
        backend,
        {"ip": "10.0.0.5", "oid": "1.3.6.1.2.1.43.5.1.1.3.1", "value": "4",
         "snmp_type": "int", "verify": False},
        SnmpParams(),
    ))
    assert result["ok"] is True
    assert result["verified"] is None      # never True
    assert backend.writes[0][3] == "int"


def test_a_refused_write_downgrades_capability():
    backend = _ReadOnlyBackend({SYS_LOCATION: "old"})
    result = asyncio.run(rh.perform_write(
        backend,
        {"ip": "10.0.0.5", "oid": SYS_LOCATION, "value": "new",
         "snmp_type": "string", "verify": True},
        SnmpParams(),
    ))
    assert result["ok"] is False and result["writable"] is False


# --------------------------------------------------------------------------- #
# Fetch, against a real HTTP server
# --------------------------------------------------------------------------- #
class _Handler(http.server.BaseHTTPRequestHandler):
    behaviour = "ok"

    def log_message(self, *args):      # keep pytest output clean
        pass

    def do_GET(self):
        cls = type(self)
        if cls.behaviour == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
            return
        if cls.behaviour == "huge":
            body = b"A" * (rh.MAX_BODY_BYTES * 2)
        elif cls.behaviour == "cookie":
            body = b"<h1>hi</h1>"
        else:
            body = b"<h1>Ready</h1>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cls.behaviour == "cookie":
            self.send_header("Set-Cookie", "session=stolen; Path=/")
        self.end_headers()
        self.wfile.write(body)
        cls.last_headers = dict(self.headers)


@pytest.fixture()
def device_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _fetch(server, behaviour, monkeypatch, path="/"):
    _Handler.behaviour = behaviour
    port = server.server_address[1]
    # The loopback refusal and the port allowlist are what this test has to step
    # around to reach a local server at all -- which is itself the evidence they
    # are enforced. They are patched here and nowhere in the shipped path.
    monkeypatch.setattr(rh, "ALLOWED_PORTS", (port,))
    monkeypatch.setattr(rh, "check_address", lambda ip: "127.0.0.1")
    return asyncio.run(rh.fetch_page(
        {"ip": "127.0.0.1", "scheme": "http", "port": port, "path": path}
    ))


def test_a_page_comes_back_with_its_status_and_body(device_server, monkeypatch):
    result = _fetch(device_server, "ok", monkeypatch)
    assert result["ok"] is True
    assert result["http_status"] == 200
    assert result["body"] == "<h1>Ready</h1>"
    assert result["truncated"] is False


def test_a_redirect_to_cloud_metadata_is_reported_and_never_followed(
    device_server, monkeypatch
):
    result = _fetch(device_server, "redirect", monkeypatch)
    assert result["http_status"] == 302
    assert result["detail"]["redirect_not_followed"] is True
    assert "169.254.169.254" in result["detail"]["location"]
    # Nothing from the metadata service came back, because we never went there.
    assert "meta-data" not in (result.get("body") or "")


def test_an_enormous_body_is_cut_off_at_the_cap(device_server, monkeypatch):
    result = _fetch(device_server, "huge", monkeypatch)
    assert result["truncated"] is True
    assert result["body_bytes"] == rh.MAX_BODY_BYTES
    assert len(result["body"]) == rh.MAX_BODY_BYTES


def test_no_credential_of_ours_is_sent_and_no_cookie_of_theirs_is_kept(
    device_server, monkeypatch
):
    result = _fetch(device_server, "cookie", monkeypatch)
    sent = {k.lower() for k in _Handler.last_headers}
    assert "authorization" not in sent
    assert "cookie" not in sent
    assert _Handler.last_headers["User-Agent"] == rh.USER_AGENT
    # A device Set-Cookie is dropped where it arrives: only the status, the
    # content type and (for a redirect) the Location ever travel to central.
    assert "set-cookie" not in {k.lower() for k in result}
    assert "stolen" not in str(result)


def test_an_unreachable_device_is_a_stated_result_not_an_exception(monkeypatch):
    monkeypatch.setattr(rh, "ALLOWED_PORTS", (9,))
    monkeypatch.setattr(rh, "check_address", lambda ip: "127.0.0.1")
    result = asyncio.run(rh.fetch_page(
        {"ip": "127.0.0.1", "scheme": "http", "port": 9, "path": "/"}
    ))
    assert result["ok"] is False and "could not reach" in result["error"]


def test_a_body_in_a_charset_we_cannot_name_still_decodes():
    assert "�" in rh._decode(b"\xff\xfe\x00", "text/html; charset=utf-8")
    assert rh._decode(b"caf\xe9", "text/html; charset=latin-1") == "caf\xe9"
    # A charset Python has never heard of falls through to utf-8 rather than
    # raising: this path must produce SOMETHING renderable for every device.
    assert rh._decode(b"ok", "text/html; charset=not-a-real-charset") == "ok"


# --------------------------------------------------------------------------- #
# The dispatcher always reports
# --------------------------------------------------------------------------- #
class _RecordingClient:
    def __init__(self):
        self.posted = []

    async def get_targets(self):
        return []

    async def post_remote_result(self, request_id, result):
        self.posted.append((request_id, result))
        return {"accepted": True}


def test_every_path_reports_something_back(monkeypatch):
    """A request that produces no answer leaves an operator guessing."""
    from printer_nanny_agent import runner
    from printer_nanny_agent.config import AgentConfig

    config = AgentConfig(central_url="http://c", agent_id=1, api_key="k")
    client = _RecordingClient()

    # A refusal: central named an address this agent will not reach.
    asyncio.run(runner.handle_remote(
        client, FakeSnmpBackend(), config, "remote_probe",
        {"request_id": 7, "ip": "169.254.169.254"},
    ))
    assert client.posted[0][0] == 7
    assert client.posted[0][1]["ok"] is False
    assert "refused by the agent" in client.posted[0][1]["error"]

    # A successful probe.
    asyncio.run(runner.handle_remote(
        client, _WritableBackend({SYS_LOCATION: "x"}), config, "remote_probe",
        {"request_id": 8, "ip": "10.0.0.5"},
    ))
    assert client.posted[1][1]["writable"] is True


def test_a_command_with_no_request_id_reports_nowhere_and_does_not_crash():
    from printer_nanny_agent import runner
    from printer_nanny_agent.config import AgentConfig

    client = _RecordingClient()
    asyncio.run(runner.handle_remote(
        client, FakeSnmpBackend(), AgentConfig(central_url="http://c", agent_id=1, api_key="k"),
        "remote_probe", {"ip": "10.0.0.5"},
    ))
    assert client.posted == []
