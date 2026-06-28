"""Hex OCTET-STRING supply-name decoding + name->(type,color) classification.

Regression coverage for the HP LaserJet MFP E72430 bug where
prtMarkerSuppliesDescription came back as raw "0x..." hex strings (e.g.
0x426c61636b == "Black") and every cartridge fell through to SupplyType
"other" with no color. Exercises the shared helpers and the agent's
build_supplies() end-to-end over a mixed hex/ASCII walk.
"""

from __future__ import annotations

from printer_nanny_agent import oids
from printer_nanny_agent.poller import build_supplies
from printer_nanny_agent.snmp_parse import (
    classify_supply,
    decode_supply_text,
)

# The exact hex strings pulled from the real bug report.
HEX_BLACK = "0x426c61636b"                                  # "Black"
HEX_FUSER = "0x4675736572"                                  # "Fuser"
HEX_ADF = "0x414446"                                        # "ADF"
HEX_ADF_KIT = "0x414446204d61696e74656e616e6365204b6974"   # "ADF Maintenance Kit"
HEX_BLACK_DRUM = "0x426c61636b204472756d"                   # "Black Drum"


# --------------------------------------------------------------------------- #
# decode_supply_text                                                          #
# --------------------------------------------------------------------------- #

def test_decode_hex_utf8_roundtrip():
    assert decode_supply_text(HEX_BLACK) == "Black"
    assert decode_supply_text(HEX_FUSER) == "Fuser"
    assert decode_supply_text(HEX_ADF) == "ADF"
    assert decode_supply_text(HEX_ADF_KIT) == "ADF Maintenance Kit"
    assert decode_supply_text(HEX_BLACK_DRUM) == "Black Drum"


def test_decode_hex_with_real_unicode_utf8():
    # "Tóner" in UTF-8 -- non-ASCII byte forces the 0x render; must come back clean.
    hexval = "0x" + "Tóner".encode("utf-8").hex()
    assert decode_supply_text(hexval) == "Tóner"


def test_decode_hex_latin1_fallback():
    # 0xff is not valid UTF-8; Latin-1 decodes it as "ÿ". "Cyanÿ" stays readable.
    raw = "Cyan".encode("latin-1") + b"\xff"
    hexval = "0x" + raw.hex()
    decoded = decode_supply_text(hexval)
    assert decoded is not None
    assert decoded.startswith("Cyan")
    assert "0x" not in decoded


def test_decode_strips_trailing_nul_and_control():
    # Many printers NUL-terminate / pad the OCTET STRING.
    raw = b"Black\x00\x00"
    assert decode_supply_text("0x" + raw.hex()) == "Black"
    raw2 = b"Yellow\x01\x02"
    assert decode_supply_text("0x" + raw2.hex()) == "Yellow"


def test_decode_non_printable_uses_fallback():
    # Pure binary control bytes are not real text -> keep the sensible fallback,
    # never leak the 0x blob.
    binary = "0x" + bytes([0x00, 0x01, 0x02, 0x03, 0x04]).hex()
    assert decode_supply_text(binary, fallback="toner") == "toner"
    # Without a fallback, return None rather than the hex.
    assert decode_supply_text(binary) is None


def test_decode_readable_text_passes_through():
    assert decode_supply_text("Cyan Toner Cartridge") == "Cyan Toner Cartridge"
    assert decode_supply_text("Black") == "Black"
    assert decode_supply_text(None) is None


def test_decode_no_hex_leak_ever():
    for val in (HEX_BLACK, HEX_FUSER, HEX_ADF, HEX_ADF_KIT, HEX_BLACK_DRUM):
        out = decode_supply_text(val, fallback="other")
        assert out is not None
        assert not out.lower().startswith("0x")


# --------------------------------------------------------------------------- #
# classify_supply                                                            #
# --------------------------------------------------------------------------- #

def test_classify_toner_colors():
    # Generic/other type code, but the name tells us the truth.
    assert classify_supply("Black", type_code=1) == ("toner", "black")
    assert classify_supply("Cyan", type_code=None) == ("toner", "cyan")
    assert classify_supply("Magenta Toner", type_code=1) == ("toner", "magenta")
    assert classify_supply("Yellow Toner Cartridge", type_code=1) == ("toner", "yellow")


def test_classify_specific_code_wins_but_color_kept():
    # type_code 3 == toner; "Cyan Toner Cartridge" keeps the color.
    assert classify_supply("Cyan Toner Cartridge", type_code=3) == ("toner", "cyan")
    # type_code 9 == drum; color extracted from "Black Drum".
    assert classify_supply("Black Drum", type_code=9) == ("drum", "black")


def test_classify_drum_family():
    assert classify_supply("Drum", type_code=1) == ("drum", None)
    assert classify_supply("Black Drum", type_code=1) == ("drum", "black")
    assert classify_supply("Imaging Unit", type_code=1) == ("drum", None)
    assert classify_supply("Photoconductor", type_code=1) == ("drum", None)


