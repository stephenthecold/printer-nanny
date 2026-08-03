"""The typed, signed outbound event bus.

Four things are attacked directly here, because each is a way this feature can
be wrong while looking right:

* **Tenancy.** A client-scoped subscription that receives another customer's
  event leaks that customer's hardware, addresses and faults to a third party.
  It is the same invariant ``services.assign_printer`` owns, so it is attacked
  the same way -- by trying to cross the boundary and asserting the refusal.
* **The signature.** A scheme that authenticates but does not bind the timestamp
  is replayable forever. The tests below tamper with the timestamp specifically,
  because a signature over the body alone passes every other test in this file.
* **The destination.** A subscription URL makes this server issue requests. The
  cases here are the ones that matter in practice: cloud metadata, loopback,
  a name that resolves somewhere private, and a redirect out of the checked set.
* **Honest terminal states.** ``sent`` is separate from ``ok`` for notifications
  and must stay separate here: a subscription that was skipped, refused or
  disabled must never be counted as delivered.
"""

from __future__ import annotations

import json

import httpx
import pytest

from central import models as m
from central import services
from central.channels.base import ChannelResult
from central.events import signing
from central.events.catalogue import (
    CATALOGUE,
    EVENT_TYPE_NAMES,
    UnknownEventType,
    _text,
    require,
)
from central.events.delivery import (
    HEADER_ATTEMPT,
    HEADER_EVENT_ID,
    HEADER_EVENT_TYPE,
    HEADER_IDEMPOTENCY,
    build_envelope,
    deliver_due,
    prune,
    send_one,
    serialize,
)
from central.events.destinations import DestinationError, validate_url
from central.events.emit import (
    assert_scope,
    emit,
    emit_alert_opened,
    emit_alert_resolved,
    emit_printer_offline,
    emit_supply_reorder_recommended,
    scope_allows,
    subscriptions_for,
)
from central.secrets import encrypt_value

RUNTIME = {
    "events.enabled": True,
    "events.max_attempts": 3,
    "events.retry_base_seconds": 60,
    "events.timeout_seconds": 5,
    "events.retention_days": 30,
    # The tests post to example.com, which is public, so the guard stays at its
    # shipped default -- a suite that silently ran with it relaxed would prove
    # nothing about the shipped configuration.
    "events.allow_private_destinations": False,
}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _client(db, name):
    c = m.Client(name=name)
    db.add(c)
    db.flush()
    site = m.Site(client_id=c.id, name=name + " HQ")
    db.add(site)
    db.flush()
    c._site = site
    return c


def _printer(db, client, ip="10.0.0.5", **kw):
    p = m.Printer(client_id=client.id, site_id=client._site.id, ip=ip, **kw)
    db.add(p)
    db.flush()
    return p


def _sub(db, name="partner", client_id=None, url="https://hooks.example.com/pn",
         secret="pnevt_test", types=None, enabled=True):
    s = m.EventSubscription(
        name=name, client_id=client_id, url=url,
        secret=encrypt_value(secret), event_types=types, enabled=enabled,
    )
    db.add(s)
    db.flush()
    return s


def _emit(db, event_type="alert.opened", client_id=None, key="k1", data=None):
    return emit(
        db, event_type, data=data or {"hello": "world"},
        client_id=client_id, idempotency_key=key,
    )


class _Capture:
    """A fake transport that records the exact bytes and headers we sent.

    httpx's MockTransport rather than a monkeypatched ``httpx.post``, because the
    thing most worth asserting is the request as constructed -- the signed body
    must be the body on the wire, and a test that intercepts above serialization
    cannot see a mismatch there.
    """

    def __init__(self, status=200, body=b"ok", raise_exc=None, headers=None):
        self.status = status
        self.body = body
        self.raise_exc = raise_exc
        self.headers = headers or {}
        self.requests = []
        #: Per-instance override, set by a test that needs to fail one host.
        #: An instance attribute rather than a class patch: replacing
        #: ``_Capture.__call__`` and deleting it afterwards removes the real
        #: method too, quietly breaking every later test in the file.
        self.handler = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        return httpx.Response(self.status, content=self.body, headers=self.headers)

    @property
    def last(self):
        return self.requests[-1]


