"""The agent half of server-pushed definitions: matching, precedence, degrading.

The property this file exists to protect is the precedence rule, because it is
the one that decides whether this feature can break a working fleet:

    built-in providers run FIRST; a definition runs LAST and fills only.

Everything else here is in service of that, plus the two things that must be
true for the feature to be safe to ship at all: an agent with no definitions is
byte-for-byte the agent that shipped before, and an agent that cannot fetch
them keeps whatever it had.
"""

from __future__ import annotations

import copy

import httpx
import pytest

from printer_nanny_agent import definitions as defs
from printer_nanny_agent import oids
from printer_nanny_agent.client import CentralClient
from printer_nanny_agent.poller import poll_printer
from printer_nanny_agent.providers import definitions as provider
from printer_nanny_agent.runner import DefinitionSync
from printer_nanny_agent.snmp import SnmpError, SnmpParams
from tests.fakes import FakeSnmpBackend, canned_printer

_PRIVATE_TONER = "1.3.6.1.4.1.12345.1.4.1.1"
_PRIVATE_DRUM = "1.3.6.1.4.1.12345.1.4.2.1"
_PRIVATE_DRUM_MAX = "1.3.6.1.4.1.12345.1.4.2.2"
_PRIVATE_METER = "1.3.6.1.4.1.12345.1.6.1"
_PRIVATE_PANEL = "1.3.6.1.4.1.12345.1.1.3"

_ACME_SYSOID = "1.3.6.1.4.1.12345.1.1"


@pytest.fixture(autouse=True)
def _no_leaked_definitions():
    """The active set is module-global (see providers/definitions.py for why).
    Leaking one into another test file would change how every other agent test
    parses a printer -- exactly the action-at-a-distance this suite must not
    have."""
    provider.clear()
    yield
    provider.clear()


def acme_device(*, black_level: int = -3, sys_oid: str = _ACME_SYSOID, **extra_scalars):
    """A device whose standard MIB reports a BUCKET, not a number.

    ``-3`` is RFC 3805's "some remaining" sentinel -- the real-world case this
    whole feature is for. The exact percentage lives only in a vendor-private
    OID, which today needs an agent release to read.
    """
    device = canned_printer(
        name="acme-front", model="Acme AC-9000dn", serial="AC900012345",
        black_level=black_level,
    )
    device["scalars"][oids.SYS_OBJECT_ID] = sys_oid
    device["scalars"].update(extra_scalars)
    return device


async def poll(device, ip="10.0.0.9"):
    return await poll_printer(FakeSnmpBackend({ip: device}), ip, SnmpParams())


ACME_DEFINITION = {
    "key": "acme-9000-series",
    "match": {"enterprise": "12345", "model": "AC-9000"},
    "supplies": [
        {"oid": _PRIVATE_TONER, "type": "toner", "color": "black",
         "description": "Black Toner", "decode": {"kind": "percent"}},
        {"oid": _PRIVATE_DRUM, "type": "drum",
         "decode": {"kind": "ratio", "max_oid": _PRIVATE_DRUM_MAX}},
    ],
    "meters": [{"oid": _PRIVATE_METER, "meter": "mono"}],
    "status_text": {"oid": _PRIVATE_PANEL},
}


