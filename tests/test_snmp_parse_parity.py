"""Guard: the agent's vendored snmp_parse must behave identically to central's."""

from __future__ import annotations

from central import snmp_parse as central_parse
from printer_nanny_agent import snmp_parse as agent_parse

_LEVELS = [(2500, 10000), (42, -2), (-3, 100), (-2, 100), (-1, 100), (None, 100), (700, 600)]
_COLORS = ["Black Toner Cartridge", "Cyan", "Photo Drum", None]
_CODES = [3, 6, 9, 15, 19, 999, None]


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
