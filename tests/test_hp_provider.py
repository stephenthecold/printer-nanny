"""HP provider: detection, brand + model tagging, status readout, and the
supply-name decode/classify hardening (real LaserJet MFP E72430 hex blobs)."""

from __future__ import annotations

from printer_nanny_agent.providers.hp import (
    OID_HP_DEVICE_MODEL,
    OID_HP_DEVICE_STATUS_MSG,
    HPProvider,
    classify_supply,
    decode_supply_text,
)
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend


def _hx(text: str) -> str:
    """Render text the way pysnmp renders a binary OCTET STRING: a 0x... blob."""
    return "0x" + text.encode("utf-8").hex()


def test_detect_matches_hp_printers_subtree():
    p = HPProvider()
    assert p.detect({}, "SNMPv2-SMI::enterprises.11.2.3.9.1") is True
    assert p.detect({}, "1.3.6.1.4.1.11.2.3.9.1") is True
    # Plain HP enterprise without the printers subtree does NOT match -- HP
    # makes a lot of non-printer hardware on .11.
    assert p.detect({}, "SNMPv2-SMI::enterprises.11.1.2.3") is False
    assert p.detect({}, "SNMPv2-SMI::enterprises.2435.2.3.9") is False  # Brother
    assert p.detect({}, "SNMPv2-SMI::enterprises.641.1.4.1") is False  # Lexmark
    assert p.detect({}, None) is False


async def test_augment_tags_brand_and_supply_precision():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    reading = {"supplies": [], "events": []}
    out = await HPProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.11.2.3.9.1",
    )
    assert out["identity"]["brand"] == "HP"
    assert out["_supply_precision"] == "hp_native"


async def test_augment_reads_status_msg_and_model():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            OID_HP_DEVICE_STATUS_MSG: "Replace Black Cartridge",
            OID_HP_DEVICE_MODEL: "HP LaserJet Pro M404dn",
        },
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await HPProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.11.2.3.9.1",
    )
    assert out["device_status_text"] == "Replace Black Cartridge"
    assert out["identity"]["model"] == "HP LaserJet Pro M404dn"
    assert any(
        e["code"] == "hp-panel" and "Replace Black Cartridge" in e["message"]
        for e in out["events"]
    )


async def test_augment_suppresses_noise_panel_messages():
    """Ready / Sleeping / Energy Save are not worth opening tickets over."""
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {OID_HP_DEVICE_STATUS_MSG: "Sleeping"},
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await HPProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.11.2.3.9.1",
    )
    assert out["device_status_text"] == "Sleeping"
    assert out["events"] == []  # noise suppressed


async def test_augment_does_not_overwrite_existing_model():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {OID_HP_DEVICE_MODEL: "HP LaserJet Pro M404dn"},
        "walks": {},
    }
    reading = {"identity": {"model": "Custom-Set Model"}, "events": []}
    out = await HPProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.11.2.3.9.1",
    )
    # Operator-set model takes priority.
    assert out["identity"]["model"] == "Custom-Set Model"


async def test_augment_swallows_no_such_object():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            OID_HP_DEVICE_STATUS_MSG: "No Such Object available on this device",
            OID_HP_DEVICE_MODEL: "",
        },
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await HPProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.11.2.3.9.1",
    )
    assert out.get("device_status_text") is None
    assert out.get("identity", {}).get("model") is None
    # Provider still always sets the brand + precision.
    assert out["identity"]["brand"] == "HP"
    assert out["_supply_precision"] == "hp_native"


# --------------------------------------------------------------------------
# Supply decode/classify hardening (LaserJet MFP E72430 hex-blob descriptions)
# --------------------------------------------------------------------------


def test_decode_supply_text_real_hex_examples():
    """The exact hex blobs seen on a LaserJet MFP E72430 decode to real names."""
    # From the bug report: 426c61636b="Black", 4675736572="Fuser",
    # 414446="ADF", 426c61636b204472756d="Black Drum".
    assert decode_supply_text("0x426c61636b") == "Black"
    assert decode_supply_text("0x4675736572") == "Fuser"
    assert decode_supply_text("0x414446") == "ADF"
    assert decode_supply_text("0x426c61636b204472756d") == "Black Drum"


def test_decode_supply_text_passthrough_and_edges():
    # Already-readable text is returned untouched.
    assert decode_supply_text("Black Cartridge HP CF259A") == "Black Cartridge HP CF259A"
    assert decode_supply_text("Toner (K)") == "Toner (K)"
    # Honest no-ops / non-decodable inputs never raise and never invent text.
    assert decode_supply_text(None) is None
    assert decode_supply_text("") is None
    assert decode_supply_text("   ") is None
    # Odd-length / non-hex blob: leave it as-is rather than guess.
    assert decode_supply_text("0xZZ") == "0xZZ"
    assert decode_supply_text("0x4") == "0x4"
    # Trailing NUL padding is trimmed.
    assert decode_supply_text("0x426c61636b00") == "Black"


