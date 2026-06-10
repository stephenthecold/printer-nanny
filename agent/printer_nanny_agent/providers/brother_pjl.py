"""Brother PJL-over-9100 provider -- the channel BRAdmin Professional uses.

Brother SNMP private MIB only exposes OK/Low/Empty buckets, and EWS HTML
scraping varies wildly across firmware generations (per-model gauge pixel
math, attribute order, layout changes). PJL queries over TCP/9100 are the
official Brother management protocol that BRAdmin Pro and most third-party
fleet tools use; they return plain-text percentages directly from the
firmware's consumption model.

Protocol: TCP/9100 is the print-job port, but it also accepts inquiry
commands when wrapped in the UEL (Universal Exit Language) marker. We send:

    \x1b%-12345X    -- UEL: enter PJL mode
    @PJL JOB
    @PJL INFO STATUS
    @PJL INFO MAINTENANCE
    @PJL INFO SUPPLIES
    @PJL INFO PAGECOUNT
    @PJL EOJ
    \x1b%-12345X    -- UEL: exit PJL mode

The printer streams back text we parse. Multiple response formats are
tolerated since Brother's PJL extensions have evolved across model
generations -- the parser tries each known shape.

Falls back silently on any failure (TCP/9100 blocked by network ACL, port
filtered, no response, garbage response) so the rest of the reading still
ships from SNMP + EWS / bucket data.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Dict, Optional

from printer_nanny_agent.providers import PrinterProvider
from printer_nanny_agent.snmp import SnmpBackend, SnmpParams

log = logging.getLogger("printer_nanny_agent.providers.brother_pjl")

PJL_PORT = 9100
PJL_TIMEOUT_SECONDS = 3.0
UEL = b"\x1b%-12345X"
PJL_QUERY = (
    UEL
    + b"@PJL JOB\r\n"
    + b"@PJL INFO STATUS\r\n"
    + b"@PJL INFO MAINTENANCE\r\n"
    + b"@PJL INFO SUPPLIES\r\n"
    + b"@PJL INFO PAGECOUNT\r\n"
    + b"@PJL EOJ\r\n"
    + UEL
)

_COLOR_KEYWORDS = {
    "BLACK": "black", "BK": "black", "K": "black",
    "CYAN": "cyan", "C": "cyan",
    "MAGENTA": "magenta", "M": "magenta",
    "YELLOW": "yellow", "Y": "yellow",
}

# Format 1 (most modern Brother lasers, e.g. HL-L2370DW, MFC-L8900CDW with
# recent firmware): each line is "<KEYWORD>_<COLOR>=NN" or "<KEYWORD>=NN".
# Tolerates both ordering: TONER_BLACK vs BLACK_TONER, with optional %.
_RE_TONER_KEY_VALUE = re.compile(
    r"""(?:TONER[_\-\s]*(?P<color1>BLACK|CYAN|MAGENTA|YELLOW|BK|K|C|M|Y)
        |(?P<color2>BLACK|CYAN|MAGENTA|YELLOW|BK|K|C|M|Y)[_\-\s]*TONER)
        [_\-\s]*(?:LIFE)?[_\-\s]*(?:REMAINING|REMAIN|LIFE)?
        \s*=\s*(?P<pct>\d{1,3})\s*%?""",
    re.IGNORECASE | re.VERBOSE,
)

# Format 2 (older / Brother BR-Script): "TONER=K=85%,C=70%,M=60%,Y=50%" all
# on one line.
_RE_TONER_COMPACT = re.compile(
    r"""TONER\s*=\s*
        (?P<body>[KCMY\s,=%\d]+)""",
    re.IGNORECASE | re.VERBOSE,
)
_RE_COMPACT_PAIR = re.compile(r"([KCMY])\s*=\s*(\d{1,3})", re.IGNORECASE)

# Format 3: "TONER LIFE REMAINING=K=NN" / "TONER=K=NN" single-line w/ color
# as the FIRST nested key. Same shape Brother emits on older HL/MFC firmware.
_RE_TONER_NESTED_COLOR = re.compile(
    r"""TONER(?:[_\-\s]*LIFE)?(?:[_\-\s]*REMAINING|[_\-\s]*REMAIN)?
        \s*=\s*(?P<color>[KCMY])
        \s*=\s*(?P<pct>\d{1,3})\s*%?""",
    re.IGNORECASE | re.VERBOSE,
)

# Drum / page count -- nice-to-have, dumped into status_note for now.
# Accepts both "DRUM=NN%" (modern) and "DRUM=K=NN%" (older / BR-Script form).
_RE_DRUM = re.compile(
    r"""DRUM[_\-\s]*(?:LIFE[_\-\s]*)?(?:REMAINING|REMAIN)?
        \s*=\s*(?:[KCMY]\s*=\s*)?(?P<pct>\d{1,3})\s*%?""",
    re.IGNORECASE | re.VERBOSE,
)


def _parse_pjl_response(text: str) -> Dict[str, int]:
    """{color_name: percent} for toner supplies. Empty when nothing parseable."""
    if not text:
        return {}
    out: Dict[str, int] = {}

    # --- Format 1: per-toner key=value lines ---
    for match in _RE_TONER_KEY_VALUE.finditer(text):
        color_code = (match.group("color1") or match.group("color2") or "").upper()
        color = _COLOR_KEYWORDS.get(color_code)
        try:
            pct = int(match.group("pct"))
        except (TypeError, ValueError):
            continue
        if color and 0 <= pct <= 100 and color not in out:
            out[color] = pct
    if out:
        return out

    # --- Format 2: compact TONER=K=85%,C=70%,M=60%,Y=50% ---
    # Run this BEFORE Format 3 so the comma-separated form isn't truncated --
    # Format 3's regex would otherwise match just the first K=NN pair and
    # short-circuit.
    m = _RE_TONER_COMPACT.search(text)
    if m and "," in m.group("body"):
        for pair in _RE_COMPACT_PAIR.finditer(m.group("body")):
            color = _COLOR_KEYWORDS.get(pair.group(1).upper())
            try:
                pct = int(pair.group(2))
            except ValueError:
                continue
            if color and 0 <= pct <= 100:
                out[color] = pct
        if out:
            return out

    # --- Format 3: nested-color, single value (TONER LIFE REMAINING=K=NN) ---
    for match in _RE_TONER_NESTED_COLOR.finditer(text):
        color = _COLOR_KEYWORDS.get(match.group("color").upper())
        try:
            pct = int(match.group("pct"))
        except ValueError:
            continue
        if color and 0 <= pct <= 100 and color not in out:
            out[color] = pct
    return out


def _parse_pjl_drum(text: str) -> Optional[int]:
    """Drum life % remaining, or None if absent."""
    if not text:
        return None
    m = _RE_DRUM.search(text)
    if not m:
        return None
    try:
        pct = int(m.group("pct"))
    except ValueError:
        return None
    return pct if 0 <= pct <= 100 else None


async def _query_pjl(ip: str) -> Optional[str]:
    """Open TCP/9100, send the PJL inquiry, return the decoded response or None.

    Defensive: any failure (refused, timeout, EOF) returns None and the caller
    moves on. We never raise; PJL is an opportunistic enrichment.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, PJL_PORT), timeout=PJL_TIMEOUT_SECONDS
        )
    except (OSError, asyncio.TimeoutError) as exc:
        log.debug("PJL connect %s:%d failed: %s", ip, PJL_PORT, exc)
        return None
    chunks = []
    try:
        writer.write(PJL_QUERY)
        await writer.drain()
        # Read until UEL terminator or socket closes. Brother lasers
        # typically stream all responses back within ~500ms.
        deadline = asyncio.get_event_loop().time() + PJL_TIMEOUT_SECONDS
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            # Brother sends a final UEL when the response is complete; stop
            # waiting once we see the second one (one at start, one at end).
            if b"".join(chunks).count(UEL) >= 2:
                break
    except OSError as exc:
        log.debug("PJL read %s failed: %s", ip, exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - close-failure is non-fatal
            pass
    raw = b"".join(chunks)
    if not raw:
        return None
    # PJL responses are ASCII-ish; latin-1 round-trips bytes safely.
    return raw.decode("latin-1", errors="replace")


class BrotherPjlProvider(PrinterProvider):
    """Runs BEFORE BrotherEwsProvider so the more-reliable PJL data wins.
    Provider order in providers/__init__.py: brother (SNMP) -> brother_pjl
    (this) -> brother_ews (HTML fallback)."""

    name = "brother_pjl"
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
            response = await _query_pjl(ip)
        except Exception as exc:  # noqa: BLE001 - PJL failures are non-fatal
            log.debug("PJL augment raised for %s: %s", ip, exc)
            return reading
        if not response:
            return reading

        toner_pcts = _parse_pjl_response(response)
        drum_pct = _parse_pjl_drum(response)
        if not toner_pcts and drum_pct is None:
            log.debug("PJL response for %s contained no parseable supplies", ip)
            return reading

        log.info(
            "PJL supply percentages for %s: toner=%s drum=%s",
            ip, toner_pcts, drum_pct,
        )

        for supply in reading.get("supplies", []):
            if supply.get("type") == "toner":
                color = supply.get("color")
                if color and color in toner_pcts:
                    supply["level_pct"] = float(toner_pcts[color])
                    supply["status_note"] = None
                    # Mark so the EWS scraper (less reliable) leaves it alone.
                    supply["_pjl_sourced"] = True
                    supply["_brother_estimated"] = False
            elif supply.get("type") == "drum" and drum_pct is not None:
                if supply.get("level_pct") is None or supply.get("_brother_estimated"):
                    supply["level_pct"] = float(drum_pct)
                    supply["status_note"] = None
                    supply["_pjl_sourced"] = True
        reading["_supply_precision"] = "brother_pjl"
        return reading


# Not registered standalone: the consolidated BrotherProvider (brother.py)
# invokes this as a fallback pass so a Brother printer produces ONE
# diagnostics row instead of four.
