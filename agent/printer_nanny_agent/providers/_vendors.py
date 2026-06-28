"""Defensive providers for the long tail of office printer vendors.

Same pattern as HP and Lexmark: detect by sysObjectID enterprise prefix,
tag brand + ``_supply_precision`` so the dashboard says where data came
from, read a couple of well-known private-MIB scalars to surface the
front-panel status message (which is what techs reference when calling
into the printer), and degrade silently on every error.

These providers DON'T attempt to decode supply percentages from the
private MIB without real probe data -- the standard Printer-MIB
(prtMarkerSuppliesLevel) already covers most of these vendors at the
percentage level, and getting the private subtree right requires
``printer-nanny-agent probe <ip>`` against the actual hardware.

They DO defensively harden the *standard*-MIB supplies the poller already
built: pysnmp renders a binary OCTET STRING description as a "0x…" hex
string, which would otherwise leak onto the printer-detail Supplies list as
the cartridge name, and the standard prtMarkerSuppliesType code is often the
catch-all "other" so every cartridge shows up colorless. We decode that hex
to readable text and re-classify the recognizable names (Black/Cyan/...,
Drum, Fuser, Waste, ...) to the right SupplyType + color. This invents no
private-MIB OIDs -- it only cleans up what the standard tables produced.

When you add a new model to the fleet and want exact private-MIB
decoding for it, paste the probe output and we'll teach the matching
provider here -- the registration / detection scaffolding is the
expensive part and it's done.

Enterprise OIDs used (cross-checked against IANA registrations):
  Xerox          253
  Kyocera        1347
  Canon          1602
  Ricoh          367
  Konica Minolta 18334

Front-panel / status-message OIDs are well-documented per vendor in
their public MIB files; URLs in each provider's docstring.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from printer_nanny_agent.providers import PrinterProvider, register
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams

# These scaffolding providers prefer the shared decode/classify helpers when the
# decode-foundation work has landed them on snmp_parse, so all brands converge on
# one implementation. They are imported defensively: at time of writing the
# foundation change is a sibling in-flight, so we fall back to the inline
# equivalents below (kept byte-compatible in intent) when the import is absent.
# NOTE (integration): once snmp_parse exports ``decode_supply_text`` and
# ``classify_supply``, drop the inline ``_fallback_*`` definitions and use the
# imported names everywhere.
try:  # pragma: no cover - exercised once the foundation helper lands
    from printer_nanny_agent.snmp_parse import (  # type: ignore[attr-defined]
        classify_supply as _shared_classify_supply,
        decode_supply_text as _shared_decode_supply_text,
    )
    _HAVE_SHARED_DECODE = True
except ImportError:  # foundation not merged yet -> use inline equivalents
    _shared_classify_supply = None  # type: ignore[assignment]
    _shared_decode_supply_text = None  # type: ignore[assignment]
    _HAVE_SHARED_DECODE = False

log = logging.getLogger("printer_nanny_agent.providers.vendors")


def _clean(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if not text or text.startswith(("No Such Object", "No Such Instance")):
        return None
    return text


# --- Defensive supply decode/classify ---------------------------------------
# pysnmp renders a non-ASCII / binary OCTET STRING as a "0x…" hex string. The
# standard-MIB poller (poller.build_supplies) stores prtMarkerSuppliesDescription
# verbatim, so on these long-tail brands that hex leaks straight onto the printer
# detail page as the supply *name* (e.g. "0x426c61636b" instead of "Black") and
# every cartridge falls through to SupplyType "other" with no color. This is
# defensive hardening: decode the hex to readable text and re-classify the name
# to the right (SupplyType, color). We invent NO private-MIB OIDs here -- we only
# clean up what the standard Printer-MIB already produced.

# Recognizable supply-name keyword -> our SupplyType string. Ordered so that the
# component keywords ("drum"/"fuser"/...) win over a bare color word: a "Black
# Drum" is a drum, not toner. Maintenance / transfer / PF kits are real supplies
# but have no dedicated SupplyType, so they land on "other" with a readable name.
_TYPE_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ("waste", "waste"),
    ("drum", "drum"),
    ("imaging unit", "drum"),
    ("imaging drum", "drum"),
    ("photoconductor", "drum"),
    ("opc", "drum"),
    ("fuser", "fuser"),
    ("fusing", "fuser"),
    ("developer", "developer"),
    ("staple", "staples"),
    ("ink", "ink"),
    ("toner", "toner"),
    ("cartridge", "toner"),
)

_COLOR_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ("black", "black"),
    ("cyan", "cyan"),
    ("magenta", "magenta"),
    ("yellow", "yellow"),
)


def _fallback_decode_supply_text(value: Optional[str]) -> Optional[str]:
    """Render a possibly-hex SNMP OCTET STRING as readable text.

    pysnmp gives non-printable octet strings back as "0x<hex>". Decode those to
    UTF-8 (latin-1 fallback), strip NULs/control padding, and collapse runs of
    whitespace. Clean ASCII input passes through unchanged. Returns None when the
    value is empty or decodes to nothing printable -- never returns a "0x…"
    string.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith("0x"):
        hexpart = "".join(text[2:].split())
        if len(hexpart) % 2:
            hexpart = "0" + hexpart
        try:
            raw = bytes.fromhex(hexpart)
        except ValueError:
            # Not valid hex after all -- treat the original as plain text.
            return text or None
        for codec in ("utf-8", "latin-1"):
            try:
                decoded = raw.decode(codec)
                break
            except UnicodeDecodeError:
                decoded = None
        if decoded is None:
            decoded = raw.decode("latin-1", errors="ignore")
        text = decoded
    # Drop NULs / control padding that some firmwares append, then squash
    # internal whitespace so "Black\x00\x00" -> "Black".
    cleaned = "".join(ch for ch in text if ch == " " or ch.isprintable())
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def _fallback_classify_supply(
    description: Optional[str],
) -> Tuple[str, Optional[str]]:
    """Best-effort (SupplyType, color) from a (decoded) supply description.

    Defensive: only classifies when the name is recognizable; otherwise returns
    ("other", <color-if-any>). Color is extracted independently of type so a
    "Black Drum" comes back ("drum", "black"). Maintenance / transfer / PF kits
    and the like fall to ("other", None) -- but the caller keeps the readable
    name, so the operator still sees what the supply is.
    """
    if not description:
        return ("other", None)
    low = description.lower()
    color: Optional[str] = None
    for keyword, name in _COLOR_KEYWORDS:
        if keyword in low:
            color = name
            break
    for keyword, stype in _TYPE_KEYWORDS:
        if keyword in low:
            return (stype, color)
    # Recognizable bare color with no component word -> a toner cartridge.
    if color is not None:
        return ("toner", color)
    return ("other", None)


