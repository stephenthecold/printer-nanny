"""Consolidated Brother provider: alert-text bucket hints + history events.

These tests exercise the umbrella's status pass and bucket logic. The
maintenance blob is absent in every fixture (FakeSnmpBackend returns None
for its OIDs), so the umbrella exercises the alert/bucket path; the PJL and
EWS fallback seams are stubbed out below so tests never touch the network.
"""

from __future__ import annotations

import pytest

from printer_nanny_agent.providers import brother as brother_mod
from printer_nanny_agent.providers.brother import (
    BrotherProvider,
    OID_ACTIVE_ALERT_TEXT,
    OID_ALERT_HISTORY_DESCR,
    OID_ALERT_HISTORY_INDEX,
    OID_ALERT_HISTORY_PAGES,
    _parse_alert,
)
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend


@pytest.fixture(autouse=True)
def _no_network_fallbacks(monkeypatch):
    """Stub the PJL (TCP/9100) and EWS (HTTP) fallback seams.

    The bucket fixtures leave toner gaps, so the umbrella would otherwise
    attempt real connections to the fixture IPs from CI."""
    async def passthrough(backend, ip, params, reading, sys_object_id):
        return reading

    monkeypatch.setattr(brother_mod, "_pjl_step", passthrough)
    monkeypatch.setattr(brother_mod, "_ews_step", passthrough)


def test_parse_alert_matches_brother_formats():
    # Color-laser format: severity + parenthesized or trailing color code.
    assert _parse_alert("Toner Low (BK)")     == ("low",   "black")
    assert _parse_alert("Toner Empty (C)")    == ("empty", "cyan")
    assert _parse_alert("Replace Toner (M)")  == ("empty", "magenta")
    assert _parse_alert("Toner Near End (Y)") == ("low",   "yellow")
    # Mono-laser format: severity word alone, no color code in the text.
    # The augment step defaults the color to black when there's only a black
    # toner on the device -- the parser just reports "no color found".
    assert _parse_alert("No Toner")      == ("empty", None)
    assert _parse_alert("Replace Toner") == ("empty", None)
    assert _parse_alert("Toner Low")     == ("low",   None)
    # Non-toner alerts shouldn't trigger toner status changes.
    assert _parse_alert("Drum Low")      == ("low",   None)  # parser doesn't filter; augment does
    assert _parse_alert(None)            == (None,   None)
    assert _parse_alert("")              == (None,   None)


def test_detects_brother_via_enterprise_oid():
    p = BrotherProvider()
    assert p.detect({}, "SNMPv2-SMI::enterprises.2435.2.3.9.1") is True
    assert p.detect({}, "1.3.6.1.4.1.2435.2.3.9.1") is True
    assert p.detect({}, "SNMPv2-SMI::enterprises.11.2.3.9.1") is False  # HP
    assert p.detect({}, None) is False


def _backend_with_brother_alerts(alert_text: str | None = None,
                                 history: list[tuple[str, int]] | None = None) -> FakeSnmpBackend:
    """Build a FakeSnmpBackend that responds to the OIDs the Brother provider GETs/walks."""
    scalars: dict = {}
    walks: dict = {}
    if alert_text is not None:
        scalars[OID_ACTIVE_ALERT_TEXT + ".0"] = alert_text
    if history:
        idx_rows = {f"{OID_ALERT_HISTORY_INDEX}.{n+1}": str(n + 1) for n in range(len(history))}
        desc_rows = {f"{OID_ALERT_HISTORY_DESCR}.{n+1}": desc for n, (desc, _p) in enumerate(history)}
        page_rows = {f"{OID_ALERT_HISTORY_PAGES}.{n+1}": str(p) for n, (_d, p) in enumerate(history)}
        walks[OID_ALERT_HISTORY_INDEX] = idx_rows
        walks[OID_ALERT_HISTORY_DESCR] = desc_rows
        walks[OID_ALERT_HISTORY_PAGES] = page_rows
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": scalars, "walks": walks}
    return backend


async def test_augment_upgrades_toner_status_when_brother_reports_low():
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": None,
             "status_note": "some remaining", "description": "Black Toner Cartridge"},
            {"type": "toner", "color": "cyan", "level_pct": None,
             "status_note": "some remaining", "description": "Cyan Toner Cartridge"},
            # A toner that has real data (e.g. some Brother HLs do report %) -- DON'T overwrite it.
            {"type": "toner", "color": "yellow", "level_pct": 73.0,
             "status_note": None, "description": "Yellow Toner Cartridge"},
        ],
        "events": [],
    }
    backend = _backend_with_brother_alerts(alert_text="Toner Low (BK)")
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1"
    )
    black = next(s for s in out["supplies"] if s["color"] == "black")
    cyan = next(s for s in out["supplies"] if s["color"] == "cyan")
    yellow = next(s for s in out["supplies"] if s["color"] == "yellow")
    assert black["status_note"] == "low"
    assert black["level_pct"] == 15.0
    assert black["_brother_estimated"] is True
    # Cyan untouched (alert only named BK).
    assert cyan["status_note"] == "some remaining"
    assert cyan["level_pct"] is None
    # Yellow had a real percent - provider must NOT overwrite real data.
    assert yellow["level_pct"] == 73.0
    assert "_brother_estimated" not in yellow
    assert out["_supply_precision"] == "brother_buckets"