# --------------------------------------------------------------------------- #
# The demonstration: a printer parsed by a pushed definition
# --------------------------------------------------------------------------- #
async def test_a_pushed_definition_parses_a_printer_that_was_a_bucket_before():
    """This is the feature. Same device, same agent build, two outcomes -- the
    only difference is a row somebody added on the server."""
    device = acme_device(**{
        _PRIVATE_TONER: "47",
        _PRIVATE_DRUM: "12000", _PRIVATE_DRUM_MAX: "30000",
        _PRIVATE_METER: "88213",
        _PRIVATE_PANEL: "Ready",
    })

    before = await poll(copy.deepcopy(device))
    black = next(s for s in before["supplies"] if s["color"] == "black")
    assert black["level_pct"] is None
    assert black["status_note"] == "some remaining"
    assert not any(s["type"] == "drum" for s in before["supplies"])
    assert before["mono_count"] is None
    assert before.get("provider_trace") is None

    assert provider.set_active([ACME_DEFINITION]) == 1
    after = await poll(copy.deepcopy(device))

    black = next(s for s in after["supplies"] if s["color"] == "black")
    assert black["level_pct"] == 47.0
    # The bucket note went with it: "some remaining" beside "47%" is a
    # contradiction, and it described the absence of the number we now have.
    assert black["status_note"] is None

    drum = next(s for s in after["supplies"] if s["type"] == "drum")
    assert drum["level_pct"] == 40.0  # 12000 / 30000
    assert drum["description"] == "drum"

    assert after["mono_count"] == 88213
    assert after["meter_snapshot"] == {"mono": 88213}
    assert after["device_status_text"] == "Ready"
    assert after["_supply_precision"] == "definition:acme-9000-series"

    trace = after["provider_trace"][-1]
    assert trace["name"] == "definition"
    assert trace["ok"] is True
    assert "definition=acme-9000-series" in trace["summary"]
    assert any("47%" in line for line in trace["changed"])


async def test_with_no_definitions_the_reading_is_what_it_always_was():
    """Degrading safely, asserted as an identity rather than a vibe: the whole
    reading is compared, so a stray trace row or a new key would fail."""
    device = acme_device(**{_PRIVATE_TONER: "47"})
    baseline = await poll(copy.deepcopy(device))

    provider.set_active([])
    again = await poll(copy.deepcopy(device))

    baseline.pop("ts")
    again.pop("ts")
    assert again == baseline
    assert "provider_trace" not in again


async def test_a_definition_that_matches_nothing_leaves_no_trace_row():
    """detect() answers with the match, not merely with 'definitions exist', so
    an unrelated printer costs nothing -- not an SNMP round trip, not a row on
    its diagnostics panel."""
    provider.set_active([ACME_DEFINITION])
    other = canned_printer(model="HP LaserJet M404")
    other["scalars"][oids.SYS_OBJECT_ID] = "1.3.6.1.4.1.11.2.3.9.1"
    reading = await poll(other)
    assert not any(t["name"] == "definition" for t in reading.get("provider_trace") or [])


# --------------------------------------------------------------------------- #
# Precedence -- the decision
# --------------------------------------------------------------------------- #
def _reading_with_builtin_value(pct=62.0):
    return {
        "ip": "10.0.0.9", "brand": "Acme", "model": "Acme AC-9000dn",
        "supplies": [
            {"type": "toner", "color": "black", "description": "Black",
             "level_pct": pct, "status_note": None},
        ],
        "mono_count": 1000,
        "device_status_text": "Set by a built-in",
    }


def test_a_definition_never_silently_replaces_a_builtin_value():
    """The rule that keeps this feature from breaking a working printer. A
    built-in ran first and produced 62%; the definition reads 47% and must NOT
    win -- and must say that it held."""
    definition = defs.validate_definition(ACME_DEFINITION)
    reading = _reading_with_builtin_value()
    report = provider.apply_definition(
        definition,
        {_PRIVATE_TONER: "47", _PRIVATE_METER: "9999", _PRIVATE_PANEL: "Jam"},
        reading,
    )
    assert reading["supplies"][0]["level_pct"] == 62.0
    assert reading["mono_count"] == 1000
    assert reading["device_status_text"] == "Set by a built-in"
    assert report["held"] == ["black toner 62%", "meter mono=1000", "status text"]
    assert report["overrode"] == []


def test_override_builtin_wins_but_never_quietly():
    """The escape hatch, and the price of it: every displaced field is named, in
    the report that becomes provider_trace on the printer's detail page."""
    definition = defs.validate_definition(dict(ACME_DEFINITION, override_builtin=True))
    reading = _reading_with_builtin_value()
    report = provider.apply_definition(
        definition,
        {_PRIVATE_TONER: "47", _PRIVATE_METER: "9999", _PRIVATE_PANEL: "Jam"},
        reading,
    )
    assert reading["supplies"][0]["level_pct"] == 47.0
    assert reading["mono_count"] == 9999
    assert reading["device_status_text"] == "Jam"
    assert report["overrode"] == [
        "black toner 62% -> 47%", "meter mono 1000 -> 9999", "status text",
    ]


