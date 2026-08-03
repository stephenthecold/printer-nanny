"""Guard: the agent's vendored definition validator must BE central's.

The agent is a standalone package and cannot import ``central``, so the
validator exists twice -- the same arrangement ``snmp_parse`` uses. Two copies
of a security boundary is a drift hazard, and drift here is specifically bad in
both directions: a definition central accepts and the agent refuses is a
feature that silently does nothing, and a definition central refuses but the
agent would accept is a validator that only exists on paper.

So this asserts both halves:

* the SOURCE below the module docstring is identical, which catches a fix
  applied to one copy, and
* the BEHAVIOUR agrees on a corpus, which is what actually matters and which
  keeps passing if the files are ever legitimately reorganised.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from central import device_definitions as central_defs
from printer_nanny_agent import definitions as agent_defs

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CENTRAL = _ROOT / "central" / "device_definitions.py"
_AGENT = _ROOT / "agent" / "printer_nanny_agent" / "definitions.py"


def _body_source(path: pathlib.Path) -> str:
    """Everything after the module docstring, which is the part that must match.

    The docstrings differ deliberately: the agent's carries a header saying it
    is vendored and why. Nothing below it may.
    """
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    first = tree.body[0]
    assert isinstance(first, ast.Expr), f"{path} does not start with a docstring"
    return "\n".join(text.splitlines()[first.end_lineno:]).strip()


def test_validator_source_is_identical():
    assert _body_source(_CENTRAL) == _body_source(_AGENT)


def test_vocabularies_match_the_database_enum():
    """SUPPLY_TYPES is duplicated as a literal because the agent cannot import
    central's models. If SupplyType ever gains a member, a definition naming it
    would be refused by a validator nobody thought to update."""
    from central import models as m

    assert tuple(t.value for t in m.SupplyType) == central_defs.SUPPLY_TYPES
    assert agent_defs.SUPPLY_TYPES == central_defs.SUPPLY_TYPES


def test_bounds_match():
    for name in (
        "MAX_PAYLOAD_BYTES", "MAX_DEFINITIONS", "MAX_JSON_DEPTH",
        "MAX_OIDS_PER_DEFINITION", "MAX_SUPPLIES", "MAX_METERS",
        "MAX_MAP_ENTRIES", "MAX_TEXT", "MAX_OID_LEN", "MAX_OID_ARCS",
        "MIN_MATCH_TAG", "DECODE_KINDS", "CORE_METERS",
    ):
        assert getattr(agent_defs, name) == getattr(central_defs, name), name


def test_min_match_tag_matches_the_driver_package_rule():
    """Same number, same reason: a 1-2 character substring claims the fleet.
    Two different minimums would make one screen's advice wrong on the other."""
    from central import services

    assert central_defs.MIN_MATCH_TAG == services.MIN_DRIVER_MODEL_TAG


_CORPUS = [
    # Good
    {"key": "acme-9000", "match": {"model": "AC-9000"},
     "supplies": [{"oid": "1.3.6.1.4.1.1.1", "type": "toner", "color": "black"}]},
    {"key": "brand-only", "match": {"brand": "acme"},
     "meters": [{"oid": "1.3.6.1.4.1.1.2", "meter": "mono"}]},
    {"key": "ent-only", "match": {"enterprise": "2435"},
     "status_text": {"oid": "1.3.6.1.4.1.2435.1"}},
    {"key": "ratio-def", "match": {"model": "xyz9"},
     "supplies": [{"oid": "1.1.1", "type": "drum",
                   "decode": {"kind": "ratio", "max": 30000}}]},
    {"key": "mapped", "match": {"model": "abc1"},
     "supplies": [{"oid": "1.1.1", "type": "toner",
                   "decode": {"kind": "map", "values": {"1": 100, "2": 0}}}]},
    {"key": "sliced", "match": {"model": "abc1"},
     "supplies": [{"oid": "1.1.1", "type": "toner",
                   "decode": {"kind": "text_between", "after": "T:", "before": "%"}}]},
    # Bad, in a variety of ways
    {"key": "no-match-criteria", "supplies": []},
    {"key": "ab", "match": {"model": "abc"}},
    {"key": "bad-oid", "match": {"model": "abc"},
     "supplies": [{"oid": "1.3.6; rm -rf /", "type": "toner"}]},
    {"key": "bad-type", "match": {"model": "abc"},
     "supplies": [{"oid": "1.1.1", "type": "plutonium"}]},
    {"key": "regexy", "match": {"model": "abc"},
     "supplies": [{"oid": "1.1.1", "type": "toner",
                   "decode": {"kind": "percent", "regex": "(a+)+$"}}]},
    {"key": "short-tag", "match": {"model": "ab"}},
    {"key": "reads-nothing", "match": {"model": "abc"}},
    "not a dict",
    None,
    [],
]


@pytest.mark.parametrize("raw", _CORPUS, ids=range(len(_CORPUS)))
def test_validate_definition_parity(raw):
    """Same verdict, and where both accept, the same normalised output."""
    try:
        expected = central_defs.validate_definition(raw)
        central_error = None
    except central_defs.DefinitionError as exc:
        expected, central_error = None, str(exc)

    try:
        got = agent_defs.validate_definition(raw)
        agent_error = None
    except agent_defs.DefinitionError as exc:
        got, agent_error = None, str(exc)

    assert (central_error is None) == (agent_error is None), (central_error, agent_error)
    if central_error is None:
        assert got == expected
    else:
        assert agent_error == central_error


def test_signature_parity():
    """Both ends must compute the same HMAC or the agent refuses every feed."""
    rows = central_defs.validate_feed(
        [{"key": "acme-9000", "match": {"model": "AC-9000"},
          "supplies": [{"oid": "1.1.1", "type": "toner"}]}]
    )
    version = central_defs.feed_version(rows)
    assert version == agent_defs.feed_version(rows)
    sig = central_defs.payload_signature("deadbeef", version, rows)
    assert sig == agent_defs.payload_signature("deadbeef", version, rows)
    assert agent_defs.signature_ok("deadbeef", version, rows, sig)
    assert not agent_defs.signature_ok("cafe", version, rows, sig)
