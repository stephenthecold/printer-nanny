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
    _decode_hex_description,
    _harden_descriptions,
    _parse_alert,
)
from printer_nanny_agent.providers.brother_maintenance import OID_MAINTENANCE
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend

SYS_OID = "SNMPv2-SMI::enterprises.2435.2.3.9.1"


def _mrec(rec_id: str, value: int) -> str:
    """One 14-hex-char maintenance record: ID + 2 reserved bytes + 4-byte value."""
    return f"{rec_id}0104{value:08x}"


def _mblob(*records: str) -> str:
    """A pysnmp-rendered binary octet string: 0x-prefixed concatenated records."""
    return "0x" + "".join(records)


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
    # Non-toner alerts must not trigger toner status changes. The parser is
    # what filters -- augment cannot, because it defaults a missing color to
    # black, so any severity reaching it fabricates a level on the black toner.
    assert _parse_alert("Drum Low")      == (None,   None)
    assert _parse_alert(None)            == (None,   None)
    assert _parse_alert("")              == (None,   None)


# --------------------------------------------------------------------------- #
# Alert-text parsing is hostile input: the string comes off the device, and a
# match makes the provider SYNTHESISE a supply percentage (15% low / 0% empty)
# that flows into low-supply alerts, FreeScout tickets and the reorder
# forecast. The parser must fail closed.
#
# Regression: the trailing color group was unanchored ("(BK|K|C|M|Y)\s*$"), and
# no toner context was required, so the last letter of any word became a color
# code and any severity word became a toner report:
#   "Replace Drum"   -> magenta toner 0.0%   ("Drum" -> M)
#   "Out of Memory"  -> yellow  toner 0.0%   ("Memory" -> Y)
#   "No Paper"       -> black   toner 0.0%   (no color -> defaults to black)
# --------------------------------------------------------------------------- #

# Real Brother alert strings that are NOT toner reports. Sourced from this
# repo's own device-derived fixtures (the alert-history rows below and in
# test_augment_does_not_use_history_as_fallback_when_live_is_idle), plus the
# component names the maintenance blob decodes (belt / fuser / laser / PF kit).
NON_TONER_ALERTS = [
    "Replace Drum",        # verified misparse -> magenta 0.0%
    "No Paper",            # verified misparse -> black 0.0%
    "Out of Memory",       # verified misparse -> yellow 0.0%
    "No Tray",             # "Tray" -> Y
    "Drum Low",
    "Drum Near End",
    "Replace Belt",
    "Replace Fuser",
    "Replace Laser",
    "Replace PF Kit MP",
    "Replace Ink Cartridge",   # inkjets report ink, not toner
    "Toner OK, Drum Low",      # severity belongs to the drum, not the toner
    "Document Jam",
    "Jam Inside",
    "Cannot Print 3A",
    "Paper Tray Open",
    "Cover is Open",
    "Sleep",
    "Machine Error 4F",
    # Words whose TAIL is a severity keyword ("Timeout" -> out, "Below" -> low,
    # "Mono" -> no) -- the severity group needed a left anchor too.
    "Network Timeout",
    "Below Minimum",
    "Mono Mode",
]


@pytest.mark.parametrize("text", NON_TONER_ALERTS)
def test_parse_alert_fails_closed_on_non_toner_alerts(text):
    """No toner context => no severity and no color, so no reading is invented."""
    assert _parse_alert(text) == (None, None)


# Genuine Brother toner alerts that MUST keep working. "Toner Low (BK)" is the
# exact string in central/snmp.md's device dump; "No Toner" / "Replace Toner" /
# "Toner Low" are the real mono-laser (HL-L2370DW / L2460DW) phrasings.
GENUINE_TONER_ALERTS = [
    ("Toner Low (BK)",     ("low",   "black")),
    ("Toner Low (K)",      ("low",   "black")),
    ("Toner Empty (C)",    ("empty", "cyan")),
    ("Replace Toner (M)",  ("empty", "magenta")),
    ("Toner Near End (Y)", ("low",   "yellow")),
    ("Toner Empty Y",      ("empty", "yellow")),
    ("Toner Empty BK",     ("empty", "black")),
    ("Toner Low: BK",      ("low",   "black")),
    ("Toner Low: C",       ("low",   "cyan")),
    ("Replace Toner BK",   ("empty", "black")),
    ("Toner Depleted (M)", ("empty", "magenta")),
    # Mono phrasings: no color code, caller defaults to the single black toner.
    ("No Toner",           ("empty", None)),
    ("Replace Toner",      ("empty", None)),
    ("Toner Low",          ("low",   None)),
    ("Out of Toner",       ("empty", None)),
    ("Replace Cartridge",  ("empty", None)),
    # Case / spacing / punctuation variants of the same real strings.
    ("toner empty y",      ("empty", "yellow")),
    ("TONER LOW (C)",      ("low",   "cyan")),
    ("ToNeR lOw (bk)",     ("low",   "black")),
    ("Toner Empty Y.",     ("empty", "yellow")),
    ("Toner Low(M)",       ("low",   "magenta")),
    ("  Toner Low (Y)  ",  ("low",   "yellow")),
    ("Toner Empty Y \t",   ("empty", "yellow")),
]