async def test_an_override_is_announced_on_the_reading_itself():
    """'Never silently' has to reach the operator, not just the log. The trace
    lands on the printer's own detail page."""
    provider.set_active([dict(ACME_DEFINITION, override_builtin=True)])
    device = acme_device(black_level=5000, **{_PRIVATE_TONER: "47"})
    reading = await poll(device)
    black = next(s for s in reading["supplies"] if s["color"] == "black")
    assert black["level_pct"] == 47.0  # standard MIB said 50%
    assert "black toner 50% -> 47%" in reading["_definition_overrode"]
    assert "overrode=black toner 50% -> 47%" in reading["provider_trace"][-1]["summary"]


def test_the_definition_provider_is_registered_last():
    """Registration order IS the precedence rule -- run_providers iterates the
    registry in order. If this ever reorders, definitions start overwriting
    built-ins before they have run."""
    from printer_nanny_agent.providers import providers

    names = [p.name for p in providers()]
    assert names[-1] == "definition"
    assert "brother" in names[:-1] and "hp" in names[:-1]


def test_a_definition_fills_a_supply_the_builtin_left_empty():
    definition = defs.validate_definition(ACME_DEFINITION)
    reading = _reading_with_builtin_value(pct=None)
    reading["supplies"][0]["status_note"] = "some remaining"
    reading["mono_count"] = None
    reading.pop("device_status_text")
    report = provider.apply_definition(
        definition, {_PRIVATE_TONER: "47", _PRIVATE_METER: "7", _PRIVATE_PANEL: "Ready"},
        reading,
    )
    assert reading["supplies"][0]["level_pct"] == 47.0
    assert report["filled"] == ["black toner 47%", "meter mono=7", "status text"]
    assert report["held"] == []


def test_a_supply_the_definition_could_not_decode_is_dropped_not_guessed():
    definition = defs.validate_definition(ACME_DEFINITION)
    reading = {"supplies": []}
    report = provider.apply_definition(definition, {_PRIVATE_TONER: "250"}, reading)
    assert reading["supplies"] == []
    assert "black toner" in report["dropped"]


def test_a_definition_never_appends_a_row_central_would_collapse():
    """Central's upsert_supply keys on (type, color). A looser match here would
    append a row central then merges onto the existing one -- which would be an
    overwrite wearing an append's clothing, defeating the fill-only rule."""
    definition = defs.validate_definition(ACME_DEFINITION)
    reading = _reading_with_builtin_value()
    provider.apply_definition(definition, {_PRIVATE_TONER: "47"}, reading)
    keys = [(s["type"], s.get("color")) for s in reading["supplies"]]
    assert keys == [("toner", "black")]


@pytest.mark.parametrize("raw,expected", [
    ("47", 47.0), ("0", 0.0), ("100", 100.0),
    # Out of range is a WRONG decode, not a full cartridge. Clamping here would
    # report "full" for a misread -- a printer that runs dry with a green dot.
    ("101", None), ("-1", None), ("", None), ("forty", None), (None, None),
])
def test_percent_decoding_refuses_rather_than_clamps(raw, expected):
    assert provider.decode_percent({"kind": "percent"}, {"o": raw}, "o") == expected


def test_ratio_refuses_a_reading_above_its_own_maximum():
    decode = {"kind": "ratio", "max": 100}
    assert provider.decode_percent(decode, {"o": "50"}, "o") == 50.0
    assert provider.decode_percent(decode, {"o": "101"}, "o") is None
    assert provider.decode_percent(decode, {"o": "-1"}, "o") is None
    assert provider.decode_percent({"kind": "ratio", "max_oid": "m"},
                                   {"o": "5", "m": "0"}, "o") is None


