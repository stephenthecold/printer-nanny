"""HP / Hewlett-Packard provider.

HP has the widest enterprise deployment of any laser-printer brand and one of
the most mature Printer-MIB implementations -- the standard ``prtMarker``
tables return accurate percentages for toner / drum / fuser / maintenance kit
on every LaserJet, OfficeJet, and PageWide model from the last decade. The
job of the HP provider, therefore, is mostly enrichment and tagging:

* Confirm the brand on every reading. HP devices identify under enterprise
  ``11.2.3.9`` (hpPrinters) but ``sysDescr`` strings vary -- "HP LaserJet
  M404", "HP Color LaserJet Pro M454dw", "HP ETHERNET MULTI-ENVIRONMENT,
  ROM ...". Lock brand="HP" when the sysObjectID is in the HP subtree.
* Read the HP device status text scalar (the message shown on the front
  panel of every HP printer -- "Ready", "Replace Black Cartridge",
  "Paper Jam in Tray 2", etc.) from the HP private MIB and surface it as
  the reading's ``device_status_text`` so techs can see what the printer is
  showing locally without driving out.
* Read ``hpDeviceModel`` (more precise than sysDescr for sub-model split,
  e.g. M404dn vs M404n) when available.
* Tag the reading with ``_supply_precision = "hp_native"`` so the UI can
  show a positive precision badge.

The provider is defensive: a printer that exposes only the standard MIB
still works -- this layer just sets the brand tag and the supply-precision
badge.

OID refs (HP public Printer MIB extension):
 * HP enterprise OID:   1.3.6.1.4.1.11
 * HP printers subtree: 1.3.6.1.4.1.11.2.3.9
 * Device status msg:   1.3.6.1.4.1.11.2.3.9.4.2.1.1.3.3.0
 * Device model:        1.3.6.1.4.1.11.2.3.9.1.1.7.0
"""

from __future__ import annotations

import logging
from typing import Optional

from printer_nanny_agent.providers import PrinterProvider, register
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams

log = logging.getLogger("printer_nanny_agent.providers.hp")

# HP device status text scalar -- the front-panel display message.
OID_HP_DEVICE_STATUS_MSG = "1.3.6.1.4.1.11.2.3.9.4.2.1.1.3.3.0"
# HP device model identifier (more precise than sysDescr's free-text string).
OID_HP_DEVICE_MODEL = "1.3.6.1.4.1.11.2.3.9.1.1.7.0"


def _clean(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith(("No Such Object", "No Such Instance")):
        return None
    return text


class HPProvider(PrinterProvider):
    name = "hp"
    # Match the HP printers subtree specifically, not the whole HP enterprise --
    # HP makes a lot of non-printer hardware, and the generic "11" prefix would
    # over-match. ``11.2.3.9`` is the documented hpPrinters branch.
    enterprise_prefixes = ("11.2.3.9",)

    async def augment(
        self,
        backend: SnmpBackend,
        ip: str,
        params: SnmpParams,
        reading: dict,
        sys_object_id: Optional[str],
    ) -> dict:
        # Always tag the brand -- sysDescr strings vary too much to rely on.
        identity = reading.setdefault("identity", {})
        if not (identity.get("brand") or reading.get("brand")):
            identity["brand"] = "HP"

        # Pull the HP private MIB scalars in a single SNMP GET when possible.
        try:
            scalars = await backend.get(
                ip,
                [OID_HP_DEVICE_STATUS_MSG, OID_HP_DEVICE_MODEL],
                params,
            )
            status_msg = _clean(scalars.get(OID_HP_DEVICE_STATUS_MSG))
            device_model = _clean(scalars.get(OID_HP_DEVICE_MODEL))
        except SnmpError as exc:
            log.debug("HP private MIB get failed for %s: %s", ip, exc)
            status_msg = device_model = None

        if status_msg:
            reading["device_status_text"] = status_msg
            normalized = status_msg.strip().lower()
            # Surface non-"Ready" panel messages as info-level events. HP's
            # display covers a wide spectrum from informational ("Ready",
            # "Sleeping") to alarming ("Replace Cartridge", "13.20 Paper Jam"),
            # so we let central's alert rules decide what's worth a ticket.
            if normalized and normalized not in ("ready", "sleeping", "energy save on"):
                reading.setdefault("events", []).append(
                    {
                        "code": "hp-panel",
                        "severity": "info",
                        "source": "snmp_alert",
                        "message": f"Operator panel: {status_msg}",
                    }
                )

        if device_model and not identity.get("model"):
            identity["model"] = device_model

        reading["_supply_precision"] = "hp_native"
        return reading


register(HPProvider())