def test_classify_supply_types_and_colors():
    # Bare colour names are toner cartridges.
    assert classify_supply("Black") == ("toner", "black")
    assert classify_supply("Cyan") == ("toner", "cyan")
    assert classify_supply("Magenta") == ("toner", "magenta")
    assert classify_supply("Yellow") == ("toner", "yellow")
    # Part keywords win over the bare colour.
    assert classify_supply("Black Drum") == ("drum", "black")
    assert classify_supply("Drum") == ("drum", None)
    assert classify_supply("Fuser") == ("fuser", None)
    assert classify_supply("Fuser Kit") == ("fuser", None)
    assert classify_supply("Waste Toner Box") == ("waste", None)
    assert classify_supply("Staple Cartridge") == ("staples", None)
    # Kits have no dedicated SupplyType -> "other", but a readable name.
    assert classify_supply("Transfer Kit") == ("other", None)
    assert classify_supply("Maintenance Kit") == ("other", None)
    assert classify_supply("Image Transfer Belt") == ("other", None)
    # Nothing recognizable -> no opinion (caller keeps what it had).
    assert classify_supply("ADF") == (None, None)
    assert classify_supply(None) == (None, None)


async def test_augment_decodes_hex_supplies_end_to_end():
    """An HP MFP whose marker-supply descriptions arrive as hex OCTET STRINGs
    ends up with readable, correctly-typed/colored supplies -- no 0x... leak."""
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}

    # Standard-MIB supplies as the poller would have built them: hex descriptions
    # and the generic "other" type that some HP firmware reports for everything.
    reading = {
        "supplies": [
            {"type": "other", "color": None, "description": _hx("Black"),
             "level_pct": 47.0},
            {"type": "other", "color": None, "description": _hx("Cyan"),
             "level_pct": 80.0},
            {"type": "other", "color": None, "description": _hx("Magenta"),
             "level_pct": 12.0},
            {"type": "other", "color": None, "description": _hx("Yellow"),
             "level_pct": 60.0},
            {"type": "other", "color": None, "description": _hx("Black Drum"),
             "level_pct": 90.0},
            {"type": "other", "color": None, "description": _hx("Fuser"),
             "level_pct": 55.0},
            {"type": "other", "color": None, "description": _hx("Transfer Kit"),
             "level_pct": 70.0},
            {"type": "other", "color": None, "description": _hx("Waste Toner Box"),
             "level_pct": 30.0},
            {"type": "other", "color": None, "description": _hx("ADF"),
             "level_pct": None},
        ],
        "events": [],
    }

    out = await HPProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.11.2.3.9.1",
    )

    by_desc = {s["description"]: s for s in out["supplies"]}

    # No hex blob leaks anywhere.
    for s in out["supplies"]:
        assert not str(s["description"]).lower().startswith("0x"), s

    # Toners: readable name, (toner, <color>).
    assert (by_desc["Black"]["type"], by_desc["Black"]["color"]) == ("toner", "black")
    assert (by_desc["Cyan"]["type"], by_desc["Cyan"]["color"]) == ("toner", "cyan")
    assert (by_desc["Magenta"]["type"], by_desc["Magenta"]["color"]) == ("toner", "magenta")
    assert (by_desc["Yellow"]["type"], by_desc["Yellow"]["color"]) == ("toner", "yellow")

    # Imaging / fuser / waste get their proper types.
    assert (by_desc["Black Drum"]["type"], by_desc["Black Drum"]["color"]) == ("drum", "black")
    assert by_desc["Fuser"]["type"] == "fuser"
    assert by_desc["Waste Toner Box"]["type"] == "waste"

    # Maintenance/transfer kits stay "other" but readable.
    assert by_desc["Transfer Kit"]["type"] == "other"

    # Unmapped part (ADF) keeps its existing type but is readable, not hex.
    assert by_desc["ADF"]["description"] == "ADF"
    assert by_desc["ADF"]["type"] == "other"

    # Precision badge preserved.
    assert out["_supply_precision"] == "hp_native"


async def test_augment_does_not_downgrade_good_standard_mib_supplies():
    """When the standard MIB already typed a supply, HP keeps it -- we only
    fill missing type/color, never overwrite correct data."""
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    reading = {
        "supplies": [
            # Standard MIB already nailed (toner, black) with a plain name.
            {"type": "toner", "color": "black", "description": "Black Cartridge",
             "level_pct": 40.0},
            # Typed drum but missing color -> color filled, type untouched.
            {"type": "drum", "color": None, "description": "Black Drum",
             "level_pct": 90.0},
        ],
        "events": [],
    }
    out = await HPProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.11.2.3.9.1",
    )
    assert out["supplies"][0]["type"] == "toner"
    assert out["supplies"][0]["color"] == "black"
    assert out["supplies"][1]["type"] == "drum"  # not downgraded
    assert out["supplies"][1]["color"] == "black"  # filled in