def test_map_matches_exactly_and_nothing_else():
    decode = {"kind": "map", "values": {"1": 100, "2": 50, "3": 0}}
    assert provider.decode_percent(decode, {"o": "2"}, "o") == 50.0
    assert provider.decode_percent(decode, {"o": "4"}, "o") is None
    assert provider.decode_percent(decode, {"o": "2 "}, "o") == 50.0  # raw is stripped
    assert provider.decode_percent(decode, {"o": "22"}, "o") is None


def test_a_hex_rendered_octet_string_is_decoded_like_everywhere_else():
    """pysnmp renders a binary OCTET STRING as '0x...'. A definition reading a
    front-panel message must get the text, not the blob."""
    decode = {"kind": "text_between", "after": "T:", "before": "%"}
    # "T:47% left"
    assert provider.decode_percent(decode, {"o": "0x543a3437252c6c656674"}, "o") == 47.0


def test_a_meter_that_would_overflow_central_is_dropped():
    """Central drops an out-of-range meter at ingest anyway, but a value that
    overflows INT4 would 500 the batch on some paths. Refused here too."""
    assert provider.decode_count({"o": str(2**31)}, "o") is None
    assert provider.decode_count({"o": "-1"}, "o") is None
    assert provider.decode_count({"o": "88213"}, "o") == 88213


def test_a_non_core_meter_lands_in_the_snapshot_not_in_a_column():
    definition = defs.validate_definition({
        "key": "fn-meters", "match": {"model": "ac-9000"},
        "meters": [
            {"oid": "1.1.1", "meter": "print"},
            {"oid": "1.1.2", "meter": "copy"},
            {"oid": "1.1.3", "meter": "color"},
        ],
    })
    reading = {"supplies": []}
    provider.apply_definition(definition, {"1.1.1": "10", "1.1.2": "20", "1.1.3": "30"}, reading)
    assert reading["color_count"] == 30
    assert reading["meter_snapshot"] == {"print": 10, "copy": 20, "color": 30}
    assert "print" not in reading


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _d(key, **match):
    return defs.validate_definition(
        {"key": key, "match": match, "supplies": [{"oid": "1.1.1", "type": "toner"}]}
    )


def test_enterprise_brand_and_model_criteria_are_all_required_to_match():
    definition = _d("strict", enterprise="12345", brand="acme", model="ac-9000")
    reading = {"brand": "Acme", "model": "Acme AC-9000dn"}
    assert provider.definition_matches(definition, reading, _ACME_SYSOID)
    assert not provider.definition_matches(definition, reading, "1.3.6.1.4.1.11.2")
    assert not provider.definition_matches(
        definition, {"brand": "Acme", "model": "AC-8000"}, _ACME_SYSOID)
    assert not provider.definition_matches(
        definition, {"brand": "Other", "model": "AC-9000"}, _ACME_SYSOID)


def test_enterprise_matching_is_the_same_code_the_builtins_use():
    """Two implementations of 'is this a Brother?' agree right up until one of
    them is fixed, so there is only one."""
    from printer_nanny_agent.providers import brother, enterprise_matches

    for sys_oid in ("1.3.6.1.4.1.2435.2.3.9.1", "enterprises.2435.2.3", "1.3.6.1.4.1.2435"):
        assert enterprise_matches("2435", sys_oid)
        assert brother.BrotherProvider().detect({}, sys_oid)
    assert not enterprise_matches("2435", "1.3.6.1.4.1.24350.1")


def test_the_most_specific_definition_wins():
    family = _d("acme-family", brand="acme")
    model = _d("acme-9000", brand="acme", model="ac-9000")
    reading = {"brand": "Acme", "model": "Acme AC-9000dn"}
    assert provider.select_definition([family, model], reading, None)["key"] == "acme-9000"
    assert provider.select_definition([model, family], reading, None)["key"] == "acme-9000"


def test_an_exact_tie_is_refused_rather_than_guessed(caplog):
    """Two definitions over one device's supply rows is a coin flip whose losing
    side looks exactly like data. Same discipline as an ambiguous driver
    package."""
    one = _d("acme-a", model="ac-9000")
    two = _d("acme-b", model="ac-9000")
    reading = {"ip": "10.0.0.9", "brand": "Acme", "model": "Acme AC-9000dn"}
    with caplog.at_level("WARNING"):
        assert provider.select_definition([one, two], reading, None) is None
    assert "refusing to guess" in caplog.text
    assert "acme-a, acme-b" in caplog.text