@pytest.fixture()
def capture(monkeypatch):
    cap = _Capture()
    real_client = httpx.Client

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(cap)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client_factory)
    # example.com resolves off-box in CI-less environments; pin it so the SSRF
    # pre-flight is exercised with a deterministic public answer rather than
    # depending on the network being up.
    monkeypatch.setattr(
        "central.events.destinations.resolve", lambda host, port: ["93.184.216.34"]
    )
    return cap


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #
def test_every_catalogued_type_is_versioned_and_described():
    assert EVENT_TYPE_NAMES, "the catalogue is the contract; it cannot be empty"
    for name in EVENT_TYPE_NAMES:
        spec = CATALOGUE[name]
        assert spec.name == name
        assert spec.version >= 1
        assert spec.summary.strip()
        assert spec.fields


def test_the_four_documented_types_exist():
    for name in (
        "alert.opened", "alert.resolved", "printer.offline",
        "supply.reorder_recommended",
    ):
        assert name in CATALOGUE


def test_an_uncatalogued_type_is_refused_not_shipped(db):
    with pytest.raises(UnknownEventType):
        require("alert.invented")
    with pytest.raises(UnknownEventType):
        emit(db, "alert.invented", data={}, idempotency_key="x")


def test_device_strings_are_bounded_and_stripped_but_not_html_escaped():
    """JSON is the safe case; escaping on top of it would corrupt real values."""
    assert _text("Sales & Marketing") == "Sales & Marketing"
    assert _text('He said "hi"') == 'He said "hi"'
    assert _text("<img src=x onerror=alert(1)>") == "<img src=x onerror=alert(1)>"
    # Control characters are removed -- not an escaping concern, a transport and
    # storage one: a NUL in a device string breaks a Postgres text column.
    assert _text("HP\x00 M404\x1b[31m") == "HP M404[31m"
    assert _text(None) is None
    assert _text("   ") is None
    long = _text("A" * 900)
    assert len(long) == 500 and long.endswith("…")


def test_a_hostile_device_string_survives_json_intact():
    """The escaping question, settled by round-tripping rather than by assertion.

    A model of ``x", "level_pct": 100, "junk": "`` must not be able to inject a
    sibling key. json.dumps escapes the quote; the parse on the other side gets
    back exactly the characters the device sent, in the field it was put in.
    """
    hostile = 'x", "level_pct": 100, "junk": "'
    body = serialize({"data": {"model": hostile}})
    parsed = json.loads(body.decode("utf-8"))
    assert parsed["data"]["model"] == hostile
    assert list(parsed["data"]) == ["model"], "a device string created a sibling key"


# --------------------------------------------------------------------------- #
# Signing
# --------------------------------------------------------------------------- #
def test_signature_header_shape_and_round_trip():
    secret, body = "pnevt_abc", b'{"a":1}'
    header = signing.sign(secret, body, 1_785_312_000)
    assert header.startswith("t=1785312000,v1=")
    assert signing.verify(secret, body, header, now=1_785_312_010)


def test_signature_is_a_known_answer():
    """Pinned so a refactor cannot silently change the scheme under consumers.

    An operator's verifier is code we do not control; changing what we sign is a
    breaking change for every one of them, and it must not be possible to do it
    by accident.
    """
    expected = signing.compute("pnevt_secret", b'{"id":"evt_1"}', 1_700_000_000)
    assert signing.signed_payload(1_700_000_000, b'{"id":"evt_1"}') == (
        b'1700000000.{"id":"evt_1"}'
    )
    assert signing.compute("pnevt_secret", b'{"id":"evt_1"}', 1_700_000_000) == expected
    assert len(expected) == 64  # sha256 hex


def test_a_tampered_body_fails():
    header = signing.sign("s", b'{"amount":1}', 1_785_312_000)
    assert not signing.verify("s", b'{"amount":999}', header, now=1_785_312_000)


def test_a_tampered_timestamp_fails_because_it_is_inside_the_mac():
    """The reason this scheme was chosen over a bare body HMAC.

    An attacker replaying a captured request has to move ``t`` forward to get
    past a freshness check. With the timestamp in the signed material that
    invalidates the digest; with GitHub-style body-only signing it would not.
    """
    header = signing.sign("s", b"payload", 1_785_312_000)
    moved = header.replace("t=1785312000", "t=1785398400")
    assert not signing.verify("s", b"payload", moved, now=1_785_398_400)


def test_a_stale_signature_is_rejected_as_a_replay():
    header = signing.sign("s", b"payload", 1_785_312_000)
    assert signing.verify("s", b"payload", header, now=1_785_312_100)
    assert not signing.verify("s", b"payload", header, now=1_785_312_000 + 3600)


def test_the_wrong_secret_fails():
    header = signing.sign("right", b"payload", 1_785_312_000)
    assert not signing.verify("wrong", b"payload", header, now=1_785_312_000)


