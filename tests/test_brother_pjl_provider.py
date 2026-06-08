"""Brother PJL-over-9100 provider: the channel BRAdmin Professional uses.

PJL queries return plain-text percentages directly from the printer's
firmware consumption model -- much more reliable than EWS HTML scraping
(which varies per model) and more precise than SNMP buckets.
"""

from __future__ import annotations

import pytest

from printer_nanny_agent.providers.brother_pjl import (
    BrotherPjlProvider,
    _parse_pjl_drum,
    _parse_pjl_response,
)
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend


# ---- Parser ----

def test_parse_modern_per_color_keys():
    """Format 1 (HL-L2370DW class)."""
    text = """
    @PJL INFO MAINTENANCE
    TONER_BLACK=23%
    TONER_CYAN=70%
    TONER_MAGENTA=60%
    TONER_YELLOW=50%
    DRUM_LIFE_REMAINING=72%
    """
    assert _parse_pjl_response(text) == {
        "black": 23, "cyan": 70, "magenta": 60, "yellow": 50,
    }
    assert _parse_pjl_drum(text) == 72


def test_parse_reversed_keyword_order():
    """BLACK_TONER= works too -- some Brother firmware revs put color first."""
    text = """
    BLACK_TONER=18
    CYAN_TONER=85
    """
    assert _parse_pjl_response(text) == {"black": 18, "cyan": 85}


def test_parse_compact_form_brscript():
    """Format 2: TONER=K=85%,C=70%,M=60%,Y=50% -- BR-Script3 / older PJL."""
    text = """
    @PJL INFO SUPPLIES
    TONER=K=85%,C=70%,M=60%,Y=50%
    DRUM=K=80%
    """
    assert _parse_pjl_response(text) == {
        "black": 85, "cyan": 70, "magenta": 60, "yellow": 50,
    }
    assert _parse_pjl_drum(text) == 80


def test_parse_life_remaining_phrasing():
    """Some Brother lasers spell it out: 'TONER LIFE REMAINING=K=NN'."""
    text = "TONER LIFE REMAINING=K=15"
    assert _parse_pjl_response(text) == {"black": 15}


def test_parse_rejects_out_of_range():
    text = "TONER_BLACK=999%\nTONER_CYAN=72%"
    pcts = _parse_pjl_response(text)
    assert "black" not in pcts
    assert pcts["cyan"] == 72


def test_parse_empty_returns_empty():
    assert _parse_pjl_response("") == {}
    assert _parse_pjl_response("    ") == {}
    assert _parse_pjl_drum("") is None


def test_parse_no_supplies_in_response():
    text = "@PJL INFO STATUS\nPRINTING\nREADY\n"
    assert _parse_pjl_response(text) == {}
    assert _parse_pjl_drum(text) is None


# ---- augment() integration ----

async def test_augment_overrides_supplies_with_pjl_values(monkeypatch):
    """A successful PJL query overrides whatever earlier providers seeded."""
    from printer_nanny_agent.providers import brother_pjl as mod

    async def fake_query(ip: str):
        return "TONER_BLACK=23%\nDRUM_LIFE_REMAINING=72%"

    monkeypatch.setattr(mod, "_query_pjl", fake_query)

    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": 15.0,
             "status_note": "low", "_brother_estimated": True},
            {"type": "drum", "color": "black", "level_pct": None,
             "status_note": None},
        ],
    }
    out = await BrotherPjlProvider().augment(
        FakeSnmpBackend(), "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1"
    )
    black = next(s for s in out["supplies"] if s["type"] == "toner")
    drum = next(s for s in out["supplies"] if s["type"] == "drum")
    assert black["level_pct"] == 23.0
    assert black["status_note"] is None
    assert black["_pjl_sourced"] is True
    assert black["_brother_estimated"] is False
    assert drum["level_pct"] == 72.0
    assert drum["_pjl_sourced"] is True
    assert out["_supply_precision"] == "brother_pjl"


async def test_augment_failure_leaves_reading_alone(monkeypatch):
    """TCP/9100 refused / port blocked -- earlier provider data stays intact."""
    from printer_nanny_agent.providers import brother_pjl as mod

    async def fake_query(ip: str):
        return None

    monkeypatch.setattr(mod, "_query_pjl", fake_query)
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": 15.0,
             "status_note": "low", "_brother_estimated": True},
        ],
    }
    out = await BrotherPjlProvider().augment(
        FakeSnmpBackend(), "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1"
    )
    assert out["supplies"][0]["level_pct"] == 15.0
    assert "_pjl_sourced" not in out["supplies"][0]


async def test_augment_swallows_unexpected_exceptions(monkeypatch):
    """A bug in the parser / socket code MUST NOT crash the agent's poll loop."""
    from printer_nanny_agent.providers import brother_pjl as mod

    async def boom(ip: str):
        raise RuntimeError("simulated PJL crash")

    monkeypatch.setattr(mod, "_query_pjl", boom)
    reading = {"supplies": [
        {"type": "toner", "color": "black", "level_pct": 15.0,
         "_brother_estimated": True},
    ]}
    out = await BrotherPjlProvider().augment(
        FakeSnmpBackend(), "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1"
    )
    assert out["supplies"][0]["level_pct"] == 15.0


@pytest.mark.parametrize("sys_oid,expected", [
    ("SNMPv2-SMI::enterprises.2435.2.3.9.1", True),
    ("1.3.6.1.4.1.2435.2.3.9.1", True),
    ("SNMPv2-SMI::enterprises.11.2.3.9.1", False),  # HP
    (None, False),
])
def test_detect_only_brother(sys_oid, expected):
    assert BrotherPjlProvider().detect({}, sys_oid) is expected


# ---- EWS coordination ----

async def test_ews_does_not_overwrite_pjl_values(monkeypatch):
    """When a supply has _pjl_sourced=True, the EWS provider must leave it alone.
    PJL is BRAdmin's protocol -- per-model gauge math from EWS is the fallback,
    never the override."""
    from printer_nanny_agent.providers import brother_ews as mod

    async def fake_html(ip: str):
        # EWS gauge would say 100% (full bar) but PJL already said 23%.
        return """
        <table id="inkLevel"><tr>
        <td><img class="tonerremain" alt="Black" height="11" /></td>
        </tr></table>"""

    monkeypatch.setattr(mod, "_fetch_ews_html", fake_html)
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": 23.0,
             "status_note": None, "_pjl_sourced": True,
             "_brother_estimated": False},
        ],
    }
    out = await mod.BrotherEwsProvider().augment(
        FakeSnmpBackend(), "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1"
    )
    assert out["supplies"][0]["level_pct"] == 23.0
    # No _ews_sourced added because we deferred to PJL.
    assert out["supplies"][0].get("_ews_sourced") is not True