def test_a_model_tag_beats_a_longer_brand_tag():
    """Specificity is a tuple, not a sum: a model criterion is more specific
    than a brand one however many characters the brand has."""
    brand_only = _d("by-brand", brand="acme corporation international")
    by_model = _d("by-model", model="ac-9")
    reading = {"brand": "Acme Corporation International", "model": "AC-9000"}
    assert provider.select_definition(
        [brand_only, by_model], reading, None)["key"] == "by-model"


def test_matching_uses_the_identity_a_builtin_may_have_set():
    """HP's provider writes identity.brand when sysDescr was unhelpful. A
    definition must see the same brand the rest of the reading will."""
    definition = _d("by-brand", brand="acme")
    assert provider.definition_matches(definition, {"identity": {"brand": "Acme"}}, None)


# --------------------------------------------------------------------------- #
# SNMP failure
# --------------------------------------------------------------------------- #
async def test_a_device_that_will_not_answer_the_private_oids_still_ships_its_reading():
    """The ordinary case for a definition aimed at a sibling model. The standard
    reading is the contract every provider keeps."""
    provider.set_active([ACME_DEFINITION])

    class Refusing(FakeSnmpBackend):
        async def get(self, host, oid_list, params):
            if any(o.startswith("1.3.6.1.4.1.12345.1.4") for o in oid_list):
                raise SnmpError("no response")
            return await super().get(host, oid_list, params)

    backend = Refusing({"10.0.0.9": acme_device()})
    reading = await poll_printer(backend, "10.0.0.9", SnmpParams())
    assert reading["page_count"] == 84231
    assert reading["_definition_note"] == "snmp read failed"
    trace = reading["provider_trace"][-1]
    assert trace["ok"] is True  # not a provider failure; the device just said no