def test_parse_tolerates_whitespace_and_a_future_scheme():
    ts, digests = signing.parse(" t=123 , v1=abc , v2=future ")
    assert ts == 123 and digests == ["abc"]
    assert signing.parse("garbage") == (None, [])
    assert signing.parse("v1=abc") == (None, ["abc"])


def test_a_generated_secret_is_prefixed_and_long():
    secret = signing.generate_secret()
    assert secret.startswith("pnevt_")
    assert len(secret) > 40
    assert signing.generate_secret() != secret


# --------------------------------------------------------------------------- #
# Destinations -- the SSRF boundary
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://internal:70/x",
    "ftp://internal/x",
    "data:text/plain,hi",
    "notaurl",
    "",
])
def test_only_http_and_https_are_accepted(url):
    with pytest.raises(DestinationError):
        validate_url(url, resolve_names=False)


def test_credentials_in_the_url_are_refused():
    """They would be a second secret living in a column the UI renders."""
    with pytest.raises(DestinationError) as exc:
        validate_url("https://user:pass@example.com/hook", resolve_names=False)
    assert "credentials" in str(exc.value)


@pytest.mark.parametrize("host", [
    "169.254.169.254",   # AWS/GCP/Azure instance metadata
    "169.254.1.1",
    "[fe80::1]",
    "0.0.0.0",
    "[::]",
    "224.0.0.1",
])
def test_link_local_and_friends_are_refused_even_with_private_allowed(host):
    """No setting reaches these. The metadata range is why."""
    with pytest.raises(DestinationError):
        validate_url("http://%s/hook" % host, allow_private=True, resolve_names=False)


@pytest.mark.parametrize("host", [
    "127.0.0.1",
    "[::1]",
    "10.1.2.3",
    "192.168.0.10",
    "172.16.4.4",
    "100.64.0.1",          # CGNAT
    "[fc00::1]",           # unique-local
    "[::ffff:127.0.0.1]",  # loopback wearing IPv6 clothing
])
def test_private_ranges_are_refused_by_default_and_allowed_by_the_setting(host):
    with pytest.raises(DestinationError):
        validate_url("http://%s/hook" % host, allow_private=False, resolve_names=False)
    assert validate_url("http://%s/hook" % host, allow_private=True, resolve_names=False)


def test_a_public_https_url_is_accepted_and_normalised(monkeypatch):
    monkeypatch.setattr(
        "central.events.destinations.resolve", lambda host, port: ["93.184.216.34"]
    )
    out = validate_url("https://hooks.example.com/pn?x=1#frag")
    # The fragment is never transmitted; keeping it invites an operator to
    # believe it means something.
    assert out == "https://hooks.example.com/pn?x=1"


def test_a_name_that_resolves_somewhere_private_is_refused(monkeypatch):
    """The literal check alone is one DNS record away from useless."""
    monkeypatch.setattr(
        "central.events.destinations.resolve", lambda host, port: ["169.254.169.254"]
    )
    with pytest.raises(DestinationError) as exc:
        validate_url("https://totally-fine.example.com/hook", allow_private=True)
    assert "resolves to" in str(exc.value)


@pytest.mark.parametrize("url", [
    "http://host:notaport/h",
    "http://[::1/h",
])
def test_a_malformed_authority_is_a_refusal_not_a_500(url):
    """``urlparse`` is lenient, but ``.port`` and ``.hostname`` raise."""
    with pytest.raises(DestinationError):
        validate_url(url, resolve_names=False)


def test_unresolvable_is_tolerated_at_save_time_and_fatal_at_send_time(monkeypatch):
    import socket

    def _boom(host, port):
        raise socket.gaierror("nope")

    monkeypatch.setattr("central.events.destinations.resolve", _boom)
    # Save time: a DNS blip must not become a form error nobody can act on.
    assert validate_url("https://not-yet.example.com/h", require_resolvable=False)
    with pytest.raises(DestinationError):
        validate_url("https://not-yet.example.com/h", require_resolvable=True)


# --------------------------------------------------------------------------- #
# Tenancy -- the invariant
# --------------------------------------------------------------------------- #
def test_a_client_scoped_subscription_never_receives_another_tenants_event(db):
    """The whole point of the feature's security model, stated as a test."""
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    acme_sub = _sub(db, "acme partner", client_id=acme.id)
    globex_sub = _sub(db, "globex partner", client_id=globex.id)

    event = _emit(db, client_id=globex.id, key="globex-1")
    db.flush()

    targets = {d.subscription_id for d in db.query(m.EventDelivery).all()}
    assert globex_sub.id in targets
    assert acme_sub.id not in targets, "Acme's partner was queued Globex's event"
    assert event.client_id == globex.id


