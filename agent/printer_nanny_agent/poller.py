"""Poll one printer over SNMP and assemble the reading payload the central
ingest API accepts. Parsing is split into pure helpers so it can be unit-tested
without any SNMP I/O.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from printer_nanny_agent import oids
from printer_nanny_agent.snmp_parse import (
    normalize_color,
    parse_supply_level,
    supply_type_from_code,
)
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams
# Import providers package for its registry side-effect (Brother registers itself).
from printer_nanny_agent.providers import run_providers

# Vendor keywords matched (case-insensitively) in sysDescr / printer name.
_VENDORS = [
    "HP", "Hewlett", "Brother", "Canon", "Xerox", "Lexmark", "Konica",
    "Ricoh", "Kyocera", "Epson", "Samsung", "Dell", "OKI", "Sharp", "Toshiba",
]


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


# Common firmware/version markers embedded in sysDescr by many vendors, e.g.
#   "HP ETHERNET MULTI-ENVIRONMENT,SN:..., FW:20230815"
#   "Brother NC-... ,Firmware Ver.1.34 ,..."
#   "KYOCERA ... Version 2S5_2000.002.052 ..."
# We extract the token following the marker; honest None when nothing matches so
# the posture view reports "unknown" rather than a misleading guess.
_FIRMWARE_PATTERNS = [
    re.compile(r"firmware\s*ver(?:sion)?\.?\s*[:=]?\s*([A-Za-z0-9][\w.\-/]*)", re.I),
    re.compile(r"\bfw\s*[:=]\s*([A-Za-z0-9][\w.\-/]*)", re.I),
    re.compile(r"\bversion\s*[:=]?\s*([0-9][\w.\-/]*)", re.I),
]


def parse_firmware(*texts: Optional[str]) -> Optional[str]:
    """Best-effort firmware/version string from sysDescr-style text.

    Pure + dependency-free so it unit-tests without SNMP. Returns the matched
    version token (trimmed) or None when no recognizable marker is present.
    """
    for text in texts:
        if not text:
            continue
        for pattern in _FIRMWARE_PATTERNS:
            match = pattern.search(text)
            if match:
                token = match.group(1).strip(" ,;")
                if token:
                    return token[:200]
    return None


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

    def _match(base: str) -> Dict[str, str]:
        return {_oid_suffix(o, base): v for o, v in walks.get(base, {}).items()}

    desc_by_suffix = _match(oids.PRT_MARKER_SUPPLIES_DESCRIPTION)
    type_by_suffix = _match(oids.PRT_MARKER_SUPPLIES_TYPE)
    max_by_suffix = _match(oids.PRT_MARKER_SUPPLIES_MAX_CAPACITY)
    level_by_suffix = _match(oids.PRT_MARKER_SUPPLIES_LEVEL)

    # Union of every index seen in any table -- some devices report a level row
    # without a matching description (don't silently drop those supplies).
    suffixes = set(desc_by_suffix) | set(type_by_suffix) | set(max_by_suffix) | set(level_by_suffix)

    supplies: List[dict] = []
    for suffix in sorted(suffixes):
        desc = desc_by_suffix.get(suffix)
        raw_level = _to_int(level_by_suffix.get(suffix))
        max_cap = _to_int(max_by_suffix.get(suffix))
        type_code = _to_int(type_by_suffix.get(suffix))
        parsed = parse_supply_level(raw_level, max_cap)
        # When the device reports a sentinel instead of a number (e.g. Brother
        # toner = "some remaining"), surface that coarse state as a note.
        note = parsed.note if parsed.level_pct is None else None
        supplies.append(
            {
                "type": supply_type_from_code(type_code),
                "color": normalize_color(desc),
                "description": desc,
                "level_pct": parsed.level_pct,
                "status_note": note,
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
    # Firmware is best-effort from sysDescr (where most vendors embed it). Left
    # None when nothing parseable -- the central posture report surfaces that
    # honestly as "unknown".
    firmware = parse_firmware(sys_descr, device_descr, printer_name)

    supplies = build_supplies(supply_walks)

    # Two complementary sources of error info:
    # (a) hrPrinterDetectedErrorState bits -- coarse buckets ("service requested")
    # (b) prtAlertTable -- vendor's own message ("Replace fuser. ~5000 pages left.")
    # Walk (b) and use those messages when available; the bits alone are too
    # vague to tell an operator what's actually wrong.
    events: List[dict] = []
    has_critical = has_warning = False

    alert_descriptions = (alert_walk or {}).get(oids.PRT_ALERT_DESCRIPTION, {})
    alert_severities = (alert_walk or {}).get(oids.PRT_ALERT_SEVERITY_LEVEL, {})
    for full_oid, message in alert_descriptions.items():
        if not message:
            continue
        suffix = _oid_suffix(full_oid, oids.PRT_ALERT_DESCRIPTION)
        sev_code = _to_int(alert_severities.get(oids.PRT_ALERT_SEVERITY_LEVEL + suffix))
        # RFC 3805 prtAlertSeverityLevel: 3=critical, 4=warning(unary), 5=warning(binary).
        if sev_code == 3:
            severity, has_critical = "critical", True
        elif sev_code in (4, 5):
            severity, has_warning = "warning", True
        else:
            severity = "info"
        events.append(
            {
                "code": "prt-alert" + suffix.replace(".", "-"),
                "severity": severity,
                "source": "snmp_alert",
                "message": message.strip(),
            }
        )

    error_bits = parse_error_bits(scalars.get(oids.HR_PRINTER_DETECTED_ERROR_STATE))
    for bit in error_bits:
        label = oids.ERROR_STATE_BITS.get(bit)
        if not label:
            continue
        # Skip critical bits already covered by a richer prtAlertTable message --
        # avoids "Service requested" sitting next to "Replace fuser" saying the
        # same thing in different words.
        if alert_descriptions and bit in oids.CRITICAL_ERROR_BITS:
            continue
        if bit in oids.CRITICAL_ERROR_BITS:
            severity, has_critical = "critical", True
        elif bit in oids.INFO_ERROR_BITS:
            severity = "info"  # e.g. power-save "offline" -- recorded, not alarmed
        else:
            severity, has_warning = "warning", True
        events.append(
            {
                "code": label.replace(" ", "-"),
                "severity": severity,
                "source": "snmp_alert",
                "message": label.capitalize(),
            }
        )

    # We successfully polled the device, so it is reachable: a clean read is "ok"
    # regardless of hrPrinterStatus (which some printers report as other/unknown
    # while idle). Only real error/warning bits change that.
    if has_critical:
        status = "error"
    elif has_warning:
        status = "warning"
    else:
        status = "ok"

    return {
        "ip": ip,
        "status": status,
        "page_count": _to_int(scalars.get(oids.PRT_MARKER_LIFE_COUNT)),
        "hostname": scalars.get(oids.SYS_NAME),
        "brand": brand,
        "model": model,
        "serial": scalars.get(oids.PRT_GENERAL_SERIAL_NUMBER),
        "firmware": firmware,
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

_ALERT_BASES = [
    oids.PRT_ALERT_SEVERITY_LEVEL,
    oids.PRT_ALERT_DESCRIPTION,
]


async def poll_printer(backend: SnmpBackend, ip: str, params: SnmpParams) -> dict:
    """Fetch all OIDs for one printer and return the reading payload.

    Raises SnmpError if the device doesn't answer the scalar GET.
    """
    # sysObjectID is fetched alongside the standard scalars so vendor providers
    # can detect by enterprise number without a second GET round-trip.
    scalars = await backend.get(ip, _SCALAR_OIDS + [oids.SYS_OBJECT_ID], params)
    supply_walks: Dict[str, Dict[str, str]] = {}
    for base in _SUPPLY_BASES:
        try:
            supply_walks[base] = await backend.walk(ip, base, params)
        except SnmpError:
            supply_walks[base] = {}
    alert_walks: Dict[str, Dict[str, str]] = {}
    for base in _ALERT_BASES:
        try:
            alert_walks[base] = await backend.walk(ip, base, params)
        except SnmpError:
            alert_walks[base] = {}
    reading = build_reading(ip, scalars, supply_walks, alert_walk=alert_walks)
    return await run_providers(backend, ip, params, reading, scalars.get(oids.SYS_OBJECT_ID))
