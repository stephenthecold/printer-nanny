"""Brother EWS scraper: real toner percentages from /general/status.html.

The page format varies by firmware; we match three layouts that cover ~all
Brother color lasers from the 2014+ generation. Fail-safe: if scraping fails
for any reason, the SNMP bucket data from BrotherProvider stays put -- the
operator just sees "Low / OK" rather than the real percentage.
"""

from __future__ import annotations

import pytest

from printer_nanny_agent.providers.brother_ews import (
    BrotherEwsProvider,
    _parse_toner_percentages,
)
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend


def test_parse_javascript_indexed_array():
    """Pattern A -- the most common layout on 2018+ color lasers."""
    html = """
    <html><head><script>
    function setToner() {
        var TonerInfo = new Array(4);
        TonerInfo[0] = "85";
        TonerInfo[1] = "70";
        TonerInfo[2] = "60";
        TonerInfo[3] = "50";
    }
    </script></head></html>
    """
    pcts = _parse_toner_percentages(html)
    assert pcts == {"black": 85, "cyan": 70, "magenta": 60, "yellow": 50}


def test_parse_javascript_new_array_literal():
    html = """<script>var Toner = new Array("85","75","65","45");</script>"""
    pcts = _parse_toner_percentages(html)
    assert pcts == {"black": 85, "cyan": 75, "magenta": 65, "yellow": 45}


def test_parse_html_table():
    """Pattern B -- some older Brother HLs render gauges as a plain table."""
    html = """
    <table id="toner">
      <tr><td>Black</td><td>85%</td></tr>
      <tr><td>Cyan</td><td>75%</td></tr>
      <tr><td>Magenta</td><td>65%</td></tr>
      <tr><td>Yellow</td><td>45%</td></tr>
    </table>
    """
    pcts = _parse_toner_percentages(html)
    assert pcts == {"black": 85, "cyan": 75, "magenta": 65, "yellow": 45}


def test_parse_html_table_with_intermediate_cells():
    """Some firmwares stick an icon cell between color name and percent."""
    html = """
    <table>
      <tr><td>Black</td><td><img src="/icons/k.gif"></td><td>85%</td></tr>
      <tr><td>Cyan</td><td><img src="/icons/c.gif"></td><td>75%</td></tr>
    </table>
    """
    pcts = _parse_toner_percentages(html)
    assert pcts["black"] == 85
    assert pcts["cyan"] == 75


def test_parse_img_filenames():
    """Pattern C -- gauge images named e.g. Toner_C_85.gif."""
    html = """
    <img src="/sw/img/Toner_K_85.gif" alt="black 85">
    <img src="/sw/img/Toner_C_75.gif" alt="cyan 75">
    <img src="/sw/img/Toner_M_65.gif" alt="magenta 65">
    <img src="/sw/img/Toner_Y_45.gif" alt="yellow 45">
    """
    pcts = _parse_toner_percentages(html)
    assert pcts == {"black": 85, "cyan": 75, "magenta": 65, "yellow": 45}


def test_parse_returns_empty_for_unknown_layout():
    """A completely-different page must NOT produce spurious percentages."""
    html = "<html><body>Welcome to the printer admin page.</body></html>"
    assert _parse_toner_percentages(html) == {}


def test_parse_tonerremain_gauge_height_attribute_mfc_l8900cdw():
    """Pattern D -- real L8900CDW HTML uses gauge height as the value, no
    numeric percentage displayed anywhere. Pulled directly from Stephen's
    /general/status.html dump."""
    html = """
    <div id="ink_level"><table id="inkLevel" summary="ink level">
    <tr><th><img src="../common/images/low.gif" alt="Low" /></th><th></th><th></th><th></th></tr>
    <tr>
      <td><img src="../common/images/black.gif"   alt="Black"   class="tonerremain" height="5"  /></td>
      <td><img src="../common/images/cyan.gif"    alt="Cyan"    class="tonerremain" height="11" /></td>
      <td><img src="../common/images/magenta.gif" alt="Magenta" class="tonerremain" height="11" /></td>
      <td><img src="../common/images/yellow.gif"  alt="Yellow"  class="tonerremain" height="11" /></td>
    </tr>
    <tr><th>BK</th><th>C</th><th>M</th><th>Y</th></tr>
    </table></div>
    """
    pcts = _parse_toner_percentages(html)
    # Max gauge height on this model family is 11px ("full"). Black=5 -> ~45%
    # (which matches the printer simultaneously reporting "Toner Low (BK)" --
    # Brother's low warning fires at this level on the L8900). C/M/Y = full.
    assert pcts == {"black": 45, "cyan": 100, "magenta": 100, "yellow": 100}


def test_parse_tonerremain_handles_reversed_attribute_order():
    """Brother firmwares vary attribute order; class= can come before alt= or after."""
    html = """
    <img height="3" class="tonerremain" alt="Black" src="/x.gif" />
    <img class="tonerremain" height="10" alt="Cyan" src="/y.gif" />
    """
    pcts = _parse_toner_percentages(html)
    assert pcts["black"] == 27  # 3/11 -> 27%
    assert pcts["cyan"] == 91   # 10/11 -> 91%


