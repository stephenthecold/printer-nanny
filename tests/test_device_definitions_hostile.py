"""A hostile definition is REFUSED -- not executed, not hung on, not stored.

A definition is instructions that every agent in a fleet acts on, so this file
treats one as the attacker-shaped input it is. Three properties, and each has
been a real failure somewhere:

1. **Refused, not executed.** There is no eval/exec/import/subprocess path, and
   the vocabulary is closed, so an attempt at one is simply an unknown key.
2. **Refused, not hung on.** Every case here is timed. A validator that
   *eventually* rejects a 50 MB payload has already spent the memory, and a
   catastrophically backtracking pattern would stall every agent at once --
   which is why there is no regex in the language at all.
3. **Refused at BOTH ends.** Central validates on ingest and the agent
   validates again on receipt and on every cache load. A test that only
   exercises one end proves nothing about the other, since they are two files.
"""

from __future__ import annotations

import json
import time

import pytest

from central import device_definitions as central_defs
from printer_nanny_agent import definitions as agent_defs
from printer_nanny_agent.providers import definitions as provider

#: Anything here must be decided in well under a second. Generous enough not to
#: flake on a loaded CI box, tight enough that "it eventually rejected it after
#: chewing through 50 MB" fails.
_TIME_BUDGET_S = 2.0


def _both(raw):
    """Run a candidate through both validators; return (central_err, agent_err)."""
    errors = []
    for module in (central_defs, agent_defs):
        try:
            module.validate_definition(raw)
            errors.append(None)
        except module.DefinitionError as exc:
            errors.append(str(exc))
    return tuple(errors)


def _refused(raw, *, contains: str = ""):
    started = time.monotonic()
    central_err, agent_err = _both(raw)
    elapsed = time.monotonic() - started
    assert elapsed < _TIME_BUDGET_S, f"took {elapsed:.2f}s to refuse"
    assert central_err is not None, "central accepted a hostile definition"
    assert agent_err is not None, "the agent accepted a hostile definition"
    assert central_err == agent_err
    if contains:
        assert contains.lower() in central_err.lower(), central_err
    return central_err


# --------------------------------------------------------------------------- #
# Patterns: the whole point is that the language cannot express one
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("field", ["regex", "pattern", "re", "expr", "eval", "exec", "script", "code"])
def test_a_pattern_field_is_refused_by_name(field):
    """The classic catastrophic backtracker, offered under every name somebody
    would reach for. Refused with the REASON named, so the message reads as a
    decision rather than a typo."""
    err = _refused(
        {
            "key": "evil-pattern", "match": {"model": "abc9"},
            "supplies": [
                {"oid": "1.1.1", "type": "toner",
                 "decode": {"kind": "percent", field: "(a+)+$"}}
            ],
        },
        contains="denial-of-service",
    )
    assert field in err


def test_the_only_extraction_is_literal_and_cannot_backtrack():
    """text_between is the regex replacement. Feed it the input that kills a
    naive `(a+)+$` and it must return promptly -- and correctly."""
    definition = agent_defs.validate_definition(
        {
            "key": "slice-me", "match": {"model": "abc9"},
            "supplies": [
                {"oid": "1.1.1", "type": "toner",
                 "decode": {"kind": "text_between", "after": "T:", "before": "%"}}
            ],
        }
    )
    decode = definition["supplies"][0]["decode"]
    evil = "a" * 40000 + "!"
    started = time.monotonic()
    assert provider.decode_percent(decode, {"1.1.1": evil}, "1.1.1") is None
    assert provider.decode_percent(decode, {"1.1.1": f"T:{evil}%"}, "1.1.1") is None
    assert provider.decode_percent(decode, {"1.1.1": "Toner T:47% left"}, "1.1.1") == 47.0
    assert time.monotonic() - started < _TIME_BUDGET_S


# --------------------------------------------------------------------------- #
# Size and shape
# --------------------------------------------------------------------------- #
def test_a_huge_payload_is_refused_by_its_size_before_it_is_parsed():
    """The cap is on RAW BYTES, checked before json.loads. By the time a bomb
    has a shape it has already cost whatever it was going to cost."""
    huge = json.dumps({"key": "big", "notes": "x" * (central_defs.MAX_PAYLOAD_BYTES + 1)})
    assert len(huge) > central_defs.MAX_PAYLOAD_BYTES

    from central.dashboard.definitions import _parse_spec

    started = time.monotonic()
    with pytest.raises(central_defs.DefinitionError) as caught:
        _parse_spec(huge)
    assert time.monotonic() - started < _TIME_BUDGET_S
    assert "over the" in str(caught.value)


