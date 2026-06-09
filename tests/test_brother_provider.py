"""Brother provider enriches toner supplies and surfaces alert history.

The MFC-L8900CDW class of Brother lasers does not have a continuous toner
sensor; standard Printer-MIB reports level=-3 for every toner. Brother's
private MIB exposes 'Toner Low (BK)' as a plain text scalar -- we use that
to upgrade the toner's status_note from generic "some remaining" to "low".
"""

from __future__ import annotations

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


async def test_augment_falls_back_to_history_when_live_alert_is_idle():
    """Live HL-L2370DW symptom: when the printer is sleeping at poll time,
    the active-alert OID returns 'Sleep' (or '' / 'Ready' / similar) so the
    parser finds no severity. The history table still carries the real
    'No Toner' entry -- use it. Regression: provider trace shows 'no changes'
    despite the user staring at an empty cartridge."""
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": None,
             "status_note": "some remaining", "description": "Black Toner Cartridge"},
        ],
        "events": [],
    }
    # Live active alert is "Sleep" (idle state); history has the real signal.
    history = [
        ("Toner Low", 691),
        ("Cannot Print 3A", 1077),
        ("Jam Inside", 1060),
        ("No Toner", 0),  # most recent supply alert
    ]
    backend = _backend_with_brother_alerts(alert_text="Sleep", history=history)
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.2435.2.3.9.1",
    )
    black = next(s for s in out["supplies"] if s["color"] == "black")
    assert black["status_note"] == "empty"  # picked up via history fallback
    assert black["level_pct"] == 0.0
    # Diagnostic breadcrumbs surfaced so the provider trace can explain it.
    assert out["_brother_alert_source"] == "history"
    assert "No Toner" in out["_brother_active_alert"]


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
    assert out["_brother_alert_source"] == "live"
    assert out["_brother_parsed_severity"] == "none"


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
