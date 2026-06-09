"""Lexmark provider.

Lexmark printers ship one of the most compliant Printer-MIB implementations
in the industry -- standard ``prtMarkerSuppliesLevel`` reports real
percentages for toner, drum, fuser, and the maintenance kit, and
``prtAlertTable`` carries Lexmark's well-written human-readable alert text.
So unlike Brother (where this layer rescues a broken standard MIB), the
Lexmark provider's job is enrichment, not rescue:

* Confirm the brand on every reading so the dashboard tags it correctly even
  when ``sysDescr`` doesn't include the word ``Lexmark``. Some firmware uses
  ``LMA`` (Lexmark Management Agent) or model-prefix-only sysDescr.
* Surface the operator-panel display message (Lexmark's private MIB exposes
  the current front-panel text as a single scalar) as the reading's status
  note so techs can see what the printer is showing locally without driving
  to the site.
* Tag the reading with ``_supply_precision = "lexmark_native"`` so the UI
  can show a positive "real percentages from device" badge instead of the
  generic Printer-MIB question marks.

OIDs come from Lexmark's published Print MIB. The provider is fully
defensive: a printer that exposes only the standard MIB still works, and
this layer just adds a brand tag + supply precision label.

Refs:
 * Lexmark enterprise OID: 1.3.6.1.4.1.641
 * Lexmark Print MIB: lexPrinterMIB at .6
"""

from __future__ import annotations

import logging
from typing import Optional

from printer_nanny_agent.providers import PrinterProvider, register
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams

log = logging.getLogger("printer_nanny_agent.providers.lexmark")

# Lexmark private MIB scalars. The operator panel message is the same string
# the printer's front-panel LCD displays -- "Ready", "Paper Jam Tray 2",
# "88 Cyan Cartridge Low", etc. Exposed as a single scalar in lexgenOpMsg.
OID_OPERATOR_PANEL_LINE1 = "1.3.6.1.4.1.641.6.4.4.4.1.0"
OID_OPERATOR_PANEL_LINE2 = "1.3.6.1.4.1.641.6.4.4.4.2.0"
# Fall-back location for older firmware (lexPrinterOpPanel table).
OID_OPERATOR_PANEL_FALLBACK = "1.3.6.1.4.1.641.6.4.5.1.5.1"


def _clean(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    # pysnmp renders missing values with the marker text below; treat as None.
    if text.startswith(("No Such Object", "No Such Instance")):
        return None
    return text


class LexmarkProvider(PrinterProvider):
    name = "lexmark"
    enterprise_prefixes = ("641",)

    async def augment(
        self,
        backend: SnmpBackend,
        ip: str,
        params: SnmpParams,
        reading: dict,
        sys_object_id: Optional[str],
    ) -> dict:
        # Brand tag: many Lexmark models report sysDescr like "Lexmark MX622"
        # already, but some say only "MX622" or use Lexmark's LMA agent
        # string. Set it from the sysObjectID match so the dashboard always
        # groups correctly.
        identity = reading.setdefault("identity", {})
        if not (identity.get("brand") or reading.get("brand")):
            identity["brand"] = "Lexmark"

        # Operator panel message -- best-effort. Lexmark exposes a two-line
        # front-panel display via the private MIB; concatenate non-empty lines.
        panel_text: Optional[str] = None
        try:
            scalars = await backend.get(
                ip,
                [OID_OPERATOR_PANEL_LINE1, OID_OPERATOR_PANEL_LINE2],
                params,
            )
            lines = [
                _clean(scalars.get(OID_OPERATOR_PANEL_LINE1)),
                _clean(scalars.get(OID_OPERATOR_PANEL_LINE2)),
            ]
            joined = " ".join(line for line in lines if line)
            if joined:
                panel_text = joined
        except SnmpError as exc:
            log.debug("Lexmark op-panel scalars failed for %s: %s", ip, exc)

        if panel_text is None:
            # Fallback: some firmware buries the same text under the print panel
            # table. Treat any reachable value as the display text.
            try:
                fb = await backend.get(ip, [OID_OPERATOR_PANEL_FALLBACK], params)
                panel_text = _clean(fb.get(OID_OPERATOR_PANEL_FALLBACK))
            except SnmpError as exc:
                log.debug("Lexmark op-panel fallback failed for %s: %s", ip, exc)

        if panel_text:
            reading["device_status_text"] = panel_text
            # Surface a non-"Ready" panel message as an info-level event so the
            # operator can see at a glance what's on the device's display.
            normalized = panel_text.strip().lower()
            if normalized and normalized not in ("ready", "ready *"):
                reading.setdefault("events", []).append(
                    {
                        "code": "lexmark-panel",
                        "severity": "info",
                        "source": "snmp_alert",
                        "message": f"Operator panel: {panel_text}",
                    }
                )

        reading["_supply_precision"] = "lexmark_native"
        return reading


register(LexmarkProvider())