@pytest.mark.parametrize("text,expected", GENUINE_TONER_ALERTS)
def test_parse_alert_still_reads_genuine_toner_alerts(text, expected):
    """The fix must not stop real empty/low toner from being detected."""
    assert _parse_alert(text) == expected


def test_parse_alert_reads_every_color_code():
    """Each Brother color code maps to its color -- in parens and bare."""
    for code, color in (("BK", "black"), ("K", "black"), ("C", "cyan"),
                        ("M", "magenta"), ("Y", "yellow")):
        assert _parse_alert("Toner Empty ({})".format(code)) == ("empty", color)
        assert _parse_alert("Toner Empty {}".format(code)) == ("empty", color)


# One word ending in each color code, in a non-toner alert. Every one of these
# used to hand back that color; none may now.
@pytest.mark.parametrize("word", ["Feedback", "Jack", "Panic", "Drum", "Memory",
                                  "Tray", "Duplex", "Deck", "Program"])
def test_parse_alert_never_takes_a_color_from_a_word_tail(word):
    for text in ("Replace {}".format(word), "No {}".format(word),
                 "{} Low".format(word), "{} Empty".format(word)):
        assert _parse_alert(text) == (None, None), text


def _fuzz_id(text):
    """Name the huge cases by shape rather than by content.

    pytest puts the node ID in ``PYTEST_CURRENT_TEST``, and Windows caps an
    environment variable at 32767 characters -- so the 100k-character case below
    errors at both setup and teardown there, for a reason that has nothing to do
    with the parser under test. Returning None lets pytest name the rest.
    """
    if isinstance(text, str) and len(text) > 64:
        return f"long-{len(text)}"
    return None


@pytest.mark.parametrize("text", [
    "", "   ", "\t\n", None,
    "0x00", "0xdeadbeef",
    "Toner",                      # consumable with no severity
    "Low",                        # severity with no consumable
    "BK", "(BK)", "K", "Y",       # bare color codes, no alert at all
    "État: Bourrage papier", # non-ASCII (accented) status text
    "トナー交換",  # non-ASCII (Japanese) status text
    "\udcff\udcfe",               # lone surrogates, as a decode error would give
    "Replace" * 500,              # very long: no severity/consumable pair
    "A" * 100000,                 # very long garbage
    "Drum " * 5000 + "Low",       # very long non-toner alert
], ids=_fuzz_id)
def test_parse_alert_yields_nothing_for_junk_and_hostile_input(text):
    """Adversarial strings must produce no reading -- and must not raise."""
    assert _parse_alert(text) == (None, None)


def test_parse_alert_survives_a_very_long_genuine_alert():
    """A real severity buried in a long device string still parses (no crash,
    no pathological backtracking) -- the parser is tight, not brittle."""
    assert _parse_alert("Status: " + "x" * 5000 + " Toner Empty (C)") == ("empty", "cyan")


@pytest.mark.parametrize("text", NON_TONER_ALERTS)
async def test_augment_invents_no_supply_level_for_non_toner_alerts(text):
    """End-to-end fail-closed check: a non-toner alert must leave the toner
    untouched -- no fabricated 0.0/15.0 to reach the low-supply alert rule
    (central.worker.jobs) or the forecast history (central.queries)."""
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": None,
             "status_note": "some remaining", "description": "Black Toner Cartridge"},
            {"type": "toner", "color": "magenta", "level_pct": None,
             "status_note": "some remaining", "description": "Magenta Toner Cartridge"},
            {"type": "toner", "color": "yellow", "level_pct": None,
             "status_note": "some remaining", "description": "Yellow Toner Cartridge"},
        ],
        "events": [],
    }
    backend = _backend_with_brother_alerts(alert_text=text)
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading, SYS_OID,
    )
    for supply in out["supplies"]:
        assert supply["level_pct"] is None, (text, supply)
        assert supply["status_note"] == "some remaining", (text, supply)
        assert "_brother_estimated" not in supply, (text, supply)
    assert out["_brother_parsed_severity"] == "none"
    assert out["_brother_source"] == "none"