async def test_augment_surfaces_alert_history_as_events():
    reading = {"supplies": [], "events": []}
    history = [
        ("Document Jam", 76842),
        ("Toner Low (BK)", 76786),
        ("Replace Drum", 75585),
    ]
    backend = _backend_with_brother_alerts(alert_text=None, history=history)
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1"
    )
    messages = [e["message"] for e in out["events"]]
    assert "Document Jam (page 76,842)" in messages
    assert "Toner Low (BK) (page 76,786)" in messages
    assert "Replace Drum (page 75,585)" in messages
    assert all(e["severity"] == "info" for e in out["events"])


async def test_augment_handles_mono_no_toner_alert():
    """Mono lasers like the HL-L2370DW say 'No Toner' with no color code.
    The augment step defaults missing color to the printer's single black toner.
    Regression: real-world Brother showed 'No Toner' active alert but the
    provider left status_note='some remaining' because the old regex required
    a color in parens."""
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": None,
             "status_note": "some remaining", "description": "Black Toner Cartridge"},
        ],
        "events": [],
    }
    backend = _backend_with_brother_alerts(alert_text="No Toner")
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1",
    )
    black = next(s for s in out["supplies"] if s["color"] == "black")
    assert black["status_note"] == "empty"
    assert black["level_pct"] == 0.0
    assert black["_brother_estimated"] is True


async def test_augment_handles_mono_replace_toner_alert():
    """'Replace Toner' on mono lasers maps to empty/black, no color code needed."""
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": None,
             "status_note": "some remaining", "description": "Black Toner Cartridge"},
        ],
        "events": [],
    }
    backend = _backend_with_brother_alerts(alert_text="Replace Toner")
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1",
    )
    black = next(s for s in out["supplies"] if s["color"] == "black")
    assert black["status_note"] == "empty"


async def test_augment_handles_mono_toner_low_alert():
    """'Toner Low' on a mono printer (no color in parens) -> low/black."""
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": None,
             "status_note": "some remaining", "description": "Black Toner Cartridge"},
        ],
        "events": [],
    }
    backend = _backend_with_brother_alerts(alert_text="Toner Low")
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1",
    )
    black = next(s for s in out["supplies"] if s["color"] == "black")
    assert black["status_note"] == "low"
    assert black["level_pct"] == 15.0


async def test_augment_does_not_use_history_as_fallback_when_live_is_idle():
    """Real-world HL-L2460DW: the alert history carries old 'Toner Low @page 691'
    and stale 'No Toner @page 0' entries even though the actual cartridge gauge
    (EWS) reads ~80% full. Earlier revisions used history as a fallback when
    the live active-alert OID returned 'Sleep' -- that produced false 'empty'
    and false 'low' badges. The history is unreliable: cartridges get replaced,
    Toner-Low warnings stay in history for a thousand pages, and factory
    placeholder rows hold 'No Toner @page 0' from day one.

    Current behaviour: live alert is the ONLY source. If it's idle, no
    supply update fires -- supply preserved at 'some remaining'. The
    breadcrumb explains it.
    """
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": None,
             "status_note": "some remaining", "description": "Black Toner Cartridge"},
        ],
        "events": [],
    }
    history = [
        ("Toner Low", 691),
        ("Cannot Print 3A", 1077),
        ("No Toner", 0),
        ("No Toner", 0),
    ]
    backend = _backend_with_brother_alerts(alert_text="Sleep", history=history)
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1",
    )
    black = next(s for s in out["supplies"] if s["color"] == "black")
    # No false-positive fired -- supply preserved as the standard MIB reported.
    assert black["status_note"] == "some remaining"
    assert black["level_pct"] is None
    # Diagnostic breadcrumb explains: alert=Sleep, parsed=none, source=none
    assert out["_brother_active_alert"] == "Sleep"
    assert out["_brother_parsed_severity"] == "none"
    assert out["_brother_source"] == "none"
    # But the history still feeds the events list (Error history card).
    messages = [e["message"] for e in out["events"]]
    assert any("Toner Low" in m for m in messages)


async def test_augment_records_diagnostics_even_when_live_alert_has_no_severity():
    """When the live alert exists but doesn't carry a supply state (e.g. a
    paper jam) and history has nothing supply-related either, the breadcrumb
    fields must still be set so the dashboard can tell the operator what
    happened: 'alert=Jam Inside, source=live, parsed=none'."""
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": None,
             "status_note": "some remaining"},
        ],
        "events": [],
    }
    backend = _backend_with_brother_alerts(
        alert_text="Jam Inside",
        history=[("Paper Tray Open", 100), ("Jam Inside", 200)],
    )
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1",
    )
    # No supply state matched: toner stays as-is.
    black = next(s for s in out["supplies"] if s["color"] == "black")
    assert black["status_note"] == "some remaining"
    assert black["level_pct"] is None
    # But the diagnostic breadcrumb explains why.
    assert out["_brother_active_alert"] == "Jam Inside"
    assert out["_brother_parsed_severity"] == "none"
    assert out["_brother_source"] == "none"


async def test_augment_swallows_snmp_errors():
    """A printer that exposes a partial / no Brother MIB must not crash the poll."""
    reading = {"supplies": [], "events": []}
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1"
    )
    # Even with no Brother data, the supply_precision tag is set so the UI
    # can still render the "buckets only" note.
    assert out["_supply_precision"] == "brother_buckets"
