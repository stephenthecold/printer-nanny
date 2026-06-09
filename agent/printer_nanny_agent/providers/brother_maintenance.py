"""Brother maintenance-blob provider -- the data path BRAdmin Professional uses.

Brother management tools (BRAdmin Pro, Brother iPrint&Scan, Home Assistant's
Brother integration, most Brother fleet tools) do NOT scrape the embedded web
server for supply levels. They read three binary blobs from Brother's private
MIB over plain SNMP -- the same firmware counters that render the EWS toner
gauge -- and decode typed records out of them:

    1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.8.0    "maintenance"  (remaining-life %)
    1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.10.0   "counters"     (page counters)
    1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.11.0   "nextcare"     (remaining pages)

Each blob is a packed sequence of records. On modern Brother devices
(roughly 2010+) a record is 7 bytes -> 14 hex chars:

    [ID: 1 byte][reserved: 2 bytes][value: 4 bytes big-endian]

Percent-typed records store value*100 (8200 == 82.00%). The ID table below
follows the community-documented mapping (cross-checked against the
Home Assistant `brother` library, which has years of fleet coverage):

    6f/70/71/72  toner remaining %, black/cyan/magenta/yellow
    a1/a2/a3/a4  toner remaining % (newer firmware variant of the same)
    41           drum remaining life %
    79/7a/7b/80  per-color drum remaining % (cyan/magenta/yellow/black)
    69           belt unit remaining %
    6a           fuser remaining %
    6b           laser unit remaining %
    6c           PF kit MP remaining %
    6d           PF kit 1 remaining %

This is read-only SNMP -- no settings writes, no extra TCP ports beyond 161,
and it does not depend on the web UI's HTML layout. It is therefore the
preferred Brother supply source; PJL and EWS scraping remain as fallbacks
for models whose firmware doesn't populate these blobs.

Registered FIRST among the Brother providers so:
* the bucket provider (brother.py) skips supplies that already carry a real
  percentage,
* the EWS scraper defers via the ``_maintenance_sourced`` flag,
* PJL may still overwrite -- both read the same firmware counters, so the
  values agree when PJL responds at all.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

from printer_nanny_agent.providers import PrinterProvider, register
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams

log = logging.getLogger("printer_nanny_agent.providers.brother_maintenance")

OID_MAINTENANCE = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.8.0"
OID_NEXTCARE = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.11.0"

# Record IDs in the maintenance blob -> (kind, type, color).
# kind "pct" records hold value*100 (8200 == 82%).
MAINTENANCE_IDS: Dict[str, Tuple[str, str, Optional[str]]] = {
    # Toner remaining (both ID generations; firmware emits one or the other)
    "6f": ("pct", "toner", "black"),
    "70": ("pct", "toner", "cyan"),
    "71": ("pct", "toner", "magenta"),
    "72": ("pct", "toner", "yellow"),
    "a1": ("pct", "toner", "black"),
    "a2": ("pct", "toner", "cyan"),
    "a3": ("pct", "toner", "magenta"),
    "a4": ("pct", "toner", "yellow"),
    # Drum remaining life
    "41": ("pct", "drum", None),
    "79": ("pct", "drum", "cyan"),
    "7a": ("pct", "drum", "magenta"),
    "7b": ("pct", "drum", "yellow"),
    "80": ("pct", "drum", "black"),
    # Long-life maintenance parts
    "69": ("pct", "belt", None),
    "6a": ("pct", "fuser", None),
    "6b": ("pct", "laser", None),
    "6c": ("pct", "pf_kit_mp", None),
    "6d": ("pct", "pf_kit_1", None),
}

# Nextcare blob: remaining PAGES until each part needs service.
NEXTCARE_IDS: Dict[str, str] = {
    "82": "drum",
    "88": "belt",
    "89": "fuser",
    "73": "laser",
    "86": "pf_kit_mp",
    "77": "pf_kit_1",
    "a4": "drum_black",
    "a5": "drum_cyan",
    "a6": "drum_magenta",
    "a7": "drum_yellow",
}

# Parts that aren't in the standard prtMarkerSupplies table on most models;
# we add them as supply rows so the dashboard can show maintenance-kit life.
# Maps part key -> (SupplyType value, color-slug, display description).
# The color slug matters: central upserts supplies keyed on (type, color), so
# the three "other"-typed parts need distinct colors or they'd overwrite each
# other in the supplies table.
_EXTRA_PART_ROWS = {
    "belt": ("other", "belt", "Belt Unit"),
    "fuser": ("fuser", None, "Fuser Unit"),
    "laser": ("other", "laser", "Laser Unit"),
    "pf_kit_mp": ("other", "pf-kit-mp", "PF Kit MP"),
    "pf_kit_1": ("other", "pf-kit-1", "PF Kit 1"),
}

_RECORD_HEX_LEN = 14  # 7 bytes: 1 ID + 2 reserved + 4 value (big-endian)
_NOT_APPLICABLE = 0xFFFFFFFF  # firmware's "this part doesn't exist" sentinel


def _normalize_blob(raw: Optional[str]) -> str:
    """Reduce a pysnmp-rendered octet string to a bare lowercase hex string.

    pysnmp's prettyPrint renders binary octets as ``0x6f01040000...``; some
    transports introduce whitespace or colons. Anything that doesn't survive
    as pure hex is rejected (returns "")."""
    if not raw:
        return ""
    text = raw.strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    text = text.replace(" ", "").replace(":", "").replace("\n", "")
    if not text or any(c not in "0123456789abcdef" for c in text):
        return ""
    return text


def decode_maintenance(raw: Optional[str]) -> Dict[str, float]:
    """Decode the maintenance blob -> {part_key: percent_remaining}.

    part_key is "toner:black", "drum", "drum:cyan", "belt", "fuser", ...
    Unknown record IDs are collected under the special key "_unknown" as a
    comma-joined id list (diagnostics; lets us extend the table from real
    fleet hardware instead of guessing).
    """
    blob = _normalize_blob(raw)
    out: Dict[str, float] = {}
    unknown: list[str] = []
    for i in range(0, len(blob) - _RECORD_HEX_LEN + 1, _RECORD_HEX_LEN):
        rec = blob[i:i + _RECORD_HEX_LEN]
        rec_id = rec[:2]
        try:
            value = int(rec[-8:], 16)
        except ValueError:
            continue
        spec = MAINTENANCE_IDS.get(rec_id)
        if spec is None:
            if rec_id not in unknown:
                unknown.append(rec_id)
            continue
        kind, part, color = spec
        if kind != "pct":
            continue
        if value == _NOT_APPLICABLE:
            continue  # firmware says this part doesn't exist on this model
        pct = value / 100.0
        if not (0.0 <= pct <= 100.0):
            continue  # garbage / unsupported encoding; never ship a bad %
        key = f"{part}:{color}" if color else part
        out[key] = pct
    if unknown:
        out["_unknown"] = ",".join(unknown)  # type: ignore[assignment]
    return out


def decode_nextcare(raw: Optional[str]) -> Dict[str, int]:
    """Decode the nextcare blob -> {part_key: remaining_pages}."""
    blob = _normalize_blob(raw)
    out: Dict[str, int] = {}
    for i in range(0, len(blob) - _RECORD_HEX_LEN + 1, _RECORD_HEX_LEN):
        rec = blob[i:i + _RECORD_HEX_LEN]
        part = NEXTCARE_IDS.get(rec[:2])
        if part is None:
            continue
        try:
            value = int(rec[-8:], 16)
        except ValueError:
            continue
        if value == _NOT_APPLICABLE or value > 10_000_000:
            continue
        out[part] = value
    return out


class BrotherMaintenanceProvider(PrinterProvider):
    """Exact supply percentages from the Brother maintenance blob (SNMP-only)."""

    name = "brother_maintenance"
    enterprise_prefixes = ("2435",)

    async def augment(
        self,
        backend: SnmpBackend,
        ip: str,
        params: SnmpParams,
        reading: dict,
        sys_object_id: Optional[str],
    ) -> dict:
        try:
            blobs = await backend.get(ip, [OID_MAINTENANCE, OID_NEXTCARE], params)
        except SnmpError as exc:
            log.debug("maintenance blob get failed for %s: %s", ip, exc)
            reading["_brother_maintenance"] = "snmp-error"
            return reading

        levels = decode_maintenance(blobs.get(OID_MAINTENANCE))
        unknown_ids = levels.pop("_unknown", None)
        pages = decode_nextcare(blobs.get(OID_NEXTCARE))

        if not levels:
            # Blob absent or in a format we don't decode (legacy 10-char
            # record inkjets, ~pre-2010). PJL / EWS fallbacks still run.
            reading["_brother_maintenance"] = "no-data"
            if unknown_ids:
                reading["_brother_maintenance"] += f" unknown_ids={unknown_ids}"
            return reading

        applied: list[str] = []

        # --- Fill existing toner / drum supply rows by (type, color) ---
        for supply in reading.get("supplies", []):
            stype = supply.get("type")
            color = supply.get("color")
            if stype == "toner":
                key = f"toner:{color}" if color else None
                # Mono printers may report toner without a color in the
                # maintenance blob keyspace -- their single toner is black.
                pct = levels.get(key) if key else None
                if pct is None and color == "black":
                    pct = levels.get("toner:black")
            elif stype == "drum":
                pct = levels.get(f"drum:{color}") if color else None
                if pct is None:
                    pct = levels.get("drum")
            else:
                continue
            if pct is None:
                continue
            old = supply.get("level_pct")
            # Always prefer the maintenance % over a bucket estimate; respect
            # a standard-MIB value only when it already agrees (some models
            # report real percentages in prtMarkerSupplies too).
            if old is not None and not supply.get("_brother_estimated") and abs(old - pct) <= 5:
                supply["_maintenance_sourced"] = True  # protect from EWS anyway
                continue
            supply["level_pct"] = pct
            supply["status_note"] = None
            supply["_brother_estimated"] = False
            supply["_maintenance_sourced"] = True
            applied.append(f"{color or stype}={pct:.0f}%")

        # --- Add rows for long-life parts the standard MIB doesn't list ---
        existing_descrs = {
            (s.get("description") or "").lower() for s in reading.get("supplies", [])
        }
        for part, (stype, color_slug, descr) in _EXTRA_PART_ROWS.items():
            pct = levels.get(part)
            if pct is None or descr.lower() in existing_descrs:
                continue
            row = {
                "type": stype,
                "color": color_slug,
                "description": descr,
                "level_pct": pct,
                "status_note": None,
                "_maintenance_sourced": True,
            }
            remaining = pages.get(part)
            if remaining is not None:
                row["status_note"] = f"~{remaining:,} pages left"
            reading.setdefault("supplies", []).append(row)
            applied.append(f"{descr}={pct:.0f}%")

        # Drum pages-remaining enrich (row already exists from standard MIB).
        drum_pages = pages.get("drum")
        if drum_pages is not None:
            for supply in reading.get("supplies", []):
                if supply.get("type") == "drum" and not supply.get("status_note"):
                    supply["status_note"] = f"~{drum_pages:,} pages left"

        reading["_supply_precision"] = "brother_maintenance"
        summary = "decoded " + ",".join(sorted(levels)) if levels else "no-data"
        if unknown_ids:
            summary += f" unknown_ids={unknown_ids}"
        reading["_brother_maintenance"] = summary
        if applied:
            log.info("maintenance blob supplies for %s: %s", ip, ", ".join(applied))
        return reading


register(BrotherMaintenanceProvider())