@pytest.mark.parametrize("code,color", [("BK", "black"), ("K", "black"), ("C", "cyan"),
                                        ("M", "magenta"), ("Y", "yellow")])
async def test_augment_still_flags_the_right_toner_on_a_genuine_empty(code, color):
    """A genuine 'Toner Empty <code>' must still empty exactly that cartridge."""
    reading = {
        "supplies": [
            {"type": "toner", "color": c, "level_pct": None,
             "status_note": "some remaining", "description": "{} Toner".format(c)}
            for c in ("black", "cyan", "magenta", "yellow")
        ],
        "events": [],
    }
    backend = _backend_with_brother_alerts(alert_text="Toner Empty ({})".format(code))
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading, SYS_OID,
    )
    for supply in out["supplies"]:
        if supply["color"] == color:
            assert supply["status_note"] == "empty"
            assert supply["level_pct"] == 0.0
            assert supply["_brother_estimated"] is True
        else:
            assert supply["level_pct"] is None
            assert supply["status_note"] == "some remaining"


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


# --------------------------------------------------------------------------- #
# Hex-description hardening: a non-ASCII prtMarkerSuppliesDescription that
# pysnmp renders as "0x..." must never reach the dashboard verbatim. Real bug
# (HP LaserJet MFP E72430, but the leak is generic): "0x426c61636b" == "Black".
# The Brother provider decodes these and re-derives a missing color so the
# maintenance blob can still find the toner row.
# --------------------------------------------------------------------------- #
def test_decode_hex_description_decodes_real_examples():
    # The exact hex OCTET STRINGs seen in the field, decoded to their names.
    assert _decode_hex_description("0x426c61636b") == "Black"
    assert _decode_hex_description("0x4675736572") == "Fuser"
    assert _decode_hex_description("0x414446") == "ADF"
    assert _decode_hex_description("0x426c61636b204472756d") == "Black Drum"
    # Uppercase 0X prefix + whitespace/colon separators are tolerated.
    assert _decode_hex_description("0X42 6c:61 63 6b") == "Black"


def test_decode_hex_description_leaves_plain_text_untouched():
    # Already-readable descriptions pass straight through (no false hex match).
    assert _decode_hex_description("Black Toner Cartridge") == "Black Toner Cartridge"
    assert _decode_hex_description("Drum Unit") == "Drum Unit"
    assert _decode_hex_description(None) is None
    assert _decode_hex_description("") == ""
    # "0x" + odd-length / non-hex body is NOT a clean octet string -> untouched.
    assert _decode_hex_description("0xZZ") == "0xZZ"
    assert _decode_hex_description("0x123") == "0x123"  # odd length


def test_decode_hex_description_never_returns_hex():
    # Whatever we feed it, the result is never a 0x... string.
    for sample in ("0x426c61636b", "0x4675736572", "0x00", "0xdeadbeef",
                   "Black", "", None):
        out = _decode_hex_description(sample)
        if out:
            assert not out.lower().startswith("0x"), sample


def test_harden_descriptions_decodes_and_backfills_color():
    reading = {
        "supplies": [
            # Colorless hex "Black" toner -> decoded + color re-derived as black.
            {"type": "toner", "color": None, "level_pct": None,
             "description": "0x426c61636b"},
            {"type": "drum", "color": None, "level_pct": 80.0,
             "description": "0x426c61636b204472756d"},  # "Black Drum"
            # Plain rows untouched.
            {"type": "toner", "color": "cyan", "level_pct": 50.0,
             "description": "Cyan Toner Cartridge"},
        ]
    }
    _harden_descriptions(reading)
    black = reading["supplies"][0]
    assert black["description"] == "Black"
    assert black["color"] == "black"  # backfilled so maintenance toner:black matches
    drum = reading["supplies"][1]
    assert drum["description"] == "Black Drum"
    assert drum["color"] == "black"
    cyan = reading["supplies"][2]
    assert cyan["description"] == "Cyan Toner Cartridge"
    assert cyan["color"] == "cyan"