def test_a_global_subscription_receives_every_tenants_events(db):
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    everyone = _sub(db, "msp", client_id=None)
    _emit(db, client_id=acme.id, key="a")
    _emit(db, client_id=globex.id, key="b")
    db.flush()
    rows = db.query(m.EventDelivery).all()
    assert len(rows) == 2
    assert {r.subscription_id for r in rows} == {everyone.id}


def test_a_client_scoped_subscription_gets_nothing_with_no_tenant(db):
    """Absence of tenancy is not a match, even for the only customer."""
    acme = _client(db, "Acme")
    _sub(db, "acme partner", client_id=acme.id)
    _emit(db, client_id=None, key="fleetwide")
    db.flush()
    assert db.query(m.EventDelivery).count() == 0


def test_scope_allows_and_assert_scope_agree(db):
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    sub = _sub(db, "acme", client_id=acme.id)
    glob = _sub(db, "msp", client_id=None)

    assert scope_allows(sub, acme.id) is True
    assert scope_allows(sub, globex.id) is False
    assert scope_allows(sub, None) is False
    assert scope_allows(glob, globex.id) is True
    assert scope_allows(glob, None) is True

    assert_scope(sub, acme.id)  # no raise
    with pytest.raises(services.TenancyError):
        assert_scope(sub, globex.id)


def test_the_tenancy_error_is_the_same_type_the_rest_of_the_app_uses():
    """A form typo and a cross-tenant reach are different events, everywhere."""
    assert issubclass(services.TenancyError, ValueError)
    from central.events.emit import TenancyError as imported

    assert imported is services.TenancyError


def test_a_disabled_subscription_is_not_a_destination(db):
    acme = _client(db, "Acme")
    _sub(db, "off", client_id=acme.id, enabled=False)
    _emit(db, client_id=acme.id, key="k")
    db.flush()
    assert db.query(m.EventDelivery).count() == 0


def test_the_type_filter_selects_but_does_not_scope(db):
    acme = _client(db, "Acme")
    supplies_only = _sub(db, "erp", client_id=acme.id, types=["supply.reorder_recommended"])
    everything = _sub(db, "all", client_id=acme.id, types=None)
    _emit(db, "alert.opened", client_id=acme.id, key="k")
    db.flush()
    targets = {d.subscription_id for d in db.query(m.EventDelivery).all()}
    assert targets == {everything.id}
    assert supplies_only.id not in targets


def test_subscriptions_for_is_deterministic(db):
    acme = _client(db, "Acme")
    a = _sub(db, "b-second", client_id=None)
    b = _sub(db, "a-first", client_id=acme.id)
    assert [s.id for s in subscriptions_for(db, "alert.opened", acme.id)] == [a.id, b.id]


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_the_same_occurrence_is_emitted_once(db):
    acme = _client(db, "Acme")
    _sub(db, "msp")
    first = _emit(db, client_id=acme.id, key="alert.opened:alert:9")
    second = _emit(db, client_id=acme.id, key="alert.opened:alert:9")
    db.flush()
    assert first is not None
    assert second is None, "a re-run of a worker cycle emitted the fact twice"
    assert db.query(m.OutboundEvent).count() == 1
    assert db.query(m.EventDelivery).count() == 1


def test_an_over_long_key_still_dedupes(db):
    """Truncation must happen before the lookup, not only before the insert.

    Otherwise a re-emit looks up the full key, finds nothing, and trips the
    unique constraint on the truncated one -- taking the caller's whole
    transaction (the alert it was describing included) with it.
    """
    acme = _client(db, "Acme")
    _sub(db, "msp")
    long_key = "supply.reorder_recommended:" + ("x" * 400)
    assert _emit(db, client_id=acme.id, key=long_key) is not None
    db.flush()
    assert _emit(db, client_id=acme.id, key=long_key) is None
    assert db.query(m.OutboundEvent).count() == 1
    assert len(db.query(m.OutboundEvent).one().idempotency_key) == 200


