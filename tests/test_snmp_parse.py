"""SNMP supply-level sentinel parsing."""

from __future__ import annotations

from central.snmp_parse import (
    normalize_color,
    parse_supply_level,
    supply_type_from_code,
)


def test_percentage_from_level_and_capacity():
    res = parse_supply_level(150, 600)
    assert res.known is True
    assert res.level_pct == 25.0


def test_level_already_a_percentage_when_capacity_unknown():
    res = parse_supply_level(42, -2)  # capacity unknown sentinel
    assert res.known is True
    assert res.level_pct == 42.0


def test_some_remaining_sentinel_is_not_a_number():
    res = parse_supply_level(-3, 100)
    assert res.known is False
    assert res.level_pct is None
    assert "some remaining" in res.note


def test_unknown_sentinels():
    for sentinel in (-1, -2):
        res = parse_supply_level(sentinel, 100)
        assert res.known is False
        assert res.level_pct is None


def test_none_level():
    res = parse_supply_level(None, 100)
    assert res.known is False
    assert res.level_pct is None


def test_clamped_to_100():
    res = parse_supply_level(700, 600)
    assert res.level_pct == 100.0


def test_color_and_type_normalization():
    assert normalize_color("Black Toner Cartridge") == "black"
    assert normalize_color(None, "cyan") == "cyan"
    assert normalize_color("Photo Drum") is None
    assert supply_type_from_code(3) == "toner"
    assert supply_type_from_code(15) == "fuser"
    assert supply_type_from_code(999) == "other"
    assert supply_type_from_code(None) == "other"
