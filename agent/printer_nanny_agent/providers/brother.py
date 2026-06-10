"""Brother provider -- consolidated supply pipeline for every Brother device.

One registered provider, one Provider-diagnostics row per poll. Internally it
runs up to four passes, stopping the expensive ones as soon as real numbers
exist (fewer network round-trips per poll on healthy fleets):

  1. maintenance blob (brother_maintenance) -- the BRAdmin data path: exact
     percentages for toner/drum/belt/fuser/laser/PF kits over read-only SNMP.
     Works on every modern Brother (~2010+). When this succeeds, PJL and EWS
     are skipped entirely.
  2. status pass (this module) -- live active-alert text -> bucket hints
     ("low"/"empty") for any toner still without a percentage, plus the
     alert-history walk -> info events. Always runs (events are wanted even
     when percentages are exact).
  3. PJL over TCP/9100 (brother_pjl) -- only when a toner still lacks a real
     percentage (legacy firmware without the maintenance blob).
  4. EWS HTML scrape (brother_ews) -- last resort for the same gap.

The sub-modules keep their classes and parsers (unit-tested in isolation)
but no longer self-register; this umbrella is the only Brother entry in the
provider registry.

Diagnostic breadcrumbs (rendered in the Provider diagnostics card):
  maintenance=...   what the blob decode found (or no-data / snmp-error)
  alert=...         the live active-alert text
  parsed=...        severity extracted from the alert (low/empty/none)
  source=...        which channel supplied the percentages
                    (maintenance | pjl | ews | buckets | none)
"""

from __future__ import annotations

import re
from typing import Dict, Optional

from printer_nanny_agent.providers import PrinterProvider, register
from printer_nanny_agent.providers.brother_ews import BrotherEwsProvider
from printer_nanny_agent.providers.brother_maintenance import BrotherMaintenanceProvider
from printer_nanny_agent.providers.brother_pjl import BrotherPjlProvider
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams

# Active alert: human-readable text scalar (we read .0 as an instance).
OID_ACTIVE_ALERT_TEXT = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.2"
# Alert history table base. Subtree layout (per the L8900CDW dump):
#   .51.1.0          = count of alerts in the table
#   .51.2.1.1.<n>    = alert index 1..10
#   .51.2.1.2.<n>    = alert description string
#   .51.2.1.3.<n>    = page count when alert occurred
OID_ALERT_HISTORY_COUNT = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.51.1"
OID_ALERT_HISTORY_INDEX = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.51.2.1.1"
OID_ALERT_HISTORY_DESCR = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.51.2.1.2"
OID_ALERT_HISTORY_PAGES = "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.51.2.1.3"

# Brother color code -> our normalized color name.
_COLOR_CODES = {
    "BK": "black",
    "K": "black",
    "C": "cyan",
    "M": "magenta",
    "Y": "yellow",
}

# Alert keyword -> (status_note, severity hint). Status_note ends up shown
# next to the (empty) progress bar on the printer detail page.
_TONER_SEVERITY = {
    "empty":   "empty",
    "out":     "empty",
    "no":      "empty",   # "No Toner" (HL-L2370DW mono lasers)
    "replace": "empty",   # "Replace Toner" / "Replace Cartridge"
    "depleted": "empty",
    "low":     "low",
    "near":    "low",     # "Near end of life" etc.
}

# Match a Brother alert string in one of these forms:
#   "Toner Low (BK)"   -> low / black     (color in parens, modern color lasers)
#   "Toner Empty Y"    -> empty / yellow  (trailing color code)
#   "Replace Toner"    -> empty / -       (no color: defaults to black for mono)
#   "No Toner"         -> empty / -       (HL-L2370DW etc.)
# The color group is OPTIONAL so mono-printer alerts that omit the color still
# match; the caller defaults a missing color to "black" when there's exactly
# one toner on the printer.
_ALERT_RE = re.compile(
    r"""(?P<sev>empty|out|no|replace|depleted|low|near)\b
        (?:.*?(?:\((?P<color1>BK|K|C|M|Y)\)|(?P<color2>BK|K|C|M|Y)\s*$))?""",
    re.IGNORECASE | re.VERBOSE,
)


def _walk_table(rows: Dict[str, str], base_oid: str) -> Dict[str, str]:
    """Strip the base OID prefix from each row's key, returning {index: value}."""
    out: Dict[str, str] = {}
    base = base_oid.rstrip(".") + "."
    for full_oid, value in rows.items():
        if full_oid.startswith(base):
            out[full_oid[len(base):]] = value
    return out


