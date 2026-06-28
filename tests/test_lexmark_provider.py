"""Lexmark provider: detection, brand tag, op-panel readout, supply precision,
and the supply decode/classify path (hex -> readable, name -> type+color)."""

from __future__ import annotations

import pytest

from printer_nanny_agent.providers.lexmark import (
    OID_OPERATOR_PANEL_FALLBACK,
    OID_OPERATOR_PANEL_LINE1,
    OID_OPERATOR_PANEL_LINE2,
    LexmarkProvider,
    classify_supply,
    decode_supply_text,
)
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend


def _hex(text: str) -> str:
    """pysnmp-style rendering of a binary OCTET STRING -> '0x…' hex string."""
    return "0x" + text.encode("utf-8").hex()


def test_detect_matches_lexmark_enterprise():
    p = LexmarkProvider()
    assert p.detect({}, "SNMPv2-SMI::enterprises.641.1.4.1") is True
    assert p.detect({}, "1.3.6.1.4.1.641.1.4.1") is True
    assert p.detect({}, "SNMPv2-SMI::enterprises.2435.2.3.9") is False  # Brother
    assert p.detect({}, "SNMPv2-SMI::enterprises.11.2.3.9") is False  # HP
    assert p.detect({}, None) is False


async def test_augment_tags_brand_and_supply_precision():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    reading = {"supplies": [], "events": []}
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    assert out["identity"]["brand"] == "Lexmark"
    assert out["_supply_precision"] == "lexmark_native"


async def test_augment_does_not_overwrite_existing_brand():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    reading = {"identity": {"brand": "Lexmark International"}, "events": []}
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    # The provider must respect a brand that's already been set upstream.
    assert out["identity"]["brand"] == "Lexmark International"


async def test_augment_reads_operator_panel_two_lines():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            OID_OPERATOR_PANEL_LINE1: "88 Cyan Cartridge",
            OID_OPERATOR_PANEL_LINE2: "Low",
        },
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    assert out["device_status_text"] == "88 Cyan Cartridge Low"
    assert any(
        e["code"] == "lexmark-panel" and "88 Cyan Cartridge Low" in e["message"]
        for e in out["events"]
    )


async def test_augment_skips_event_when_panel_says_ready():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            OID_OPERATOR_PANEL_LINE1: "Ready",
            OID_OPERATOR_PANEL_LINE2: "",
        },
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    assert out["device_status_text"] == "Ready"
    # "Ready" is not worth surfacing as an event.
    assert out["events"] == []


async def test_augment_uses_fallback_oid_when_primary_empty():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {OID_OPERATOR_PANEL_FALLBACK: "Paper Jam Tray 1"},
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    assert out["device_status_text"] == "Paper Jam Tray 1"


async def test_augment_handles_no_such_object_strings():
    """pysnmp renders absent OIDs as 'No Such Object/Instance...' - treat as None."""
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            OID_OPERATOR_PANEL_LINE1: "No Such Instance currently exists at this OID",
            OID_OPERATOR_PANEL_LINE2: "",
        },
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    assert out.get("device_status_text") is None


# --------------------------------------------------------------------------
# decode_supply_text -- pysnmp renders binary OCTET STRINGs as "0x…" hex.
# These are the real hex blobs observed on an HP LaserJet MFP E72430 that
# triggered this batch; Lexmark firmware does the same thing.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0x426c61636b", "Black"),              # observed on real hardware
        ("0x4675736572", "Fuser"),              # observed on real hardware
        ("0x414446", "ADF"),                    # observed on real hardware
        ("0x426c61636b204472756d", "Black Drum"),  # observed on real hardware
        (_hex("Black Cartridge"), "Black Cartridge"),
        (_hex("Waste Toner Bottle"), "Waste Toner Bottle"),
        (_hex("Imaging Unit"), "Imaging Unit"),
        (_hex("Fuser Maintenance Kit"), "Fuser Maintenance Kit"),
    ],
)
def test_decode_hex_octet_strings(raw, expected):
    assert decode_supply_text(raw) == expected


def test_decode_passes_through_already_readable():
    assert decode_supply_text("Black Cartridge") == "Black Cartridge"
    assert decode_supply_text("Cyan Toner") == "Cyan Toner"


def test_decode_handles_none_and_blank():
    assert decode_supply_text(None) is None
    assert decode_supply_text("") is None
    assert decode_supply_text("   ") is None


def test_decode_leaves_truly_binary_hex_alone():
    # Odd-length / non-text hex must not be turned into mojibake -- return the
    # original string rather than guessing.
    assert decode_supply_text("0xfffe") == "0xfffe"  # control bytes, not text
    assert decode_supply_text("0x4") == "0x4"        # odd length


# --------------------------------------------------------------------------
# classify_supply -- Lexmark naming -> (SupplyType, color).
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "desc, expected",
    [
        ("Black Cartridge", ("toner", "black")),
        ("Cyan Cartridge", ("toner", "cyan")),
        ("Magenta Cartridge", ("toner", "magenta")),
        ("Yellow Cartridge", ("toner", "yellow")),
        ("Cyan Toner", ("toner", "cyan")),
        ("Imaging Unit", ("drum", None)),
        ("Black Imaging Unit", ("drum", "black")),
        ("Photoconductor", ("drum", None)),
        ("Black Drum", ("drum", "black")),
        ("Fuser", ("fuser", None)),
        ("Fuser Maintenance Kit", ("fuser", None)),  # "fuser" wins over "kit"
        ("Waste Toner Bottle", ("waste", None)),
        ("Maintenance Kit", ("other", None)),
        ("Transfer Belt", ("other", None)),
        ("Transfer Roller", ("other", None)),
        ("Developer Unit", ("developer", None)),
        ("Separator Roller", ("other", None)),
        ("ADF", ("other", None)),
    ],
)
def test_classify_lexmark_vocabulary(desc, expected):
    assert classify_supply(desc) == expected