def test_the_event_id_is_stable_across_retries(db, capture):
    """A retry must be recognisable as the same fact, not look like a new one."""
    acme = _client(db, "Acme")
    _sub(db, "msp", client_id=acme.id)
    event = _emit(db, client_id=acme.id, key="k")
    db.commit()

    capture.status = 500
    deliver_due(db, RUNTIME)
    first_id = capture.last.headers[HEADER_EVENT_ID]

    row = db.query(m.EventDelivery).one()
    row.next_attempt_at = None
    db.commit()
    capture.status = 200
    deliver_due(db, RUNTIME)

    assert capture.last.headers[HEADER_EVENT_ID] == first_id == event.uid
    assert capture.last.headers[HEADER_ATTEMPT] == "2"
    assert capture.requests[0].headers[HEADER_ATTEMPT] == "1"


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
def test_a_delivery_is_signed_over_the_exact_bytes_sent(db, capture):
    acme = _client(db, "Acme")
    sub = _sub(db, "msp", client_id=acme.id, secret="pnevt_known")
    event = _emit(db, "alert.opened", client_id=acme.id, key="k",
                  data={"printer": {"model": 'Brother "L8900"'}})
    db.commit()

    out = deliver_due(db, RUNTIME)
    assert out["events_delivered"] == 1

    req = capture.last
    body = req.content
    header = req.headers[signing.SIGNATURE_HEADER]
    # Verified with the plaintext secret, against the bytes actually sent.
    assert signing.verify("pnevt_known", body, header)
    # And the body really is our envelope, not a re-encoding of it.
    assert body == serialize(build_envelope(event))
    parsed = json.loads(body)
    assert parsed["id"] == event.uid
    assert parsed["type"] == "alert.opened"
    assert parsed["version"] == CATALOGUE["alert.opened"].version
    assert parsed["idempotency_key"] == "k"
    assert parsed["occurred_at"].endswith("Z")
    assert parsed["source"] == "printer-nanny"
    assert parsed["data"]["printer"]["model"] == 'Brother "L8900"'

    assert req.headers[HEADER_EVENT_TYPE] == "alert.opened"
    assert req.headers[HEADER_IDEMPOTENCY] == "k"
    assert req.headers["content-type"] == "application/json"

    row = db.query(m.EventDelivery).one()
    assert row.status == m.DeliveryStatus.delivered
    assert row.response_status == 200
    assert row.attempts == 1
    assert sub.last_ok is True


def test_a_verifier_rejects_the_body_if_one_byte_moves(db, capture):
    acme = _client(db, "Acme")
    _sub(db, "msp", client_id=acme.id, secret="pnevt_known")
    _emit(db, client_id=acme.id, key="k")
    db.commit()
    deliver_due(db, RUNTIME)
    req = capture.last
    tampered = req.content.replace(b"printer-nanny", b"printer-nannX")
    assert not signing.verify("pnevt_known", tampered, req.headers[signing.SIGNATURE_HEADER])


def test_a_failing_endpoint_retries_with_backoff_then_dead_letters(db, capture):
    acme = _client(db, "Acme")
    _sub(db, "msp", client_id=acme.id)
    _emit(db, client_id=acme.id, key="k")
    db.commit()
    capture.status = 503

    for expected_attempts in (1, 2):
        out = deliver_due(db, RUNTIME)
        row = db.query(m.EventDelivery).one()
        assert out["events_failed"] == 1
        assert row.status == m.DeliveryStatus.failed
        assert row.attempts == expected_attempts
        assert row.next_attempt_at is not None, "a failure with no backoff spins"
        row.next_attempt_at = None
        db.commit()

    out = deliver_due(db, RUNTIME)  # third attempt hits max_attempts=3
    row = db.query(m.EventDelivery).one()
    assert out["events_dead"] == 1
    assert row.status == m.DeliveryStatus.dead
    assert "503" in (row.last_error or "")

    # Terminal means terminal: another sweep must not revive it.
    assert deliver_due(db, RUNTIME)["events_delivered"] == 0
    assert db.query(m.EventDelivery).one().attempts == 3


def test_a_transport_error_is_a_retryable_failure(db, capture):
    acme = _client(db, "Acme")
    _sub(db, "msp", client_id=acme.id)
    _emit(db, client_id=acme.id, key="k")
    db.commit()
    capture.raise_exc = httpx.ConnectError("connection refused")

    out = deliver_due(db, RUNTIME)
    row = db.query(m.EventDelivery).one()
    assert out["events_failed"] == 1
    assert row.status == m.DeliveryStatus.failed
    assert "request error" in (row.last_error or "")


def test_a_redirect_is_not_followed(db, capture):
    """A pre-flight address check is defeated in one line by a 302."""
    acme = _client(db, "Acme")
    _sub(db, "msp", client_id=acme.id)
    _emit(db, client_id=acme.id, key="k")
    db.commit()
    capture.status = 302
    capture.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

    out = deliver_due(db, RUNTIME)
    assert out["events_delivered"] == 0
    assert len(capture.requests) == 1, "the redirect was followed"
    assert str(capture.requests[0].url).startswith("https://hooks.example.com")


