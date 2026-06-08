"""Brother EWS (embedded web server) scraper - vendor-estimated toner percentages.

Brother color lasers don't have continuous toner fill sensors (see
brother.py), so SNMP can only report OK / Low / Empty buckets. The web UI at
http://<ip>/general/status.html DOES show a percentage gauge -- it's still
an estimate, but a finer-grained one based on page-count consumption models
the firmware tracks but doesn't expose over SNMP. This provider scrapes the
EWS page when SNMP didn't return a real percentage.

Per the project design doc: HTTP scraping is fallback metadata only, never
the primary source of truth. The BrotherProvider (SNMP-only) runs first
and sets bucket-state status_notes; this provider runs after and overrides
the seeded UI hints with actual numeric percentages when scraping succeeds.
If scraping fails for ANY reason (HTTP timeout, 401, unsupported firmware
HTML layout, ...) the provider silently degrades to leave the SNMP buckets
in place -- there is no operator-visible failure mode.

Four HTML layout patterns are matched, covering ~2014-2026 Brother lasers:

  A) JavaScript array: `TonerInfo[i] = "85";` (some modern color lasers)
  B) Table rows with text percentages: `<td>Cyan</td><td>85%</td>`
  C) Inline gauge img names: `/img/Toner_C_85.gif` / `Toner_Y75.png`
  D) `tonerremain` gauge images where height="N" IS the value, no displayed
     percentage anywhere. This is what the MFC-L8900CDW (and other 2016+
     business color lasers) actually do -- the height attribute on the gauge
     img is the visible bar height; max is 11px for this model family. Less
     precise than A/B/C, but it's the only signal these models give.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Optional

import httpx

from printer_nanny_agent.providers import PrinterProvider, register
from printer_nanny_agent.snmp import SnmpBackend, SnmpParams

log = logging.getLogger("printer_nanny_agent.providers.brother_ews")

EWS_PATHS = ("/general/status.html", "/etc/mnt_info.html", "/")
HTTP_TIMEOUT_SECONDS = 5.0
_COLOR_CODES = {
    "BK": "black", "K": "black", "BLACK": "black",
    "C": "cyan", "CYAN": "cyan",
    "M": "magenta", "MAGENTA": "magenta",
    "Y": "yellow", "YELLOW": "yellow",
}

# Pattern A: JavaScript array. Brother color lasers commonly emit one of:
#   TonerInfo[0] = "85";   (older Web BRAdmin firmware)
#   tonerColor[0]= 85;
#   var Toner = new Array("85","70","60","50");
# Index order is conventionally K, C, M, Y on Brother color lasers.
_RE_JS_ARRAY = re.compile(
    r"""(?:tonerinfo|tonercolor|toner)
        \s*\[\s*(\d+)\s*\]
        \s*=\s*['"]?(\d{1,3})['"]?""",
    re.IGNORECASE | re.VERBOSE,
)
_RE_JS_NEW_ARRAY = re.compile(
    r"""new\s+array\s*\(
        \s*['"]?(\d{1,3})['"]?\s*,
        \s*['"]?(\d{1,3})['"]?\s*,
        \s*['"]?(\d{1,3})['"]?\s*,
        \s*['"]?(\d{1,3})['"]?""",
    re.IGNORECASE | re.VERBOSE,
)

# Pattern B: split out <tr>...</tr> rows first then find the color + % in each
# (a single regex that spans multiple <td>s with .* across rows would gobble
# across row boundaries and mis-pair columns).
_RE_TABLE_ROW_BLOCK = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_RE_ROW_COLOR = re.compile(
    r"<t[dh][^>]*>\s*(black|cyan|magenta|yellow|bk|c|m|y)"
    r"(?:\s*toner|\s*cartridge)?\s*</t[dh]>",
    re.IGNORECASE,
)
_RE_ROW_PCT = re.compile(r"(\d{1,3})\s*%")

# Pattern C: image filenames that encode the percent: Toner_C_85.gif /
# TonerY75.png / btn_K_100.svg
_RE_IMG_FILENAME = re.compile(
    r"""toner[_\-]?
        (?P<color>BK|K|C|M|Y)
        [_\-]?(?P<pct>\d{1,3})
        \.(?:gif|png|jpg|svg)""",
    re.IGNORECASE | re.VERBOSE,
)

# Pattern D: gauge image where height="N" IS the toner-remaining bar height
# (no displayed percentage). MFC-L8900CDW class. Attribute order is highly
# variable across firmware revs -- match either order of class/alt/height.
_RE_TONERREMAIN_IMG = re.compile(
    r"""<img\b[^>]*\bclass\s*=\s*["'][^"']*\btonerremain\b[^"']*["'][^>]*>""",
    re.IGNORECASE,
)
_RE_TONERREMAIN_ATTRS = re.compile(
    r"""(?:\balt\s*=\s*["'](?P<alt>[^"']+)["']) |
        (?:\bheight\s*=\s*["'](?P<height>\d+)["'])""",
    re.IGNORECASE | re.VERBOSE,
)
# Empirically the gauge max on the L8900CDW family is 11px ("full" gauges
# all show height=11). Configurable if a future model is found to use a
# different max -- right now this is the only Brother EWS that uses the
# tonerremain gauge pattern, so a single constant is correct.
_TONERREMAIN_MAX_PX = 11