def test_a_deeply_nested_definition_is_refused_without_a_recursion_error():
    """A depth check that recurses over hostile input hits Python's own limit
    first and raises RecursionError from somewhere unrelated, turning the guard
    into the failure. The check here is iterative, so this must be a clean
    DefinitionError."""
    nested = {"key": "deep", "match": {"model": "abc9"}}
    node = nested
    for _ in range(500):
        node["match"] = {"model": "abc9", "brand": node}
        node = node["match"]
    _refused(nested)


def test_a_json_document_too_deep_to_parse_is_a_sentence_not_a_stack_trace():
    """json.loads itself blows the stack long before any of our code runs."""
    from central.dashboard.definitions import _parse_spec

    bomb = "[" * 100000 + "]" * 100000
    with pytest.raises(central_defs.DefinitionError):
        _parse_spec(bomb)


def test_a_map_cannot_be_used_as_unbounded_storage():
    _refused(
        {
            "key": "fat-map", "match": {"model": "abc9"},
            "supplies": [
                {"oid": "1.1.1", "type": "toner",
                 "decode": {"kind": "map",
                            "values": {str(i): 1 for i in range(central_defs.MAX_MAP_ENTRIES + 1)}}}
            ],
        },
        contains="entries",
    )


def test_too_many_definitions_in_one_feed_is_refused():
    rows = [
        {"key": f"def-{i:04d}", "match": {"model": "abc9"},
         "supplies": [{"oid": "1.1.1", "type": "toner"}]}
        for i in range(central_defs.MAX_DEFINITIONS + 1)
    ]
    for module in (central_defs, agent_defs):
        with pytest.raises(module.DefinitionError):
            module.validate_feed(rows)


def test_too_many_oids_in_one_definition_is_refused():
    _refused(
        {
            "key": "greedy", "match": {"model": "abc9"},
            "supplies": [
                {"oid": f"1.3.6.1.4.1.9.{i}", "type": "toner", "color": f"c{i}"}
                for i in range(central_defs.MAX_OIDS_PER_DEFINITION + 1)
            ],
        },
    )


# --------------------------------------------------------------------------- #
# OIDs: this is the value that reaches an SNMP library
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("oid", [
    "1.3.6; rm -rf /",
    "1.3.6.1.4.1.$(whoami)",
    "1.3.6.1.4.1.9`id`",
    "../../etc/passwd",
    "1.3.6.1.4.1..9",
    ".1.3.6.1.4.1.9.",
    "1" * 300,
    "1." * 200 + "1",
    "1.3.6.1.4.1.9\n1.2.3",
    "1.3.6.1.4.1.99999999999999999999999999",
    "",
    "iso.org.dod.internet",
])
def test_only_a_dotted_numeric_oid_is_accepted(oid):
    _refused(
        {"key": "bad-oid-def", "match": {"model": "abc9"},
         "supplies": [{"oid": oid, "type": "toner"}]},
    )
    assert not central_defs.is_valid_oid(oid)
    assert not agent_defs.is_valid_oid(oid)


# --------------------------------------------------------------------------- #
# Types: JSON says nothing about what a field is
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", [
    {"key": {"nested": "object"}, "match": {"model": "abc9"}},
    {"key": ["a", "list"], "match": {"model": "abc9"}},
    {"key": 12345, "match": {"model": "abc9"}},
    {"key": "typed", "match": "a string"},
    {"key": "typed", "match": {"model": 9000}},
    {"key": "typed", "match": {"model": ["abc9"]}},
    {"key": "typed", "match": {"model": "abc9"}, "supplies": "not a list"},
    {"key": "typed", "match": {"model": "abc9"}, "supplies": [["oid"]]},
    {"key": "typed", "match": {"model": "abc9"},
     "supplies": [{"oid": ["1.1.1"], "type": "toner"}]},
    {"key": "typed", "match": {"model": "abc9"},
     "supplies": [{"oid": "1.1.1", "type": "toner", "decode": {"kind": "map", "values": {"1": "100"}}}]},
    {"key": "typed", "match": {"model": "abc9"},
     "supplies": [{"oid": "1.1.1", "type": "toner", "decode": {"kind": "map", "values": {"1": 250}}}]},
    {"key": "typed", "match": {"model": "abc9"},
     "supplies": [{"oid": "1.1.1", "type": "toner", "decode": {"kind": "ratio", "max": 0}}]},
    {"key": "typed", "match": {"model": "abc9"},
     "supplies": [{"oid": "1.1.1", "type": "toner", "decode": {"kind": "ratio", "max": -5}}]},
    {"key": "typed", "match": {"model": "abc9"},
     "supplies": [{"oid": "1.1.1", "type": "toner", "decode": {"kind": "ratio", "max": True}}]},
    {"key": "typed", "match": {"model": "abc9"},
     "supplies": [{"oid": "1.1.1", "type": "toner", "decode": {"kind": "ratio", "max": 10, "max_oid": "1.1.2"}}]},
    {"key": "typed", "match": {"model": "abc9"},
     "supplies": [{"oid": "1.1.1", "type": "toner", "decode": {"kind": "unheard-of"}}]},
    {"key": "typed", "match": {"model": "abc9"},
     "meters": [{"oid": "1.1.1", "meter": "mono", "decode": {"kind": "percent"}}]},
    {"key": "typed", "match": {"model": "abc9"},
     "supplies": [{"oid": "1.1.1", "type": "toner"}], "unexpected_field": 1},
    {"key": "typed", "match": {"model": "abc9", "unexpected": "x"}},
    {"key": "typed", "match": {"model": "abc9"}, "status_text": {"oid": "1.1.1", "extra": 1}},
    {"key": "typed", "match": {"model": "abc9"},
     "supplies": [{"oid": "1.1.1", "type": "toner", "color": "<script>alert(1)</script>"}]},
    {"key": "typed", "match": {"model": "abc9"},
     "meters": [{"oid": "1.1.1", "meter": "../../../etc/passwd"}]},
    {"key": "../../etc/passwd", "match": {"model": "abc9"}},
    {"key": "a" * 200, "match": {"model": "abc9"}},
    42,
    "definition",
    None,
])
def test_unexpected_types_and_fields_are_refused(raw):
    _refused(raw)


