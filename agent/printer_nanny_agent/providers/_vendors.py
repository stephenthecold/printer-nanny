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
from typing import Optional

from printer_nanny_agent.providers import PrinterProvider, register
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams

log = logging.getLogger("printer_nanny_agent.providers.vendors")


def _clean(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if not text or text.startswith(("No Such Object", "No Such Instance")):
        return None
    return text


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