def test_parse_tonerremain_clamps_to_100():
    """If a firmware reports a height greater than the assumed max (newer
    revs with a 30px gauge?) we clamp rather than emit >100%."""
    html = """<img class="tonerremain" alt="Black" height="30" />"""
    pcts = _parse_toner_percentages(html)
    assert pcts == {"black": 100}


def test_parse_returns_empty_for_blank_input():
    assert _parse_toner_percentages("") == {}
    assert _parse_toner_percentages("   ") == {}


def test_parse_rejects_out_of_range_percentages():
    """A buggy firmware that reports 999% must not poison the reading."""
    html = """<script>TonerInfo[0] = "999"; TonerInfo[1] = "70";</script>"""
    # Index 0 invalid -> dropped. Index 1 (cyan) kept.
    pcts = _parse_toner_percentages(html)
    assert pcts == {"cyan": 70}


async def test_augment_swallows_http_failures_silently(monkeypatch):
    """Unreachable EWS / connection refused / timeout: leave SNMP data alone."""
    from printer_nanny_agent.providers import brother_ews as mod

    async def boom(ip: str):
        raise RuntimeError("simulated network error")

    monkeypatch.setattr(mod, "_fetch_ews_html", boom)
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": 15.0,
             "status_note": "low", "_brother_estimated": True},
        ],
    }
    out = await BrotherEwsProvider().augment(
        FakeSnmpBackend(), "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1"
    )
    # SNMP bucket data preserved.
    assert out["supplies"][0]["level_pct"] == 15.0
    assert out["supplies"][0]["status_note"] == "low"


async def test_augment_overrides_bucket_estimate_with_real_percentages(monkeypatch):
    from printer_nanny_agent.providers import brother_ews as mod

    async def html(ip: str):
        return """<script>
        TonerInfo[0]="87";TonerInfo[1]="73";TonerInfo[2]="65";TonerInfo[3]="42";
        </script>"""

    monkeypatch.setattr(mod, "_fetch_ews_html", html)
    reading = {
        "supplies": [
            # Coming in as bucket estimates from BrotherProvider.
            {"type": "toner", "color": "black", "level_pct": 15.0,
             "status_note": "low", "_brother_estimated": True},
            {"type": "toner", "color": "cyan", "level_pct": None,
             "status_note": "some remaining"},
            {"type": "toner", "color": "magenta", "level_pct": None,
             "status_note": "some remaining"},
            {"type": "toner", "color": "yellow", "level_pct": None,
             "status_note": "some remaining"},
            # Drum has a real number from standard MIB -- must NOT be touched.
            {"type": "drum", "color": "black", "level_pct": 95.3, "status_note": None},
        ],
    }
    out = await BrotherEwsProvider().augment(
        FakeSnmpBackend(), "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1"
    )
    by_key = {(s["type"], s["color"]): s for s in out["supplies"]}
    assert by_key[("toner", "black")]["level_pct"] == 87.0
    assert by_key[("toner", "cyan")]["level_pct"] == 73.0
    assert by_key[("toner", "magenta")]["level_pct"] == 65.0
    assert by_key[("toner", "yellow")]["level_pct"] == 42.0
    # status_note cleared because we have a real number now.
    assert by_key[("toner", "black")]["status_note"] is None
    assert by_key[("toner", "black")]["_brother_estimated"] is False
    assert by_key[("toner", "black")]["_ews_sourced"] is True
    # Drum untouched.
    assert by_key[("drum", "black")]["level_pct"] == 95.3
    # Precision marker promoted.
    assert out["_supply_precision"] == "brother_ews"


async def test_augment_leaves_already_accurate_snmp_values_alone(monkeypatch):
    """Some Brother HLs already report real %. Don't override if SNMP and EWS
    agree within 5% -- avoids needless churn in the readings history."""
    from printer_nanny_agent.providers import brother_ews as mod

    async def html(ip: str):
        return """<script>TonerInfo[0]="87";</script>"""

    monkeypatch.setattr(mod, "_fetch_ews_html", html)
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": 85.0,
             "status_note": None, "_brother_estimated": False},
        ],
    }
    out = await BrotherEwsProvider().augment(
        FakeSnmpBackend(), "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1"
    )
    # 85 vs 87 within 5% tolerance -> keep the SNMP value.
    assert out["supplies"][0]["level_pct"] == 85.0


async def test_augment_swallows_parser_failures(monkeypatch):
    """A page that triggers a regex catastrophic-backtrack would re-raise; we
    must still ship the SNMP reading."""
    from printer_nanny_agent.providers import brother_ews as mod

    async def html(ip: str):
        return "<page>"

    def explode(_html: str):
        raise RuntimeError("simulated parse failure")

    monkeypatch.setattr(mod, "_fetch_ews_html", html)
    monkeypatch.setattr(mod, "_parse_toner_percentages", explode)
    reading = {"supplies": [{"type": "toner", "color": "black", "level_pct": 15.0}]}
    out = await BrotherEwsProvider().augment(
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
    assert BrotherEwsProvider().detect({}, sys_oid) is expected
