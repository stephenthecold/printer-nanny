"""Poll one printer over SNMP and assemble the reading payload the central
ingest API accepts. Parsing is split into pure helpers so it can be unit-tested
without any SNMP I/O.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from printer_nanny_agent import oids
from printer_nanny_agent.snmp_parse import (
    normalize_color,
    parse_supply_level,
    supply_type_from_code,
)
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams

# Vendor keywords matched (case-insensitively) in sysDescr / printer name.
_VENDORS = [
    "HP", "Hewlett", "Brother", "Canon", "Xerox", "Lexmark", "Konica",
    "Ricoh", "Kyocera", "Epson", "Samsung", "Dell", "OKI", "Sharp", "Toshiba",
]

# hrPrinterStatus values that mean the device is operating normally.
_OK_PRINTER_STATUS = {"3", "4", "5"}  # idle, printing, warmup


def _to_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _oid_suffix(full_oid: str, base: str) -> str:
    """The index portion of ``full_oid`` after ``base`` (e.g. '.1.1')."""
    full = full_oid.lstrip(".")
    base = base.lstrip(".")
    return full[len(base):] if full.startswith(base) else full


def _vendor_from(*texts: Optional[str]) -> Optional[str]:
    for text in texts:
        if not text:
            continue
        low = text.lower()
        for vendor in _VENDORS:
            if vendor.lower() in low:
                return "HP" if vendor == "Hewlett" else vendor
    return None


def parse_error_bits(value: Optional[str]) -> List[int]:
    """Decode hrPrinterDetectedErrorState (a BITS field) into set bit indices.

    Bit 0 is the most-significant bit of the first octet (RFC 1759). Accepts the
    hex string pysnmp renders (e.g. '0x4000') or a raw byte string.
    """
    if not value:
        return []
    raw: bytes
    text = value.strip()
    if text.lower().startswith("0x"):
        hexpart = text[2:].replace(" ", "")
        if len(hexpart) % 2:
            hexpart = "0" + hexpart
        try:
            raw = bytes.fromhex(hexpart)
        except ValueError:
            return []
    else:
        raw = text.encode("latin-1", errors="ignore")
    bits: List[int] = []
    for octet_index, byte in enumerate(raw):
        for k in range(8):
            if byte & (0x80 >> k):
                bits.append(octet_index * 8 + k)
    return bits


def build_supplies(walks: Dict[str, Dict[str, str]]) -> List[dict]:
    """Assemble supply dicts from the walked marker-supply tables, keyed by index."""
    descriptions = walks.get(oids.PRT_MARKER_SUPPLIES_DESCRIPTION, {})
    types = walks.get(oids.PRT_MARKER_SUPPLIES_TYPE, {})
    maxes = walks.get(oids.PRT_MARKER_SUPPLIES_MAX_CAPACITY, {})
    levels = walks.get(oids.PRT_MARKER_SUPPLIES_LEVEL, {})

    # Index supplies by their OID suffix so the parallel tables line up.
    by_suffix: Dict[str, dict] = {}
    for full_oid, desc in descriptions.items():
        suffix = _oid_suffix(full_oid, oids.PRT_MARKER_SUPPLIES_DESCRIPTION)
        by_suffix[suffix] = {"description": desc}

    def _match(table: Dict[str, str], base: str):
        return {_oid_suffix(o, base): v for o, v in table.items()}

    type_by_suffix = _match(types, oids.PRT_MARKER_SUPPLIES_TYPE)
    max_by_suffix = _match(maxes, oids.PRT_MARKER_SUPPLIES_MAX_CAPACITY)
    level_by_suffix = _match(levels, oids.PRT_MARKER_SUPPLIES_LEVEL)

    supplies: List[dict] = []
    for suffix, entry in by_suffix.items():
        desc = entry["description"]
        raw_level = _to_int(level_by_suffix.get(suffix))
        max_cap = _to_int(max_by_suffix.get(suffix))
        type_code = _to_int(type_by_suffix.get(suffix))
        parsed = parse_supply_level(raw_level, max_cap)
        supplies.append(
            {
                "type": supply_type_from_code(type_code),
                "color": normalize_color(desc),
                "description": desc,
                "level_pct": parsed.level_pct,
                "current": raw_level if (raw_level is not None and raw_level >= 0) else None,
                "max_capacity": max_cap if (max_cap is not None and max_cap >= 0) else None,
            }
        )
    return supplies


def build_reading(
    ip: str,
    scalars: Dict[str, Optional[str]],
    supply_walks: Dict[str, Dict[str, str]],
    alert_walk: Optional[Dict[str, Dict[str, str]]] = None,
) -> dict:
    """Pure: turn fetched SNMP values into a central ``ReadingIn``-shaped dict."""
    sys_descr = scalars.get(oids.SYS_DESCR)
    printer_name = scalars.get(oids.PRT_GENERAL_PRINTER_NAME)
    device_descr = scalars.get(oids.HR_DEVICE_DESCR)
    model = printer_name or device_descr
    brand = _vendor_from(printer_name, device_descr, sys_descr)

    supplies = build_supplies(supply_walks)

    error_bits = parse_error_bits(scalars.get(oids.HR_PRINTER_DETECTED_ERROR_STATE))
    events: List[dict] = []
    has_critical = False
    for bit in error_bits:
        label = oids.ERROR_STATE_BITS.get(bit)
        if not label:
            continue
        critical = bit in oids.CRITICAL_ERROR_BITS
        has_critical = has_critical or critical
        events.append(
            {
                "code": label.replace(" ", "-"),
                "severity": "critical" if critical else "warning",
                "source": "snmp_alert",
                "message": label.capitalize(),
            }
        )

    printer_status = scalars.get(oids.HR_PRINTER_STATUS)
    if has_critical:
        status = "error"
    elif events:
        status = "warning"
    elif printer_status in _OK_PRINTER_STATUS:
        status = "ok"
    else:
        status = "unknown"

    return {
        "ip": ip,
        "status": status,
        "page_count": _to_int(scalars.get(oids.PRT_MARKER_LIFE_COUNT)),
        "hostname": scalars.get(oids.SYS_NAME),
        "brand": brand,
        "model": model,
        "serial": scalars.get(oids.PRT_GENERAL_SERIAL_NUMBER),
        "supplies": supplies,
        "events": events,
    }


_SCALAR_OIDS = [
    oids.SYS_NAME,
    oids.SYS_DESCR,
    oids.PRT_GENERAL_PRINTER_NAME,
    oids.PRT_GENERAL_SERIAL_NUMBER,
    oids.HR_DEVICE_DESCR,
    oids.PRT_MARKER_LIFE_COUNT,
    oids.HR_PRINTER_STATUS,
    oids.HR_PRINTER_DETECTED_ERROR_STATE,
]

_SUPPLY_BASES = [
    oids.PRT_MARKER_SUPPLIES_DESCRIPTION,
    oids.PRT_MARKER_SUPPLIES_TYPE,
    oids.PRT_MARKER_SUPPLIES_MAX_CAPACITY,
    oids.PRT_MARKER_SUPPLIES_LEVEL,
]


async def poll_printer(backend: SnmpBackend, ip: str, params: SnmpParams) -> dict:
    """Fetch all OIDs for one printer and return the reading payload.

    Raises SnmpError if the device doesn't answer the scalar GET.
    """
    scalars = await backend.get(ip, _SCALAR_OIDS, params)
    supply_walks: Dict[str, Dict[str, str]] = {}
    for base in _SUPPLY_BASES:
        try:
            supply_walks[base] = await backend.walk(ip, base, params)
        except SnmpError:
            supply_walks[base] = {}
    return build_reading(ip, scalars, supply_walks)