def _parse_alert(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """('low'|'empty'|None, color_name|None) from a Brother alert string."""
    if not text:
        return None, None
    m = _ALERT_RE.search(text)
    if not m:
        return None, None
    severity = _TONER_SEVERITY.get(m.group("sev").lower())
    code = (m.group("color1") or m.group("color2") or "").upper()
    color = _COLOR_CODES.get(code)
    return severity, color


def _toner_gaps(reading: dict) -> bool:
    """True when any toner still lacks a REAL percentage (none, or only a
    bucket estimate). Drives whether the PJL / EWS fallbacks are worth the
    network round-trip."""
    return any(
        s.get("type") == "toner"
        and (s.get("level_pct") is None or s.get("_brother_estimated"))
        for s in reading.get("supplies", [])
    )


# Module-level pass instances. Each sub-provider keeps its own parsing and
# error handling; the umbrella only sequences them.
_MAINTENANCE = BrotherMaintenanceProvider()
_PJL = BrotherPjlProvider()
_EWS = BrotherEwsProvider()


# Patchable seams: tests replace these to keep CI off the network (PJL opens
# TCP/9100, EWS does HTTP GETs). Production code never touches them.
async def _pjl_step(backend, ip, params, reading, sys_object_id) -> dict:
    return await _PJL.augment(backend, ip, params, reading, sys_object_id)


async def _ews_step(backend, ip, params, reading, sys_object_id) -> dict:
    return await _EWS.augment(backend, ip, params, reading, sys_object_id)


class BrotherProvider(PrinterProvider):
    name = "brother"
    enterprise_prefixes = ("2435",)

    async def augment(
        self,
        backend: SnmpBackend,
        ip: str,
        params: SnmpParams,
        reading: dict,
        sys_object_id: Optional[str],
    ) -> dict:
        # --- Pass 1: maintenance blob (exact percentages, read-only SNMP) ---
        reading = await _MAINTENANCE.augment(backend, ip, params, reading, sys_object_id)

        # --- Pass 2: live alert + history events (always) ---
        reading = await self._status_pass(backend, ip, params, reading)

        # --- Pass 3 + 4: legacy fallbacks, only when a toner still has no
        # real percentage. On modern Brothers the maintenance blob already
        # answered and these are skipped -- no 9100 connection, no HTTP. ---
        if _toner_gaps(reading):
            reading = await _pjl_step(backend, ip, params, reading, sys_object_id)
        if _toner_gaps(reading):
            reading = await _ews_step(backend, ip, params, reading, sys_object_id)

        # --- Which channel ended up supplying the percentages? ---
        supplies = reading.get("supplies", [])
        if any(s.get("_maintenance_sourced") for s in supplies):
            source = "maintenance"
        elif any(s.get("_pjl_sourced") for s in supplies):
            source = "pjl"
        elif any(s.get("_ews_sourced") for s in supplies):
            source = "ews"
        elif any(s.get("_brother_estimated") for s in supplies):
            source = "buckets"
        else:
            source = "none"
        reading["_brother_source"] = source
        return reading

    async def _status_pass(
        self, backend: SnmpBackend, ip: str, params: SnmpParams, reading: dict
    ) -> dict:
        """Live active-alert text -> bucket hints; alert history -> events.

        The history is strictly a log: it never feeds current supply state
        (Brother models keep stale 'No Toner @page 0' placeholders forever,
        and a months-old 'Toner Low' says nothing about today's cartridge).
        """
        try:
            ident = await backend.get(ip, [OID_ACTIVE_ALERT_TEXT + ".0"], params)
            alert_text = ident.get(OID_ACTIVE_ALERT_TEXT + ".0")
        except SnmpError:
            alert_text = None

        try:
            history_index = await backend.walk(ip, OID_ALERT_HISTORY_INDEX, params)
            history_descr = await backend.walk(ip, OID_ALERT_HISTORY_DESCR, params)
            history_pages = await backend.walk(ip, OID_ALERT_HISTORY_PAGES, params)
        except SnmpError:
            history_index = history_descr = history_pages = {}
        idx_map = _walk_table(history_index, OID_ALERT_HISTORY_INDEX)
        desc_map = _walk_table(history_descr, OID_ALERT_HISTORY_DESCR)
        page_map = _walk_table(history_pages, OID_ALERT_HISTORY_PAGES)

        severity, color = _parse_alert(alert_text)
        if severity:
            toner_supplies = [
                s for s in reading.get("supplies", []) if s.get("type") == "toner"
            ]
            # When the alert omits a color code, default to black -- this is
            # how mono printers (HL-L2370DW etc.) phrase their alerts
            # ("No Toner", "Replace Toner") since they have only one supply.
            if color is None and toner_supplies:
                if any(s.get("color") == "black" for s in toner_supplies):
                    color = "black"
                elif len(toner_supplies) == 1:
                    color = toner_supplies[0].get("color")
            for supply in toner_supplies:
                if supply.get("color") != color:
                    continue
                # Only seed a bucket hint when no channel produced a real %.
                if supply.get("level_pct") is None:
                    supply["status_note"] = severity  # "low" / "empty"
                    # Numeric hint so the bar can colour itself. Brother's
                    # "Low" threshold maps roughly to ~15% on most lasers
                    # (cartridge keeps printing for ~500 more pages after).
                    # "Empty" is reported when the printer refuses to print.
                    supply["level_pct"] = 15.0 if severity == "low" else 0.0
                    supply["_brother_estimated"] = True

        # Append the alert-history walk results as info events.
        for suffix in sorted(idx_map, key=lambda s: int(s) if s.isdigit() else 0):
            desc = desc_map.get(suffix)
            if not desc or not desc.strip():
                continue
            try:
                pages = int(page_map.get(suffix, "0"))
            except (TypeError, ValueError):
                pages = 0
            reading.setdefault("events", []).append(
                {
                    "code": "brother-history",
                    "severity": "info",
                    "source": "snmp_alert",
                    "message": f"{desc.strip()} (page {pages:,})",
                }
            )

        # Diagnostic breadcrumbs -- visible in the Provider diagnostics card.
        reading["_brother_active_alert"] = alert_text or "(empty)"
        reading["_brother_parsed_severity"] = severity or "none"

        # setdefault: never downgrade the tag when the maintenance pass
        # already established real percentages ("brother_maintenance").
        reading.setdefault("_supply_precision", "brother_buckets")
        return reading


register(BrotherProvider())