def _parse_toner_percentages(html: str) -> Dict[str, int]:
    """Best-effort extraction of {color_name: percent} from a Brother EWS page.

    Tries each known pattern; the first one that returns sensible values
    (1+ colors, all in 0..100) wins. Returns an empty dict when no pattern
    matches -- that's the signal for the caller to leave SNMP data alone.
    """
    if not html:
        return {}

    # --- Pattern A: JavaScript array ---
    # Indexed form: TonerInfo[0]="85"; TonerInfo[1]="70"; ...
    indexed = {}
    for idx_str, pct_str in _RE_JS_ARRAY.findall(html):
        try:
            idx, pct = int(idx_str), int(pct_str)
        except ValueError:
            continue
        if 0 <= pct <= 100 and idx in (0, 1, 2, 3):
            indexed[idx] = pct
    if indexed:
        # Brother's documented index order: K, C, M, Y.
        order = ("black", "cyan", "magenta", "yellow")
        result = {order[i]: indexed[i] for i in sorted(indexed) if i < 4}
        if result:
            return result
    # new Array(...) form: pull the first 4 numbers, K/C/M/Y order.
    m = _RE_JS_NEW_ARRAY.search(html)
    if m:
        try:
            k, c, mg, y = (int(g) for g in m.groups())
        except ValueError:
            pass
        else:
            if all(0 <= v <= 100 for v in (k, c, mg, y)):
                return {"black": k, "cyan": c, "magenta": mg, "yellow": y}

    # --- Pattern B: HTML table rows -- process each <tr> in isolation ---
    table_hits: Dict[str, int] = {}
    for row_match in _RE_TABLE_ROW_BLOCK.finditer(html):
        row_body = row_match.group(1)
        color_m = _RE_ROW_COLOR.search(row_body)
        if not color_m:
            continue
        color = _COLOR_CODES.get(color_m.group(1).upper())
        if not color:
            continue
        # First numeric "NN%" appearing AFTER the color cell.
        pct_m = _RE_ROW_PCT.search(row_body, color_m.end())
        if not pct_m:
            continue
        try:
            pct = int(pct_m.group(1))
        except ValueError:
            continue
        if 0 <= pct <= 100:
            table_hits[color] = pct
    if table_hits:
        return table_hits

    # --- Pattern C: image filenames ---
    img_hits = {}
    for match in _RE_IMG_FILENAME.finditer(html):
        color = _COLOR_CODES.get(match.group("color").upper())
        try:
            pct = int(match.group("pct"))
        except ValueError:
            continue
        if color and 0 <= pct <= 100:
            img_hits[color] = pct
    if img_hits:
        return img_hits

    # --- Pattern D: tonerremain gauge images (MFC-L8900CDW family) ---
    gauge_hits: Dict[str, int] = {}
    for tag_match in _RE_TONERREMAIN_IMG.finditer(html):
        tag = tag_match.group(0)
        alt = height = None
        for attr in _RE_TONERREMAIN_ATTRS.finditer(tag):
            if attr.group("alt") is not None:
                alt = attr.group("alt")
            elif attr.group("height") is not None:
                height = attr.group("height")
        if not alt or height is None:
            continue
        color = _COLOR_CODES.get(alt.strip().upper())
        if not color:
            continue
        try:
            h = int(height)
        except ValueError:
            continue
        # Clamp to max -- some firmwares pad with extra pixels above the bar
        # area (we'd otherwise get >100%).
        pct = max(0, min(100, round(h * 100 / _TONERREMAIN_MAX_PX)))
        gauge_hits[color] = pct
    if gauge_hits:
        return gauge_hits

    return {}


async def _fetch_ews_html(ip: str) -> Optional[str]:
    """GET each likely Brother EWS path until one returns 200 with content."""
    # verify=False -- Brother EWS uses a self-signed cert; we're already
    # SNMP-authenticated to this device on the same LAN.
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS, verify=False, follow_redirects=True
    ) as client:
        for path in EWS_PATHS:
            url = f"http://{ip}{path}"
            try:
                resp = await client.get(url)
            except (httpx.RequestError, httpx.HTTPError) as exc:
                log.debug("EWS fetch %s failed: %s", url, exc)
                continue
            if resp.status_code == 200 and resp.text:
                return resp.text
            log.debug("EWS %s returned HTTP %d", url, resp.status_code)
    return None


class BrotherEwsProvider(PrinterProvider):
    """Runs AFTER BrotherProvider; overrides bucket UI-hints with real percentages
    when the EWS scrape succeeds. Silently does nothing on failure."""

    name = "brother_ews"
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
            html = await _fetch_ews_html(ip)
        except Exception as exc:  # noqa: BLE001 - HTTP failures are non-fatal
            log.debug("EWS fetch raised for %s: %s", ip, exc)
            return reading
        if not html:
            return reading

        try:
            percentages = _parse_toner_percentages(html)
        except Exception as exc:  # noqa: BLE001 - HTML parse failures are non-fatal
            log.debug("EWS parse raised for %s: %s", ip, exc)
            return reading

        if not percentages:
            return reading

        log.info("EWS toner percentages for %s: %s", ip, percentages)
        # Override the BrotherProvider's bucket estimate with the real % from
        # the gauge. Mark the supply as EWS-sourced for the UI.
        for supply in reading.get("supplies", []):
            if supply.get("type") != "toner":
                continue
            color = supply.get("color")
            if not color or color not in percentages:
                continue
            pct = float(percentages[color])
            # If SNMP already reported a real percentage AND it matches the EWS
            # value within 5%, leave it alone -- both agree, no need to override.
            existing = supply.get("level_pct")
            if existing is not None and abs(existing - pct) <= 5 and not supply.get("_brother_estimated"):
                continue
            supply["level_pct"] = pct
            # Replace the bucket note with something the operator finds useful.
            supply["status_note"] = None
            supply["_brother_estimated"] = False
            supply["_ews_sourced"] = True
        # Promote the precision marker: real percentages are available, not buckets.
        reading["_supply_precision"] = "brother_ews"
        return reading


register(BrotherEwsProvider())
