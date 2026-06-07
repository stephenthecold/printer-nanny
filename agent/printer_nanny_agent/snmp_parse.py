"""Brand-agnostic parsing helpers for SNMP Printer-MIB (RFC 3805) values.

Vendored into the agent so it installs standalone (no dependency on the central
server package). Kept byte-for-byte behavior-compatible with
``central/snmp_parse.py``; ``tests/test_snmp_parse_parity.py`` guards against drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# prtMarkerSuppliesLevel / prtMarkerSuppliesMaxCapacity sentinel values (RFC 3805).
LEVEL_OTHER = -1          # an unknown/other status, level not reported numerically
LEVEL_UNKNOWN = -2        # explicitly unknown
LEVEL_SOME_REMAINING = -3 # at least one unit remains, exact amount unknown
CAPACITY_UNLIMITED = -1   # supply has no defined maximum (e.g. continuous feed)
CAPACITY_UNKNOWN = -2

# prtMarkerColorantValue / common description keywords → normalized color name.
_COLOR_KEYWORDS = {
    "black": "black",
    "blk": "black",
    "k": "black",
    "cyan": "cyan",
    "magenta": "magenta",
    "yellow": "yellow",
}

# prtMarkerSuppliesType (RFC 3805) integer → our SupplyType string.
_SUPPLY_TYPE_BY_CODE = {
    3: "toner",
    4: "waste",       # wasteToner
    6: "ink",
    9: "drum",        # opc / photoConductor
    10: "developer",
    15: "fuser",
    19: "staples",
}


@dataclass
class ParsedSupplyLevel:
    """Result of normalizing a raw (level, max_capacity) supply pair."""

    level_pct: Optional[float]  # 0..100, or None when genuinely unknown
    known: bool              # False for unknown/other sentinels
    note: Optional[str] = None  # human hint, e.g. "some remaining"


def parse_supply_level(raw_level: Optional[int], max_capacity: Optional[int]) -> ParsedSupplyLevel:
    """Convert SNMP supply level + max capacity into a 0–100 percentage.

    Handles the RFC 3805 sentinels so a printer reporting "-3" (some remaining)
    doesn't get stored as a misleading negative percentage.
    """
    if raw_level is None:
        return ParsedSupplyLevel(level_pct=None, known=False, note="not reported")

    if raw_level == LEVEL_SOME_REMAINING:
        # Treat as low-but-present so it surfaces in low-supply views without a fake number.
        return ParsedSupplyLevel(level_pct=None, known=False, note="some remaining")
    if raw_level in (LEVEL_UNKNOWN, LEVEL_OTHER):
        return ParsedSupplyLevel(level_pct=None, known=False, note="unknown")

    if raw_level < 0:
        return ParsedSupplyLevel(level_pct=None, known=False, note="unknown")

    # Some devices report the level already as a percentage (max == 100 or -1/-2).
    if not max_capacity or max_capacity in (CAPACITY_UNLIMITED, CAPACITY_UNKNOWN):
        pct = float(min(raw_level, 100))
        return ParsedSupplyLevel(level_pct=pct, known=True)

    pct = (raw_level / max_capacity) * 100.0
    pct = max(0.0, min(100.0, pct))
    return ParsedSupplyLevel(level_pct=round(pct, 1), known=True)


def normalize_color(description: Optional[str], colorant: Optional[str] = None) -> Optional[str]:
    """Best-effort color from a colorant value or the supply description text."""
    for source in (colorant, description):
        if not source:
            continue
        text = source.strip().lower()
        if text in _COLOR_KEYWORDS:
            return _COLOR_KEYWORDS[text]
        for keyword, color in _COLOR_KEYWORDS.items():
            if len(keyword) > 1 and keyword in text:
                return color
    return None


def supply_type_from_code(code: Optional[int]) -> str:
    """Map a prtMarkerSuppliesType code to our SupplyType string (default 'other')."""
    if code is None:
        return "other"
    return _SUPPLY_TYPE_BY_CODE.get(code, "other")