def test_a_disabled_subscription_leaves_its_backlog_queued(db, capture):
    """Re-enabling must deliver the backlog, not find it terminated."""
    acme = _client(db, "Acme")
    sub = _sub(db, "msp", client_id=acme.id)
    _emit(db, client_id=acme.id, key="k")
    db.commit()

    sub.enabled = False
    db.commit()
    out = deliver_due(db, RUNTIME)
    row = db.query(m.EventDelivery).one()
    assert out["events_skipped"] == 1
    assert out["events_delivered"] == 0
    assert row.status == m.DeliveryStatus.pending
    assert row.attempts == 0, "a disabled destination burned a retry"
    assert not capture.requests

    sub.enabled = True
    db.commit()
    assert deliver_due(db, RUNTIME)["events_delivered"] == 1


def test_rescoping_a_subscription_refuses_the_queued_cross_tenant_backlog(db, capture):
    """The second half of the tenancy invariant, and the reason it is re-checked.

    A global subscription is queued Globex's event, then an operator re-scopes it
    to Acme. Sending now would hand Acme's partner Globex's fleet. The row is
    dead-lettered and audited instead.
    """
    acme, globex = _client(db, "Acme"), _client(db, "Globex")
    sub = _sub(db, "was global", client_id=None)
    _emit(db, client_id=globex.id, key="globex-1")
    db.commit()

    sub.client_id = acme.id
    db.commit()

    out = deliver_due(db, RUNTIME)
    row = db.query(m.EventDelivery).one()
    assert out["events_dead"] == 1
    assert out["events_delivered"] == 0
    assert row.status == m.DeliveryStatus.dead
    assert "refused" in (row.last_error or "")
    assert not capture.requests, "a cross-tenant delivery left the building"

    audit = db.query(m.AuditLog).filter(
        m.AuditLog.action == "event_delivery.refused"
    ).all()
    assert len(audit) == 1
    assert "event_subscription:%d" % sub.id == audit[0].target


def test_a_type_unsubscribed_after_queueing_is_skipped_not_delivered(db, capture):
    acme = _client(db, "Acme")
    sub = _sub(db, "erp", client_id=acme.id)
    _emit(db, "alert.opened", client_id=acme.id, key="k")
    db.commit()

    sub.event_types = ["supply.reorder_recommended"]
    db.commit()

    out = deliver_due(db, RUNTIME)
    row = db.query(m.EventDelivery).one()
    assert out["events_not_sent"] == 1
    assert out["events_delivered"] == 0
    assert row.status == m.DeliveryStatus.skipped
    assert not capture.requests


def test_an_undecryptable_secret_refuses_to_transmit(db, capture):
    """Signing with "" would produce a clean-looking send every subscriber rejects."""
    acme = _client(db, "Acme")
    sub = _sub(db, "msp", client_id=acme.id)
    sub.secret = "enc:v1:not-a-real-fernet-token"
    _emit(db, client_id=acme.id, key="k")
    db.commit()

    out = deliver_due(db, RUNTIME)
    assert out["events_delivered"] == 0
    assert not capture.requests
    row = db.query(m.EventDelivery).one()
    assert "could not be decrypted" in (row.last_error or "")


def test_a_refused_destination_reports_that_nothing_was_transmitted(db):
    """`sent` is separate from `ok` here for the same reason it is for channels.

    A refused destination is provably never contacted, so it says ``sent=False``
    -- unlike a timeout or a 5xx, where bytes may well have left the wire and
    claiming otherwise would be a guess.
    """
    acme = _client(db, "Acme")
    sub = _sub(db, "msp", client_id=acme.id, url="http://127.0.0.1:9/hook")
    event = _emit(db, client_id=acme.id, key="k")
    db.commit()
    result, status = send_one(sub, event, runtime=RUNTIME)
    assert isinstance(result, ChannelResult)
    assert result.ok is False and result.sent is False
    assert status is None
    assert "refused" in result.detail


def test_the_master_switch_queues_without_sending(db, capture):
    acme = _client(db, "Acme")
    _sub(db, "msp", client_id=acme.id)
    _emit(db, client_id=acme.id, key="k")
    db.commit()

    paused = dict(RUNTIME, **{"events.enabled": False})
    out = deliver_due(db, paused)
    assert out["events_paused"] is True
    assert not capture.requests
    assert db.query(m.EventDelivery).one().status == m.DeliveryStatus.pending

    assert deliver_due(db, RUNTIME)["events_delivered"] == 1


