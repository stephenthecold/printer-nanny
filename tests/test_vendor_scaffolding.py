"""Defensive supply hardening for the scaffolding brands.

The long-tail vendor providers (Xerox / Kyocera / Canon / Ricoh / Konica
Minolta) don't decode private-MIB supply tables, but they DO clean up the
standard Printer-MIB supplies the poller already built:

  * pysnmp renders a binary OCTET STRING description as a "0x…" hex string.
    That must be decoded to readable text -- no "0x…" may leak through.
  * the standard prtMarkerSuppliesType code is frequently the catch-all
    "other", so recognizable names (Black / Drum / Fuser / Waste / ...) must be
    re-classified to the right SupplyType + color.

These tests use the real hex examples seen on an HP LaserJet MFP E72430
(426c61636b="Black", 4675736572="Fuser", 414446="ADF",
426c61636b204472756d="Black Drum") plus plain-ASCII names, and assert the
provider augment leaves clean, classified supplies while still setting the
brand tag and front-panel status message.
"""

from __future__ import annotations

import pytest

from printer_nanny_agent.providers._vendors import (
    KonicaMinoltaProvider,
    XeroxProvider,
    classify_supply,
    decode_supply_text,
)
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend


def _hex(text: str) -> str:
    """Render text the way pysnmp renders a binary OCTET STRING: '0x<hex>'."""
    return "0x" + text.encode("utf-8").hex()


# --- decode_supply_text -----------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        (_hex("Black"), "Black"),
        (_hex("Fuser"), "Fuser"),
        (_hex("ADF"), "ADF"),
        (_hex("Black Drum"), "Black Drum"),
        # NUL / control padding some firmwares append must be stripped.
        ("0x426c61636b0000", "Black"),
        # Clean ASCII passes through unchanged.
        ("Cyan Cartridge", "Cyan Cartridge"),
        # Whitespace collapses.
        ("  Waste   Toner  ", "Waste Toner"),
        # Odd-length hex is tolerated (left-padded), never returned as 0x…
        ("0x42", "B"),
        (None, None),
        ("", None),
    ],
)
def test_decode_supply_text_never_leaks_hex(raw, expected):
    out = decode_supply_text(raw)
    assert out == expected
    if out is not None:
        assert not out.lower().startswith("0x")


# --- classify_supply --------------------------------------------------------

@pytest.mark.parametrize(
    "name,expected_type,expected_color",
    [
        ("Black", "toner", "black"),
        ("Cyan", "toner", "cyan"),
        ("Magenta Toner Cartridge", "toner", "magenta"),
        ("Yellow", "toner", "yellow"),
        ("Fuser", "fuser", None),
        ("Fuser Unit", "fuser", None),
        ("Drum", "drum", None),
        ("Black Drum", "drum", "black"),  # component word wins over color
        ("Imaging Unit", "drum", None),
        ("Waste", "waste", None),
        ("Waste Toner Box", "waste", None),  # 'waste' wins over 'toner'
        ("Developer", "developer", None),
        ("Staple Cartridge", "staples", None),
        # Maintenance / transfer / PF kits have no dedicated type -> other,
        # but the readable name is preserved by the caller.
        ("Maintenance Kit", "other", None),
        ("Transfer Belt", "other", None),
        ("ADF", "other", None),
        ("PF Kit", "other", None),
    ],
)
def test_classify_supply(name, expected_type, expected_color):
    stype, color = classify_supply(name)
    assert stype == expected_type
    assert color == expected_color


def test_classify_supply_handles_empty():
    assert classify_supply(None) == ("other", None)
    assert classify_supply("") == ("other", None)


# --- end-to-end through a couple of provider augments -----------------------

