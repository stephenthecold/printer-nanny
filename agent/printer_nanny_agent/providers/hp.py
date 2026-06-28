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
* Clean up the supply rows. HP enterprise MFPs (e.g. the LaserJet MFP
  E72430) hand back ``prtMarkerSuppliesDescription`` as a binary OCTET
  STRING, which pysnmp renders as a raw ``0x426c61636b...`` hex string, and
  some firmware leaves ``prtMarkerSuppliesType`` at the generic bucket so
  every cartridge falls through to SupplyType "other" with no color. Decode
  the hex to readable text and re-classify type + color from the name, so
  techs see "Black", "Fuser", "Black Drum" instead of a hex blob.
* Tag the reading with ``_supply_precision = "hp_native"`` so the UI can
  show a positive precision badge.

The provider is defensive: a printer that exposes only the standard MIB
still works -- this layer just sets the brand tag, normalizes supply names,
and the supply-precision badge. It does not invent private-MIB OIDs: the
only HP-private scalars touched are the documented device-status-message and
device-model objects below; the supply work operates purely on the standard
``prtMarkerSupplies`` values already in the reading.

OID refs (HP public Printer MIB extension):
 * HP enterprise OID:   1.3.6.1.4.1.11
 * HP printers subtree: 1.3.6.1.4.1.11.2.3.9
 * Device status msg:   1.3.6.1.4.1.11.2.3.9.4.2.1.1.3.3.0
 * Device model:        1.3.6.1.4.1.11.2.3.9.1.1.7.0
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from printer_nanny_agent.providers import PrinterProvider, register
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams
from printer_nanny_agent.snmp_parse import normalize_color

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


def decode_supply_text(value: Optional[str]) -> Optional[str]:
    """Render an SNMP OCTET STRING supply description as readable text.

    pysnmp returns a printable OCTET STRING verbatim, but a non-ASCII / binary
    one comes back as a ``0x...`` hex string. HP enterprise MFPs (E-series)
    encode their supply names this way, so ``prtMarkerSuppliesDescription``
    arrives as e.g. ``0x426c61636b`` ("Black") or ``0x4675736572`` ("Fuser").
    Decode the hex to UTF-8/latin-1 text; pass non-hex text straight through.

    Defensive: returns the original (stripped) string if the value isn't a
    clean even-length hex blob or doesn't decode to printable text -- never
    raises, never lets a ``0x...`` blob leak through when it can be decoded.

    NOTE FOR INTEGRATION: a sibling change set is adding a shared
    ``decode_supply_text`` to the agent decode foundation
    (poller.py / snmp_parse.py). This is the equivalent done inline so HP
    works standalone; on merge, route through the shared helper and delete
    this copy.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    low = text.lower()
    if not low.startswith("0x"):
        return text
    hexpart = text[2:].replace(" ", "").replace(":", "")
    if not hexpart or len(hexpart) % 2:
        return text
    try:
        raw = bytes.fromhex(hexpart)
    except ValueError:
        return text
    # Drop a single trailing NUL some firmware appends to fixed-width fields.
    raw = raw.rstrip(b"\x00")
    if not raw:
        return text
    for encoding in ("utf-8", "latin-1"):
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        decoded = decoded.strip().strip("\x00").strip()
        # Only accept if it's genuinely printable text -- otherwise the "0x..."
        # form was already the best representation and we leave it untouched
        # rather than emit control-char gibberish.
        if decoded and all(ch == "\t" or ch >= " " for ch in decoded):
            return decoded
    return text


# Supply-name keyword -> SupplyType. Ordered: the first keyword found in the
# (lower-cased) description wins, so more specific names ("black drum") map to
# the right type before the generic colour keyword ("black") would claim them.
# Types are the central SupplyType strings (toner/ink/drum/fuser/waste/
# staples/developer/other). Maintenance / transfer kits and ADF/roller parts
# have no dedicated SupplyType, so they stay "other" -- but with a readable
# name instead of a hex blob.
_TYPE_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ("waste", "waste"),
    ("drum", "drum"),
    ("imaging unit", "drum"),
    ("imaging kit", "drum"),
    ("opc", "drum"),
    ("photoconductor", "drum"),
    ("fuser", "fuser"),
    ("developer", "developer"),
    ("staple", "staples"),
    ("transfer", "other"),
    ("maintenance", "other"),
    ("toner", "toner"),
    ("cartridge", "toner"),
    ("ink", "ink"),
)


def classify_supply(description: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Classify a supply (type, color) from its (decoded) description name.

    Returns ``(type, color)`` where ``type`` is a central SupplyType string or
    ``None`` when nothing matches (caller keeps whatever it already had), and
    ``color`` is a normalized colorant or ``None``.

    Reuses the shared :func:`normalize_color` for the colorant, so HP's colour
    handling matches the brand-agnostic path. Type is derived from name
    keywords: a "Black Drum" is a drum (not toner), a "Fuser" is a fuser, a
    bare "Black"/"Cyan"/... is a toner cartridge.

    NOTE FOR INTEGRATION: pairs with :func:`decode_supply_text`; same dedup
    note -- fold into the shared decode foundation on merge.
    """
    color = normalize_color(description)
    if not description:
        return None, color
    low = description.strip().lower()
    for keyword, supply_type in _TYPE_KEYWORDS:
        if keyword in low:
            return supply_type, color
    # A bare colour name with no part keyword (e.g. "Black", "Cyan") is the
    # toner cartridge on a laser MFP.
    if color is not None:
        return "toner", color
    return None, color


class HPProvider(PrinterProvider):
    name = "hp"
    # Match the HP printers subtree specifically, not the whole HP enterprise --
    # HP makes a lot of non-printer hardware, and the generic "11" prefix would
    # over-match. ``11.2.3.9`` is the documented hpPrinters branch.
    enterprise_prefixes = ("11.2.3.9",)

    def _normalize_supplies(self, reading: dict) -> None:
        """Decode hex supply names + re-classify weak (type, color) in place.

        Operates on the supply dicts the standard poller already built from
        ``prtMarkerSupplies``. We never downgrade good data: a supply whose
        type the standard MIB already nailed (anything but "other") keeps it;
        we only fill type when it's missing/"other", and only fill color when
        it's currently empty. The description is always replaced with the
        decoded text when it was a hex blob.
        """
        for supply in reading.get("supplies", []):
            raw_desc = supply.get("description")
            decoded = decode_supply_text(raw_desc)
            if decoded and decoded != raw_desc:
                supply["description"] = decoded
            name = supply.get("description")

            inferred_type, inferred_color = classify_supply(name)

            cur_type = supply.get("type")
            if inferred_type and (not cur_type or cur_type == "other"):
                supply["type"] = inferred_type

            if not supply.get("color") and inferred_color:
                supply["color"] = inferred_color

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

        # Clean up supply names/types/colors from the standard-MIB rows. This
        # needs no extra SNMP and works even when the private-MIB GET below
        # fails, so a printer that only answers the standard tables still gets
        # readable, correctly-typed supplies.
        self._normalize_supplies(reading)

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