# --------------------------------------------------------------------------- #
# Cache + sync
# --------------------------------------------------------------------------- #
class FakeClient:
    """Stands in for CentralClient. Records what version the agent asked with."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.asked = []

    async def get_device_definitions(self, since=""):
        self.asked.append(since)
        item = self._responses.pop(0) if self._responses else {"changed": False}
        if isinstance(item, Exception):
            raise item
        return item


def _signed(rows, key_hash):
    validated = defs.validate_feed(rows)
    version = defs.feed_version(validated)
    return {
        "version": version, "changed": True, "definitions": validated,
        "signature": defs.payload_signature(key_hash, version, validated),
    }


def _sync(tmp_path, *responses, api_key="secret"):
    store = provider.DefinitionStore(str(tmp_path / "defs.json"))
    return DefinitionSync(store, api_key), FakeClient(*responses), store


async def test_a_signed_feed_is_applied_and_cached(tmp_path):
    fingerprint = provider.key_fingerprint("secret")
    sync, client, store = _sync(tmp_path, _signed([ACME_DEFINITION], fingerprint))
    assert await sync.refresh(client) == 1
    assert [d["key"] for d in provider.active()] == ["acme-9000-series"]
    # ...and it survives a restart with central unreachable.
    provider.clear()
    restarted = DefinitionSync(store, "secret")
    assert restarted.load_cache() == 1
    assert [d["key"] for d in provider.active()] == ["acme-9000-series"]
    # The version comes back with it, so a restart does not force a re-download.
    assert restarted.version == sync.version


async def test_the_agent_sends_the_version_it_holds_and_no_change_costs_nothing(tmp_path):
    fingerprint = provider.key_fingerprint("secret")
    feed = _signed([ACME_DEFINITION], fingerprint)
    sync, client, _ = _sync(tmp_path, feed, {"version": feed["version"], "changed": False})
    assert await sync.refresh(client) == 1
    assert await sync.refresh(client) is None
    assert client.asked == ["", feed["version"]]
    assert len(provider.active()) == 1


async def test_an_unreachable_central_keeps_the_definitions_already_active(tmp_path):
    fingerprint = provider.key_fingerprint("secret")
    sync, client, _ = _sync(
        tmp_path, _signed([ACME_DEFINITION], fingerprint), httpx.ConnectError("down"),
    )
    assert await sync.refresh(client) == 1
    assert await sync.refresh(client) is None
    assert len(provider.active()) == 1


async def test_a_feed_that_fails_its_signature_changes_nothing(tmp_path, caplog):
    """Fail closed onto what we already have. Failing forward to 'no
    definitions' would let anyone who can break the response turn the feature
    off fleet-wide."""
    fingerprint = provider.key_fingerprint("secret")
    forged = _signed([dict(ACME_DEFINITION, key="attacker-supplied")], "wrong-key")
    sync, client, _ = _sync(tmp_path, _signed([ACME_DEFINITION], fingerprint), forged)
    assert await sync.refresh(client) == 1
    with caplog.at_level("WARNING"):
        assert await sync.refresh(client) is None
    assert "signature" in caplog.text
    assert [d["key"] for d in provider.active()] == ["acme-9000-series"]


async def test_a_feed_that_legitimately_went_empty_does_take_effect(tmp_path):
    """The reason the changed case sends the full set rather than a delta: an
    operator who disables a definition needs it to STOP being applied, and
    there is no tombstone to miss."""
    fingerprint = provider.key_fingerprint("secret")
    sync, client, _ = _sync(
        tmp_path, _signed([ACME_DEFINITION], fingerprint), _signed([], fingerprint),
    )
    assert await sync.refresh(client) == 1
    assert await sync.refresh(client) == 0
    assert provider.active() == ()


async def test_a_malformed_feed_changes_nothing(tmp_path):
    fingerprint = provider.key_fingerprint("secret")
    sync, client, _ = _sync(
        tmp_path,
        _signed([ACME_DEFINITION], fingerprint),
        {"changed": True, "version": 7, "definitions": "not a list"},
    )
    assert await sync.refresh(client) == 1
    assert await sync.refresh(client) is None
    assert len(provider.active()) == 1


def test_the_cache_is_written_owner_only(tmp_path):
    """It holds no secret today, but it decides what every printer at this site
    is read with, and a world-writable one would be a way to change that."""
    import os
    import stat

    path = tmp_path / "defs.json"
    store = provider.DefinitionStore(str(path))
    fingerprint = provider.key_fingerprint("secret")
    assert store.save(_signed([ACME_DEFINITION], fingerprint))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# --------------------------------------------------------------------------- #
# The HTTP client
# --------------------------------------------------------------------------- #
def _client(handler) -> CentralClient:
    """A real CentralClient with only its transport swapped.

    Replacing the whole AsyncClient would drop the Authorization header the
    constructor installs, and the test asserting that header would then be
    testing the test.
    """
    client = CentralClient("https://central.example", 7, "secret")
    client._client._transport = httpx.MockTransport(handler)
    return client


async def test_a_central_that_predates_the_feature_is_not_an_error():
    """A 404 is 'no definitions' -- the state this whole feature degrades to
    safely -- not something worth a traceback in an operator's log."""
    client = _client(lambda request: httpx.Response(404))
    assert await client.get_device_definitions("abc") == {"version": "abc", "changed": False}
    await client.aclose()


async def test_an_oversized_feed_is_refused_by_its_bytes_before_it_is_parsed():
    """The cap is on raw bytes. By the time a bomb has a shape, it has already
    cost whatever it was going to cost."""
    body = b'{"definitions": [' + b'0,' * (defs.MAX_PAYLOAD_BYTES // 2) + b'0]}'
    client = _client(lambda request: httpx.Response(200, content=body))
    with pytest.raises(ValueError, match="over the"):
        await client.get_device_definitions()
    await client.aclose()


async def test_the_version_the_agent_holds_travels_in_the_request():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"version": "v2", "changed": False})

    client = _client(handler)
    await client.get_device_definitions("v1")
    assert "since=v1" in seen["url"]
    assert seen["url"].startswith("https://central.example/api/v1/agents/7/device-definitions")
    assert seen["auth"] == "Bearer secret"
    await client.aclose()