def test_classify_fuser():
    assert classify_supply("Fuser", type_code=1) == ("fuser", None)
    assert classify_supply("Fusing Unit", type_code=1) == ("fuser", None)


def test_classify_waste():
    assert classify_supply("Waste", type_code=1) == ("waste", None)
    assert classify_supply("Waste Toner Box", type_code=1) == ("waste", None)
    assert classify_supply("Toner Collection Unit", type_code=1) == ("waste", None)


def test_classify_kits_and_transfer_are_other_with_name():
    # Maintenance / transfer / belt / ADF kits aren't a specific SupplyType --
    # they land as "other" but the readable name is preserved by the caller.
    assert classify_supply("ADF Maintenance Kit", type_code=1) == ("other", None)
    assert classify_supply("Transfer Belt", type_code=1) == ("other", None)
    assert classify_supply("Maintenance Kit", type_code=1) == ("other", None)


def test_classify_none_description_falls_back_to_code():
    assert classify_supply(None, type_code=3) == ("toner", None)
    assert classify_supply(None, type_code=None) == ("other", None)


# --------------------------------------------------------------------------- #
# build_supplies end-to-end over a mixed hex/ASCII walk                       #
# --------------------------------------------------------------------------- #

def _mixed_walk():
    d = oids.PRT_MARKER_SUPPLIES_DESCRIPTION
    t = oids.PRT_MARKER_SUPPLIES_TYPE
    mx = oids.PRT_MARKER_SUPPLIES_MAX_CAPACITY
    lv = oids.PRT_MARKER_SUPPLIES_LEVEL
    return {
        d: {
            f"{d}.1.1": HEX_BLACK,            # "Black"
            f"{d}.1.2": HEX_FUSER,            # "Fuser"
            f"{d}.1.3": HEX_ADF_KIT,          # "ADF Maintenance Kit"
            f"{d}.1.4": HEX_BLACK_DRUM,       # "Black Drum"
            f"{d}.1.5": "Cyan Toner Cartridge",  # already ASCII
        },
        # Everything reported as generic 'other' (1) -- the real-bug condition.
        t: {
            f"{t}.1.1": "1",
            f"{t}.1.2": "1",
            f"{t}.1.3": "1",
            f"{t}.1.4": "1",
            f"{t}.1.5": "1",
        },
        mx: {
            f"{mx}.1.1": "1000",
            f"{mx}.1.2": "1000",
            f"{mx}.1.3": "1000",
            f"{mx}.1.4": "1000",
            f"{mx}.1.5": "1000",
        },
        lv: {
            f"{lv}.1.1": "800",
            f"{lv}.1.2": "500",
            f"{lv}.1.3": "900",
            f"{lv}.1.4": "300",
            f"{lv}.1.5": "250",
        },
    }


def test_build_supplies_decodes_hex_and_classifies():
    supplies = build_supplies(_mixed_walk())
    by_desc = {s["description"]: s for s in supplies}

    # No raw hex leaks anywhere.
    for s in supplies:
        assert s["description"] is not None
        assert not s["description"].lower().startswith("0x")

    assert by_desc["Black"]["type"] == "toner"
    assert by_desc["Black"]["color"] == "black"
    assert by_desc["Black"]["level_pct"] == 80.0

    assert by_desc["Fuser"]["type"] == "fuser"
    assert by_desc["Fuser"]["color"] is None
    assert by_desc["Fuser"]["level_pct"] == 50.0

    assert by_desc["ADF Maintenance Kit"]["type"] == "other"
    assert by_desc["ADF Maintenance Kit"]["level_pct"] == 90.0

    assert by_desc["Black Drum"]["type"] == "drum"
    assert by_desc["Black Drum"]["color"] == "black"
    assert by_desc["Black Drum"]["level_pct"] == 30.0

    assert by_desc["Cyan Toner Cartridge"]["type"] == "toner"
    assert by_desc["Cyan Toner Cartridge"]["color"] == "cyan"
    assert by_desc["Cyan Toner Cartridge"]["level_pct"] == 25.0


def test_build_supplies_binary_description_falls_back_to_type_name():
    d = oids.PRT_MARKER_SUPPLIES_DESCRIPTION
    t = oids.PRT_MARKER_SUPPLIES_TYPE
    lv = oids.PRT_MARKER_SUPPLIES_LEVEL
    # Pure binary description + a specific type code (3 == toner) -> the name
    # falls back to the type name, never the 0x blob.
    binary = "0x" + bytes([0x00, 0x01, 0x02, 0x03]).hex()
    walks = {
        d: {f"{d}.1.1": binary},
        t: {f"{t}.1.1": "3"},
        lv: {f"{lv}.1.1": "500"},
    }
    sup = build_supplies(walks)[0]
    assert sup["description"] == "toner"
    assert not sup["description"].lower().startswith("0x")
    assert sup["type"] == "toner"
