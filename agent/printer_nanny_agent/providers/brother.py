"""Brother provider.

Brother lasers (MFC, HL, DCP families) do not have a continuous toner fill
sensor; their firmware only tracks OK / Low / Empty buckets. The standard
Printer-MIB therefore reports `prtMarkerSuppliesLevel = -3` ("some remaining")
for every toner -- correct, but useless to an operator who wants to know
when to order. Brother's private MIB exposes the same fact more usefully:
the current active alert is at `1.3.6.1.4.1.2435.2.3.9.4.2.1.5.4.5.2.0` as
plain text like ``'Toner Low (BK)'`` / ``'Toner Empty (C)'``.

This provider:

* Reads the current active alert and, if it names a toner color, upgrades
  that supply's status_note from the generic "some remaining" to "low" or
  "empty" so the dashboard can colour-code it.
* Walks `brInfoMaintenance.51` (the last-10-alerts history table) and adds
  the entries to the reading's `events` list as info-level events tagged
  with the page count at which each occurred.

Real numeric percentages on Brother require EWS HTML scraping (a future
provider; the gauge HTML at /general/status.html varies by firmware and
needs per-model testing). Documented in central/snmp.md.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

from printer_nanny_agent.providers import PrinterProvider, register
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
        # --- Current alert -> toner status_note enrichment ---
        try:
            ident = await backend.get(ip, [OID_ACTIVE_ALERT_TEXT + ".0"], params)
            alert_text = ident.get(OID_ACTIVE_ALERT_TEXT + ".0")
        except SnmpError:
            alert_text = None

        # --- Recent alerts table -> events + fallback alert source ---
        # Walk first so we can fall back to the history when the live active
        # alert is idle ("Sleep" / "Ready" / etc.). Without this fallback, a
        # printer that's "asleep but the cartridge is empty" reports the sleep
        # state and we miss the supply condition entirely.
        try:
            history_index = await backend.walk(ip, OID_ALERT_HISTORY_INDEX, params)
            history_descr = await backend.walk(ip, OID_ALERT_HISTORY_DESCR, params)
            history_pages = await backend.walk(ip, OID_ALERT_HISTORY_PAGES, params)
        except SnmpError:
            history_index = history_descr = history_pages = {}
        idx_map = _walk_table(history_index, OID_ALERT_HISTORY_INDEX)
        desc_map = _walk_table(history_descr, OID_ALERT_HISTORY_DESCR)
        page_map = _walk_table(history_pages, OID_ALERT_HISTORY_PAGES)

        # If the live active alert is missing or doesn't carry a supply state
        # (Sleep / Ready / Power Save / a jam / etc.), walk back through the
        # alert history and use the MOST RECENT entry that actually describes
        # a supply condition. The history is index-ordered by insertion, so
        # the last entry with a parseable severity wins.
        #
        # IMPORTANT: skip history entries with page_count=0. On several Brother
        # models (HL-L2460DW class) the alert history table carries stale
        # placeholder rows from initial factory/setup whose page column is 0
        # even though the printer is now well into its life cycle. Those rows
        # often hold a literal "No Toner" string that has nothing to do with
        # the current cartridge state -- using them flips a nearly-full toner
        # to 0% and triggers a false alarm.
        parse_source = "live"
        parsed = _parse_alert(alert_text)
        if not parsed[0]:
            # Sort suffixes high-to-low so we see the newest entries first.
            for suffix in sorted(
                idx_map,
                key=lambda s: int(s) if s.isdigit() else 0,
                reverse=True,
            ):
                desc = (desc_map.get(suffix) or "").strip()
                if not desc:
                    continue
                # Skip stale page=0 placeholder rows. A real supply alert always
                # carries the page count at which it occurred; a 0 means we
                # can't trust the entry to represent the current state.
                try:
                    page_at = int(page_map.get(suffix, "0"))
                except (TypeError, ValueError):
                    page_at = 0
                if page_at <= 0:
                    continue
                hist_parsed = _parse_alert(desc)
                if hist_parsed[0]:
                    parsed = hist_parsed
                    alert_text = f"history: {desc} @page {page_at}"
                    parse_source = "history"
                    break

        severity, color = parsed
        if severity:
            toner_supplies = [
                s for s in reading.get("supplies", []) if s.get("type") == "toner"
            ]
            # When the alert omits a color code, default to black -- this is
            # how mono printers (HL-L2370DW etc.) phrase their alerts
            # ("No Toner", "Replace Toner") since they have only one supply.
            # For multi-toner color devices the regex normally matches the
            # color, but the absent-color fallback still works as long as
            # there's a single black toner to attach to.
            if color is None and toner_supplies:
                if any(s.get("color") == "black" for s in toner_supplies):
                    color = "black"
                elif len(toner_supplies) == 1:
                    color = toner_supplies[0].get("color")
            for supply in toner_supplies:
                if supply.get("color") != color:
                    continue
                # Only override when standard MIB had no real percentage.
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

        # Diagnostic breadcrumb -- visible in the Provider diagnostics card.
        # Shows what the live alert OID returned and whether we ended up
        # using history fallback. Without this, "no changes" is opaque.
        diag_alert = (alert_text or "(empty)").replace("history: ", "history=")
        reading["_brother_active_alert"] = diag_alert
        reading["_brother_alert_source"] = parse_source
        reading["_brother_parsed_severity"] = severity or "none"

        # Flag the reading so the UI knows to render the "buckets only" tooltip.
        reading["_supply_precision"] = "brother_buckets"
        return reading


register(BrotherProvider())