def decode_supply_text(value: Optional[str]) -> Optional[str]:
    """Decode an SNMP supply description (shared helper if available)."""
    if _HAVE_SHARED_DECODE and _shared_decode_supply_text is not None:
        return _shared_decode_supply_text(value)
    return _fallback_decode_supply_text(value)


def classify_supply(description: Optional[str]) -> Tuple[str, Optional[str]]:
    """Classify a decoded supply name to (type, color) (shared if available)."""
    if _HAVE_SHARED_DECODE and _shared_classify_supply is not None:
        return _shared_classify_supply(description)
    return _fallback_classify_supply(description)


def _harden_supplies(reading: dict) -> None:
    """Clean every supply the standard-MIB poller produced for this device.

    For each supply dict: decode its description (no "0x…" leaks), and -- when the
    standard prtMarkerSuppliesType code left it as the catch-all "other" (or gave
    no type) while the name is actually recognizable -- re-classify it to the
    right SupplyType and color. We never downgrade a type the standard MIB already
    pinned to something specific; we only fill in / correct the "other" fallthrough
    so the dashboard stops showing every cartridge as a colorless "other".
    """
    for supply in reading.get("supplies", []):
        if not isinstance(supply, dict):
            continue
        readable = decode_supply_text(supply.get("description"))
        if readable is not None:
            supply["description"] = readable
        guess_type, guess_color = classify_supply(readable)
        existing_type = supply.get("type")
        # Only override the catch-all "other" / missing type so we don't fight a
        # specific code the device actually sent on prtMarkerSuppliesType.
        if (not existing_type or existing_type == "other") and guess_type != "other":
            supply["type"] = guess_type
        if not supply.get("color") and guess_color is not None:
            supply["color"] = guess_color


def _surface_panel(reading: dict, brand: str, label: str, text: Optional[str],
                   noisy: tuple = ()) -> None:
    """Standard pattern: stamp brand + status text, log non-noise as an event."""
    identity = reading.setdefault("identity", {})
    if not (identity.get("brand") or reading.get("brand")):
        identity["brand"] = brand
    if text:
        reading["device_status_text"] = text
        normalized = text.strip().lower()
        if normalized and normalized not in noisy:
            reading.setdefault("events", []).append({
                "code": label,
                "severity": "info",
                "source": "snmp_alert",
                "message": f"Operator panel: {text}",
            })


# --- Xerox ------------------------------------------------------------------
# Public MIBs: ftp://ftp.xerox.com/pub/network/standards/MIB/
# Enterprise: 253
# Display message (multiple firmware generations populate one of these):
#   1.3.6.1.4.1.253.8.53.13.2.1.6.1.1     (xeroxOperatorPanelMessage)
class XeroxProvider(PrinterProvider):
    name = "xerox"
    enterprise_prefixes = ("253",)

    OID_PANEL = "1.3.6.1.4.1.253.8.53.13.2.1.6.1.1"

    async def augment(self, backend: SnmpBackend, ip: str, params: SnmpParams,
                      reading: dict, sys_object_id: Optional[str]) -> dict:
        try:
            data = await backend.get(ip, [self.OID_PANEL], params)
            text = _clean(data.get(self.OID_PANEL))
        except SnmpError as exc:
            log.debug("Xerox panel get failed for %s: %s", ip, exc)
            text = None
        _surface_panel(reading, "Xerox", "xerox-panel", text,
                       noisy=("ready", "sleep", "sleep mode", "power saver"))
        _harden_supplies(reading)
        reading["_supply_precision"] = "xerox_standard"
        return reading