# The real E72430 leak (hex) plus an ASCII row, every one of which the standard
# poller would have left as SupplyType "other" with no color.
def _bug_supplies() -> list:
    return [
        {"type": "other", "color": None, "description": _hex("Black"),
         "level_pct": 60.0},
        {"type": "other", "color": None, "description": _hex("Fuser"),
         "level_pct": 80.0},
        {"type": "other", "color": None, "description": _hex("Black Drum"),
         "level_pct": 40.0},
        {"type": "other", "color": None, "description": _hex("ADF"),
         "level_pct": 90.0},
        # An ASCII waste row with the wrong fallthrough type.
        {"type": "other", "color": None, "description": "Waste Toner",
         "level_pct": 12.0},
    ]


def _assert_no_hex(supplies: list) -> None:
    for s in supplies:
        desc = s.get("description") or ""
        assert not desc.lower().startswith("0x"), f"hex leaked: {desc!r}"


@pytest.mark.parametrize(
    "provider_cls,prefix,brand,precision,panel_oid,panel_text",
    [
        (XeroxProvider, "253", "Xerox", "xerox_standard",
         "1.3.6.1.4.1.253.8.53.13.2.1.6.1.1", "Replace Toner Cartridge"),
        (KonicaMinoltaProvider, "18334", "Konica Minolta",
         "konica_minolta_standard",
         "1.3.6.1.4.1.18334.1.1.1.5.7.1.1.4.1", "Toner Low (Cyan)"),
    ],
)
async def test_augment_hardens_supplies_and_keeps_brand_and_message(
    provider_cls, prefix, brand, precision, panel_oid, panel_text,
):
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {panel_oid: panel_text}, "walks": {}}
    sys_oid = f"SNMPv2-SMI::enterprises.{prefix}.1.2.3"
    reading = {"supplies": _bug_supplies(), "events": []}

    out = await provider_cls().augment(
        backend, "10.0.0.1", SnmpParams(), reading, sys_oid,
    )
    supplies = out["supplies"]

    # 1. No hex leaks anywhere.
    _assert_no_hex(supplies)

    by_desc = {s["description"]: s for s in supplies}
    # 2. Hex decoded to readable names.
    assert set(by_desc) == {"Black", "Fuser", "Black Drum", "ADF", "Waste Toner"}

    # 3. Correct (type, color) classification.
    assert (by_desc["Black"]["type"], by_desc["Black"]["color"]) == ("toner", "black")
    assert (by_desc["Fuser"]["type"], by_desc["Fuser"]["color"]) == ("fuser", None)
    assert (by_desc["Black Drum"]["type"], by_desc["Black Drum"]["color"]) == ("drum", "black")
    assert (by_desc["Waste Toner"]["type"], by_desc["Waste Toner"]["color"]) == ("waste", None)
    # Maintenance-ish (ADF) stays "other" but with a readable name.
    assert by_desc["ADF"]["type"] == "other"

    # 4. level_pct is untouched (we only clean name/type/color).
    assert by_desc["Black"]["level_pct"] == 60.0

    # 5. Brand tag + front-panel status message behavior preserved.
    assert out["identity"]["brand"] == brand
    assert out["_supply_precision"] == precision
    assert out["device_status_text"] == panel_text
    assert any(panel_text in e["message"] for e in out["events"])


async def test_augment_does_not_override_specific_standard_type():
    """A type the standard MIB already pinned (e.g. 'drum') is not downgraded."""
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    # Description says "Black" (would classify toner) but the standard
    # prtMarkerSuppliesType code already pinned it as a drum -- keep the drum.
    reading = {
        "supplies": [{"type": "drum", "color": None, "description": "Black"}],
        "events": [],
    }
    out = await XeroxProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.253.1",
    )
    s = out["supplies"][0]
    assert s["type"] == "drum"          # not downgraded to toner
    assert s["color"] == "black"        # color still filled in
    assert s["description"] == "Black"


async def test_augment_with_no_supplies_is_quiet():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    reading = {"supplies": [], "events": []}
    out = await XeroxProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.253.1",
    )
    assert out["supplies"] == []
    assert out["identity"]["brand"] == "Xerox"
    assert out["_supply_precision"] == "xerox_standard"
