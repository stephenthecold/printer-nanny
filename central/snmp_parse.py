"""Brand-agnostic parsing helpers for SNMP Printer-MIB (RFC 3805) values.

Kept dependency-free so the Milestone-2 agent can import the same logic it uses
to translate raw SNMP gets into the normalized values the central API stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

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

# Type codes the standard MIB treats as non-specific. When the code is one of
# these (or absent) we lean on the description text to classify the supply,
# because plenty of devices report everything as other(1)/unknown(2).
_GENERIC_TYPE_CODES = {None, 1, 2}

# Description keyword → SupplyType string. Order matters: the first phrase that
# appears in the (lowercased) description wins, so put the more specific
# multi-word phrases before the bare words they contain. Only emits SupplyType
# values that exist in central.models.SupplyType.
_DESC_TYPE_KEYWORDS = (
    ("toner collection", "waste"),   # "Toner Collection Unit/Box" == waste toner
    ("waste toner", "waste"),
    ("waste", "waste"),
    ("imaging unit", "drum"),        # Xerox/OKI name for the OPC drum
    ("photoconductor", "drum"),
    ("photo conductor", "drum"),
    ("imaging drum", "drum"),
    ("drum", "drum"),
    ("opc", "drum"),
    ("fusing", "fuser"),
    ("fuser", "fuser"),
    ("developer", "developer"),
    ("staple", "staples"),
    ("ink", "ink"),
    ("toner", "toner"),
    ("cartridge", "toner"),          # bare "Black Cartridge" == toner cartridge
)


def decode_supply_text(value: Optional[str], fallback: Optional[str] = None) -> Optional[str]:
    """Turn a possibly hex-encoded SNMP OCTET STRING into readable text.

    pysnmp renders an OCTET STRING that isn't clean ASCII as a ``0x...`` hex
    string (e.g. ``0x426c61636b`` for ``Black``). Some printers stuff
    prtMarkerSuppliesDescription with bytes that trip that path even though the
    payload is perfectly good UTF-8/Latin-1 text. Decode those back to text:

    - A value already in readable text passes through unchanged.
    - A ``0x...`` hex string is decoded hex→bytes→text (UTF-8, then Latin-1),
      with NULs and other control characters stripped.
    - If the decoded bytes still aren't printable text (genuine binary), return
      ``fallback`` rather than leaking the ``0x...`` blob to the UI.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return fallback if fallback is not None else value

    lowered = text.lower()
    if lowered.startswith("0x"):
        hexpart = text[2:].replace(" ", "")
        if len(hexpart) % 2:
            hexpart = "0" + hexpart
        try:
            raw = bytes.fromhex(hexpart)
        except ValueError:
            # Not actually hex (e.g. a model literally named "0xABC something")
            # -- treat as plain text.
            return text
        decoded = _bytes_to_text(raw)
        if decoded is None:
            return fallback
        return decoded

    # Already-readable text: hand it back unchanged.
    return text


def _bytes_to_text(raw: bytes) -> Optional[str]:
    """Decode bytes to a clean printable string, or None if it isn't text."""
    for encoding in ("utf-8", "latin-1"):
        try:
            decoded = raw.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
        # Strip NULs and other C0/C1 control chars (keep normal whitespace).
        cleaned = "".join(
            ch for ch in decoded if ch in ("\t", " ") or (ch.isprintable() and ch != "\x7f")
        ).strip()
        if not cleaned:
            return None
        # Require the result to be mostly printable -- guards against latin-1
        # happily decoding random binary into mojibake.
        printable = sum(1 for ch in cleaned if ch.isprintable())
        if printable / len(cleaned) >= 0.8:
            return cleaned
    return None


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


def supply_type_from_description(description: Optional[str]) -> Optional[str]:
    """Best-effort SupplyType from the description text, or None if no keyword hits.

    Used to rescue supplies whose prtMarkerSuppliesType is generic/other but
    whose name ("Black Drum", "Fuser Unit", "Toner Collection Box") tells us
    exactly what they are.
    """
    if not description:
        return None
    text = description.lower()
    for keyword, supply_type in _DESC_TYPE_KEYWORDS:
        if keyword in text:
            return supply_type
    return None


def classify_supply(
    description: Optional[str],
    type_code: Optional[int] = None,
    colorant: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Resolve a supply's (SupplyType, color) from its code, name and colorant.

    The MIB type code wins when it's specific; when it's generic/other/unknown
    (or absent) we classify from the description keywords. Color is always
    extracted from colorant/description so "Black Drum" -> (drum, black) and
    "Cyan Toner Cartridge" -> (toner, cyan). Returns a SupplyType string that is
    always valid for central.models.SupplyType (falls back to "other").
    """
    color = normalize_color(description, colorant)

    if type_code is not None and type_code not in _GENERIC_TYPE_CODES:
        coded = _SUPPLY_TYPE_BY_CODE.get(type_code)
        if coded is not None:
            return coded, color

    from_desc = supply_type_from_description(description)
    if from_desc is not None:
        return from_desc, color

    # No component keyword, but a bare color name ("Black", "Cyan") on a laser
    # device means a toner cartridge -- that's how vendors label the slots.
    if color is not None:
        return "toner", color

    # Description gave us no signal at all. Fall back to whatever the code
    # mapped to (which is "other" for generic codes); keep the readable name.
    return supply_type_from_code(type_code), color