async def test_augment_never_leaks_hex_descriptions():
    """End-to-end: a Brother device whose standard-MIB names came back as hex
    must produce readable descriptions after augment -- no '0x...' anywhere."""
    reading = {
        "supplies": [
            {"type": "toner", "color": None, "level_pct": None,
             "status_note": "some remaining", "description": "0x426c61636b"},  # Black
            {"type": "other", "color": None, "level_pct": 60.0,
             "status_note": None, "description": "0x4675736572"},  # Fuser
            {"type": "drum", "color": None, "level_pct": 80.0,
             "status_note": None, "description": "0x426c61636b204472756d"},  # Black Drum
            {"type": "other", "color": None, "level_pct": 70.0,
             "status_note": None, "description": "0x414446"},  # ADF
        ],
        "events": [],
    }
    backend = _backend_with_brother_alerts()
    out = await BrotherProvider().augment(backend, "10.0.0.1", SnmpParams(), reading, SYS_OID)
    descrs = [s.get("description") for s in out["supplies"]]
    assert all(d and not d.lower().startswith("0x") for d in descrs), descrs
    # The real names survived.
    assert "Black" in descrs
    assert "Fuser" in descrs
    assert "Black Drum" in descrs
    assert "ADF" in descrs


async def test_augment_decoded_color_lets_maintenance_blob_match(monkeypatch):
    """The hex 'Black' toner arrives colorless; after hardening it carries
    color=black, so the maintenance blob's toner:black record finds it and the
    exact 82% lands instead of the cartridge falling through to no percentage."""
    async def passthrough(backend, ip, params, reading, sys_object_id):
        return reading
    monkeypatch.setattr(brother_mod, "_pjl_step", passthrough)
    monkeypatch.setattr(brother_mod, "_ews_step", passthrough)

    reading = {
        "supplies": [
            {"type": "toner", "color": None, "level_pct": None,
             "status_note": "some remaining", "description": "0x426c61636b"},
        ],
        "events": [],
    }
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {OID_MAINTENANCE: _mblob(_mrec("6f", 8200))}, "walks": {},
    }
    out = await BrotherProvider().augment(backend, "10.0.0.1", SnmpParams(), reading, SYS_OID)
    black = out["supplies"][0]
    assert black["description"] == "Black"
    assert black["color"] == "black"
    assert black["level_pct"] == 82.0
    assert black["_maintenance_sourced"] is True


async def test_augment_component_life_rows_keep_names_and_types():
    """Regression: the belt/fuser/laser/PF-kit component-life rows the
    maintenance blob adds must keep their readable names + (type, color) keys --
    these are exactly what central's component-life maintenance matcher keys on
    (see central.worker.jobs._component_supply_matches)."""
    reading = {"supplies": [], "events": []}
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            OID_MAINTENANCE: _mblob(
                _mrec("69", 8800),  # belt
                _mrec("6a", 9700),  # fuser
                _mrec("6b", 9900),  # laser
                _mrec("6c", 7600),  # PF kit MP
                _mrec("6d", 8100),  # PF kit 1
            ),
        },
        "walks": {},
    }
    out = await BrotherProvider().augment(backend, "10.0.0.1", SnmpParams(), reading, SYS_OID)
    by_descr = {s["description"]: s for s in out["supplies"]}
    # Readable names survive -- none is a hex string.
    assert set(by_descr) == {"Belt Unit", "Fuser Unit", "Laser Unit", "PF Kit MP", "PF Kit 1"}
    assert all(not d.lower().startswith("0x") for d in by_descr)
    # (type, color) keys exactly match central's component-life matcher.
    assert (by_descr["Belt Unit"]["type"], by_descr["Belt Unit"]["color"]) == ("other", "belt")
    assert by_descr["Fuser Unit"]["type"] == "fuser"
    assert (by_descr["Laser Unit"]["type"], by_descr["Laser Unit"]["color"]) == ("other", "laser")
    assert (by_descr["PF Kit MP"]["type"], by_descr["PF Kit MP"]["color"]) == ("other", "pf-kit-mp")
    assert (by_descr["PF Kit 1"]["type"], by_descr["PF Kit 1"]["color"]) == ("other", "pf-kit-1")