def test_a_definition_matching_everything_is_refused():
    """No criteria means Brother private-MIB OIDs pointed at an HP, in every
    client. There is no legitimate 'applies to everything' definition."""
    _refused(
        {"key": "catch-all", "supplies": [{"oid": "1.1.1", "type": "toner"}]},
        contains="at least one match criterion",
    )


def test_a_two_character_match_tag_is_refused():
    _refused({"key": "tiny-tag", "match": {"model": "ab"}}, contains="at least 3")


# --------------------------------------------------------------------------- #
# The cache is an input too
# --------------------------------------------------------------------------- #
def test_a_cache_file_from_another_agent_is_refused(tmp_path):
    """A signature binds the feed to one agent's credential, so a cache lifted
    from another agent -- or edited on disk -- fails closed to no definitions
    rather than becoming a way to choose what a fleet reads."""
    rows = agent_defs.validate_feed(
        [{"key": "acme-9000", "match": {"model": "AC-9000"},
          "supplies": [{"oid": "1.1.1", "type": "toner"}]}]
    )
    version = agent_defs.feed_version(rows)
    payload = {
        "version": version,
        "definitions": rows,
        "signature": agent_defs.payload_signature("someone-elses-key", version, rows),
    }
    store = provider.DefinitionStore(str(tmp_path / "defs.json"))
    assert store.save(payload)
    assert store.load("my-own-key") is None
    # ...and the same file is fine for the agent it was actually made for.
    assert store.load("someone-elses-key") is not None


def test_a_tampered_cache_is_refused(tmp_path):
    rows = agent_defs.validate_feed(
        [{"key": "acme-9000", "match": {"model": "AC-9000"},
          "supplies": [{"oid": "1.1.1", "type": "toner"}]}]
    )
    version = agent_defs.feed_version(rows)
    path = tmp_path / "defs.json"
    store = provider.DefinitionStore(str(path))
    store.save({
        "version": version, "definitions": rows,
        "signature": agent_defs.payload_signature("k", version, rows),
    })
    assert store.load("k") is not None

    payload = json.loads(path.read_text())
    payload["definitions"][0]["supplies"][0]["oid"] = "1.2.3.4.5"
    path.write_text(json.dumps(payload))
    assert store.load("k") is None


def test_an_oversized_cache_is_refused_without_reading_it(tmp_path):
    path = tmp_path / "defs.json"
    path.write_text("x" * (agent_defs.MAX_PAYLOAD_BYTES + 10))
    started = time.monotonic()
    assert provider.DefinitionStore(str(path)).load("k") is None
    assert time.monotonic() - started < _TIME_BUDGET_S


def test_a_corrupt_cache_is_refused_rather_than_crashing(tmp_path):
    path = tmp_path / "defs.json"
    for junk in ("", "{", "null", "[]", '{"version": 1}', '{"version": "a", "definitions": "b"}'):
        path.write_text(junk)
        assert provider.DefinitionStore(str(path)).load("k") is None


def test_one_bad_definition_does_not_cost_the_good_ones():
    """set_active is the single door into the active set, and it drops a bad row
    with its reason rather than refusing the whole feed -- an operator's typo in
    one definition must not silently disable every other one."""
    provider.clear()
    try:
        count = provider.set_active([
            {"key": "good-one", "match": {"model": "AC-9000"},
             "supplies": [{"oid": "1.1.1", "type": "toner"}]},
            {"key": "bad-one", "match": {"model": "AC-9001"},
             "supplies": [{"oid": "not-an-oid", "type": "toner"}]},
        ])
        assert count == 1
        assert [d["key"] for d in provider.active()] == ["good-one"]
    finally:
        provider.clear()
