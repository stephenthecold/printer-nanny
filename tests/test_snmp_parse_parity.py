"""Guard: the agent's vendored snmp_parse must behave identically to central's."""

from __future__ import annotations

from central import snmp_parse as central_parse
from printer_nanny_agent import snmp_parse as agent_parse

_LEVELS = [(2500, 10000), (42, -2), (-3, 100), (-2, 100), (-1, 100), (None, 100), (700, 600)]
_COLORS = ["Black Toner Cartridge", "Cyan", "Photo Drum", None]
_CODES = [3, 6, 9, 15, 19, 999, None]
_HEX = [
    "0x426c61636b",                                  # "Black"
    "0x4675736572",                                  # "Fuser"
    "0x414446204d61696e74656e616e6365204b6974",      # "ADF Maintenance Kit"
    "0x426c61636b204472756d",                        # "Black Drum"
    "Cyan Toner Cartridge",                          # already-readable
    "0x" + bytes([0, 1, 2, 3]).hex(),                # non-printable binary
    None,
]
_CLASSIFY = [
    ("Black", 1),
    ("Cyan Toner Cartridge", 3),
    ("Black Drum", 1),
    ("Fuser", 1),
    ("Toner Collection Unit", 1),
    ("ADF Maintenance Kit", 1),
    (None, None),
]


def test_supply_level_parity():
    for level, cap in _LEVELS:
        a = agent_parse.parse_supply_level(level, cap)
        c = central_parse.parse_supply_level(level, cap)
        assert (a.level_pct, a.known, a.note) == (c.level_pct, c.known, c.note)


def test_color_parity():
    for desc in _COLORS:
        assert agent_parse.normalize_color(desc) == central_parse.normalize_color(desc)


def test_type_code_parity():
    for code in _CODES:
        assert agent_parse.supply_type_from_code(code) == central_parse.supply_type_from_code(code)


def test_decode_supply_text_parity():
    for val in _HEX:
        assert agent_parse.decode_supply_text(val, "other") == central_parse.decode_supply_text(val, "other")


def test_classify_supply_parity():
    for desc, code in _CLASSIFY:
        assert agent_parse.classify_supply(desc, code) == central_parse.classify_supply(desc, code)