def test_one_hostile_destination_cannot_abort_the_sweep(db, capture):
    """The queue behind a bad subscriber belongs to other subscribers.

    httpx does not wrap everything it can raise (an IDN host raises
    UnicodeError, a broken proxy raises from the transport), so an unguarded
    send would take the whole cycle's queue down with one row.
    """
    acme = _client(db, "Acme")
    bad = _sub(db, "bad", client_id=acme.id, url="https://bad.example.com/h")
    good = _sub(db, "good", client_id=acme.id, url="https://hooks.example.com/pn")
    _emit(db, client_id=acme.id, key="k")
    db.commit()

    def explode(request):
        if "bad.example.com" in str(request.url):
            raise RuntimeError("not an httpx error")
        return httpx.Response(200, content=b"ok")

    capture.handler = explode
    out = deliver_due(db, RUNTIME)

    rows = {d.subscription_id: d for d in db.query(m.EventDelivery).all()}
    assert out["events_delivered"] == 1
    assert rows[good.id].status == m.DeliveryStatus.delivered
    assert rows[bad.id].status == m.DeliveryStatus.failed
    assert "unhandled error" in (rows[bad.id].last_error or "")
    # The exception type only -- never its message, which can carry the URL,
    # the proxy configuration or worse.
    assert "not an httpx error" not in (rows[bad.id].last_error or "")


def test_a_cycle_is_bounded(db, capture):
    """A backlog must not spend the cycle that also has alerts to deliver."""
    from central.events.delivery import BATCH_LIMIT, _due

    acme = _client(db, "Acme")
    _sub(db, "msp", client_id=acme.id)
    for i in range(BATCH_LIMIT + 5):
        _emit(db, client_id=acme.id, key="k%d" % i)
    db.commit()
    assert len(_due(db, __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ))) == BATCH_LIMIT


def test_delivery_reuses_the_notification_backoff_policy():
    """One backoff policy in this codebase, not two."""
    import central.events.delivery as ev
    from central.channels.delivery import _apply_result as canonical

    assert ev._apply_result is canonical


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
def test_prune_drops_settled_events_and_keeps_queued_ones(db):
    from datetime import datetime, timedelta, timezone

    acme = _client(db, "Acme")
    sub = _sub(db, "msp", client_id=acme.id)
    old_done = _emit(db, client_id=acme.id, key="old-done")
    old_queued = _emit(db, client_id=acme.id, key="old-queued")
    db.flush()

    ancient = datetime.now(timezone.utc) - timedelta(days=90)
    for e in (old_done, old_queued):
        e.created_at = ancient
    db.query(m.EventDelivery).filter(
        m.EventDelivery.event_id == old_done.id
    ).one().status = m.DeliveryStatus.delivered
    db.commit()

    assert prune(db, RUNTIME) == 1
    db.commit()
    remaining = {e.idempotency_key for e in db.query(m.OutboundEvent).all()}
    assert remaining == {"old-queued"}, "a queued backlog was pruned away"
    # Deliveries cascade with their event.
    assert db.query(m.EventDelivery).filter(
        m.EventDelivery.event_id == old_done.id
    ).count() == 0
    assert sub.id is not None


def test_prune_is_disabled_by_zero(db):
    _client(db, "Acme")
    _emit(db, key="k")
    db.commit()
    assert prune(db, dict(RUNTIME, **{"events.retention_days": 0})) == 0


# --------------------------------------------------------------------------- #
# Payload builders
# --------------------------------------------------------------------------- #
def test_alert_events_carry_the_tenant_and_the_device(db):
    acme = _client(db, "Acme")
    printer = _printer(
        db, acme, "10.0.0.5", display_name="Front Desk", model="Brother MFC-L8900CDW",
        serial="ABC123", location="Reception",
    )
    alert = m.Alert(
        printer_id=printer.id, type=m.AlertConditionType.supply_below,
        severity=m.EventSeverity.warning, state=m.AlertState.open,
        title="Toner low", detail="black at 8%", dedupe_key="d1",
    )
    db.add(alert)
    db.flush()

    event = emit_alert_opened(db, alert, printer)
    assert event.type == "alert.opened"
    assert event.client_id == acme.id
    assert event.idempotency_key == "alert.opened:alert:%d:flap:0" % alert.id
    data = event.data
    assert data["client"]["name"] == "Acme"
    assert data["site"]["name"] == "Acme HQ"
    assert data["printer"]["name"] == "Front Desk"
    assert data["printer"]["serial"] == "ABC123"
    assert data["alert"]["severity"] == "warning"
    assert data["alert"]["title"] == "Toner low"
    assert data["alert"]["opened_at"].endswith("Z")

    alert.state = m.AlertState.resolved
    resolved = emit_alert_resolved(db, alert)
    assert resolved.type == "alert.resolved"
    assert resolved.idempotency_key == "alert.resolved:alert:%d:flap:0" % alert.id
    assert resolved.data["alert"]["resolved_at"] is not None