# --- Kyocera ----------------------------------------------------------------
# Public MIBs: https://www.kyoceradocumentsolutions.com/.../mib.html
# Enterprise: 1347
# Display message:
#   1.3.6.1.4.1.1347.43.5.2.1.5.1.1       (kdfPrinterStatusMessage)
class KyoceraProvider(PrinterProvider):
    name = "kyocera"
    enterprise_prefixes = ("1347",)

    OID_PANEL = "1.3.6.1.4.1.1347.43.5.2.1.5.1.1"

    async def augment(self, backend: SnmpBackend, ip: str, params: SnmpParams,
                      reading: dict, sys_object_id: Optional[str]) -> dict:
        try:
            data = await backend.get(ip, [self.OID_PANEL], params)
            text = _clean(data.get(self.OID_PANEL))
        except SnmpError as exc:
            log.debug("Kyocera panel get failed for %s: %s", ip, exc)
            text = None
        _surface_panel(reading, "Kyocera", "kyocera-panel", text,
                       noisy=("ready", "ready.", "sleeping", "energy save mode"))
        _harden_supplies(reading)
        reading["_supply_precision"] = "kyocera_standard"
        return reading


# --- Canon ------------------------------------------------------------------
# Enterprise: 1602
# Display message:
#   1.3.6.1.4.1.1602.1.11.1.3.1.4.1       (canonPrinterStatusMessage)
class CanonProvider(PrinterProvider):
    name = "canon"
    enterprise_prefixes = ("1602",)

    OID_PANEL = "1.3.6.1.4.1.1602.1.11.1.3.1.4.1"

    async def augment(self, backend: SnmpBackend, ip: str, params: SnmpParams,
                      reading: dict, sys_object_id: Optional[str]) -> dict:
        try:
            data = await backend.get(ip, [self.OID_PANEL], params)
            text = _clean(data.get(self.OID_PANEL))
        except SnmpError as exc:
            log.debug("Canon panel get failed for %s: %s", ip, exc)
            text = None
        _surface_panel(reading, "Canon", "canon-panel", text,
                       noisy=("ready to print", "sleeping", "sleep mode"))
        _harden_supplies(reading)
        reading["_supply_precision"] = "canon_standard"
        return reading


# --- Ricoh ------------------------------------------------------------------
# Enterprise: 367
# Display message (RICOH-PRINTER MIB):
#   1.3.6.1.4.1.367.3.2.1.2.24.1.1        (ricohPrinterPanelMessage)
class RicohProvider(PrinterProvider):
    name = "ricoh"
    enterprise_prefixes = ("367",)

    OID_PANEL = "1.3.6.1.4.1.367.3.2.1.2.24.1.1"

    async def augment(self, backend: SnmpBackend, ip: str, params: SnmpParams,
                      reading: dict, sys_object_id: Optional[str]) -> dict:
        try:
            data = await backend.get(ip, [self.OID_PANEL], params)
            text = _clean(data.get(self.OID_PANEL))
        except SnmpError as exc:
            log.debug("Ricoh panel get failed for %s: %s", ip, exc)
            text = None
        _surface_panel(reading, "Ricoh", "ricoh-panel", text,
                       noisy=("ready", "energy save mode", "energy save"))
        _harden_supplies(reading)
        reading["_supply_precision"] = "ricoh_standard"
        return reading


# --- Konica Minolta ---------------------------------------------------------
# Enterprise: 18334 (Konica Minolta Business Technologies)
# Display message (BIZHUB SNMP profile):
#   1.3.6.1.4.1.18334.1.1.1.5.7.1.1.4.1   (kmDeviceMessageDisplay)
class KonicaMinoltaProvider(PrinterProvider):
    name = "konica_minolta"
    enterprise_prefixes = ("18334",)

    OID_PANEL = "1.3.6.1.4.1.18334.1.1.1.5.7.1.1.4.1"

    async def augment(self, backend: SnmpBackend, ip: str, params: SnmpParams,
                      reading: dict, sys_object_id: Optional[str]) -> dict:
        try:
            data = await backend.get(ip, [self.OID_PANEL], params)
            text = _clean(data.get(self.OID_PANEL))
        except SnmpError as exc:
            log.debug("Konica panel get failed for %s: %s", ip, exc)
            text = None
        _surface_panel(reading, "Konica Minolta", "konica-panel", text,
                       noisy=("ready to print", "sleep mode", "low power mode"))
        _harden_supplies(reading)
        reading["_supply_precision"] = "konica_minolta_standard"
        return reading


for provider in (
    XeroxProvider(),
    KyoceraProvider(),
    CanonProvider(),
    RicohProvider(),
    KonicaMinoltaProvider(),
):
    register(provider)