def test_classify_color_only_word_does_not_color_a_kit():
    # A hypothetical "Black Maintenance Kit" should not carry a color (it's a
    # bundle, not a colorant), while "Black Drum" should keep its color.
    assert classify_supply("Black Maintenance Kit") == ("other", None)
    assert classify_supply("Black Drum") == ("drum", "black")


def test_classify_none_and_unknown():
    assert classify_supply(None) == ("other", None)
    assert classify_supply("") == ("other", None)
    assert classify_supply("Some Future Widget") == ("other", None)


# --------------------------------------------------------------------------
# End-to-end through augment(): hex-named, "other"-typed supplies come out
# readable + correctly (type, color) with NO hex leak. This is the core proof.
# --------------------------------------------------------------------------


async def _augment_with_supplies(supplies):
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    reading = {"supplies": supplies, "events": []}
    return await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )


async def test_augment_decodes_and_classifies_mixed_hex_and_ascii_supplies():
    # Mixed bag: some descriptions are hex (binary OCTET STRING), some plain
    # ASCII, all typed "other" with no color by the generic poller -- exactly
    # the broken state the bug describes.
    supplies = [
        {"type": "other", "color": None, "description": "0x426c61636b",          # "Black"
         "level_pct": 60.0, "status_note": None, "current": 6000, "max_capacity": 10000},
        {"type": "other", "color": None, "description": "Cyan Cartridge",
         "level_pct": 45.0, "status_note": None, "current": None, "max_capacity": None},
        {"type": "other", "color": None, "description": "0x426c61636b204472756d",  # "Black Drum"
         "level_pct": 80.0, "status_note": None, "current": None, "max_capacity": None},
        {"type": "other", "color": None, "description": "0x4675736572",           # "Fuser"
         "level_pct": 90.0, "status_note": None, "current": None, "max_capacity": None},
        {"type": "other", "color": None, "description": "Waste Toner Bottle",
         "level_pct": 12.0, "status_note": None, "current": None, "max_capacity": None},
        {"type": "other", "color": None, "description": "Fuser Maintenance Kit",
         "level_pct": 30.0, "status_note": None, "current": None, "max_capacity": None},
    ]
    out = await _augment_with_supplies(supplies)
    by_desc = {s["description"]: s for s in out["supplies"]}

    # No "0x…" hex leaks anywhere.
    for s in out["supplies"]:
        assert not s["description"].lower().startswith("0x"), s

    assert (by_desc["Black"]["type"], by_desc["Black"]["color"]) == ("toner", "black")
    assert (by_desc["Cyan Cartridge"]["type"], by_desc["Cyan Cartridge"]["color"]) == ("toner", "cyan")
    assert (by_desc["Black Drum"]["type"], by_desc["Black Drum"]["color"]) == ("drum", "black")
    assert (by_desc["Fuser"]["type"], by_desc["Fuser"]["color"]) == ("fuser", None)
    assert (by_desc["Waste Toner Bottle"]["type"], by_desc["Waste Toner Bottle"]["color"]) == ("waste", None)
    # The maintenance kit fuser row maps to fuser (fuser keyword wins); a pure
    # "Maintenance Kit" row would land on "other" with a readable name.
    assert by_desc["Fuser Maintenance Kit"]["type"] == "fuser"

    # Levels and counters are untouched by the provider.
    assert by_desc["Black"]["level_pct"] == 60.0
    assert by_desc["Black"]["current"] == 6000
    assert by_desc["Black"]["max_capacity"] == 10000


async def test_augment_never_downgrades_a_correctly_typed_supply():
    # A row the standard MIB already typed correctly (toner, cyan) must keep its
    # type/color -- the provider only upgrades "other"/missing.
    supplies = [
        {"type": "toner", "color": "cyan", "description": "Cyan Cartridge",
         "level_pct": 50.0, "status_note": None},
        {"type": "drum", "color": None, "description": "Imaging Unit",
         "level_pct": 70.0, "status_note": None},
    ]
    out = await _augment_with_supplies(supplies)
    cyan = out["supplies"][0]
    assert (cyan["type"], cyan["color"]) == ("toner", "cyan")
    drum = out["supplies"][1]
    assert drum["type"] == "drum"


async def test_augment_preserves_brand_and_panel_message_alongside_supplies():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            OID_OPERATOR_PANEL_LINE1: "88 Cyan Cartridge",
            OID_OPERATOR_PANEL_LINE2: "Low",
        },
        "walks": {},
    }
    reading = {
        "supplies": [
            {"type": "other", "color": None, "description": "0x4675736572"},  # "Fuser"
        ],
        "events": [],
    }
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    # Brand tag + supply precision still set.
    assert out["identity"]["brand"] == "Lexmark"
    assert out["_supply_precision"] == "lexmark_native"
    # Panel message intact.
    assert out["device_status_text"] == "88 Cyan Cartridge Low"
    assert any(e["code"] == "lexmark-panel" for e in out["events"])
    # And the supply was decoded + typed.
    fuser = out["supplies"][0]
    assert fuser["description"] == "Fuser"
    assert fuser["type"] == "fuser"


async def test_augment_with_no_supplies_key_does_not_crash():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    reading = {"events": []}  # no "supplies" key at all
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    assert out["identity"]["brand"] == "Lexmark"