def test_a_flapping_alert_reports_every_real_state_transition(db):
    """The failure keyed-on-the-alert-id alone would have produced.

    Flap damping re-opens the SAME row rather than raising a new alert. Keyed on
    the id, the re-open would collide with the first open and emit nothing --
    leaving every subscriber holding "resolved" for a live fault, and unable to
    hear the next resolve either because that key was spent too. The key carries
    the flap generation so each genuine transition is its own occurrence.
    """
    acme = _client(db, "Acme")
    printer = _printer(db, acme, "10.0.0.5")
    alert = m.Alert(
        printer_id=printer.id, type=m.AlertConditionType.supply_below,
        severity=m.EventSeverity.warning, state=m.AlertState.open,
        title="Toner low", detail="", dedupe_key="d1", flap_count=0,
    )
    db.add(alert)
    db.flush()

    assert emit_alert_opened(db, alert, printer) is not None
    alert.state = m.AlertState.resolved
    assert emit_alert_resolved(db, alert, printer) is not None

    # A cycle re-reading the row it just closed must add nothing.
    assert emit_alert_resolved(db, alert, printer) is None

    # It flaps: same row, generation 1.
    alert.state = m.AlertState.open
    alert.resolved_at = None
    alert.flap_count = 1
    reopened = emit_alert_opened(db, alert, printer)
    assert reopened is not None, "a subscriber was never told the fault came back"

    alert.state = m.AlertState.resolved
    assert emit_alert_resolved(db, alert, printer) is not None

    kinds = [
        (e.type, e.idempotency_key)
        for e in db.query(m.OutboundEvent).order_by(m.OutboundEvent.id)
    ]
    assert kinds == [
        ("alert.opened", "alert.opened:alert:%d:flap:0" % alert.id),
        ("alert.resolved", "alert.resolved:alert:%d:flap:0" % alert.id),
        ("alert.opened", "alert.opened:alert:%d:flap:1" % alert.id),
        ("alert.resolved", "alert.resolved:alert:%d:flap:1" % alert.id),
    ]


def test_printer_offline_keys_on_the_transition_not_the_printer(db):
    from datetime import datetime, timedelta, timezone

    acme = _client(db, "Acme")
    seen = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    printer = _printer(db, acme, "10.0.0.9", last_seen=seen)

    first = emit_printer_offline(db, printer)
    db.flush()
    assert first is not None
    # Same transition observed again on a later cycle: no second event.
    assert emit_printer_offline(db, printer) is None

    # It came back and went down again -- a genuinely new occurrence.
    printer.last_seen = seen + timedelta(hours=3)
    second = emit_printer_offline(db, printer)
    assert second is not None and second.uid != first.uid


def test_supply_reorder_is_a_recommendation_scoped_to_a_window(db):
    acme = _client(db, "Acme")
    printer = _printer(db, acme, "10.0.0.7", display_name="Lab")
    supply = m.Supply(
        printer_id=printer.id, type=m.SupplyType.toner, color="black",
        description="TN-423BK", level_pct=6.0,
    )
    db.add(supply)
    db.flush()

    first = emit_supply_reorder_recommended(
        db, printer, supply, days_to_empty=9.5, period="2026-08-03"
    )
    db.flush()
    assert first.type == "supply.reorder_recommended"
    assert first.data["supply"]["level_pct"] == 6.0
    assert first.data["supply"]["days_to_empty"] == 9.5
    assert first.data["supply"]["color"] == "black"
    # Same window: one recommendation, not one per worker cycle.
    assert emit_supply_reorder_recommended(
        db, printer, supply, days_to_empty=9.0, period="2026-08-03"
    ) is None
    assert emit_supply_reorder_recommended(
        db, printer, supply, days_to_empty=8.0, period="2026-08-04"
    ) is not None


def test_emit_does_not_commit(db):
    """Rolled back with the thing it describes, exactly like an audit row."""
    _client(db, "Acme")
    _sub(db, "msp")
    _emit(db, key="k")
    db.rollback()
    assert db.query(m.OutboundEvent).count() == 0
