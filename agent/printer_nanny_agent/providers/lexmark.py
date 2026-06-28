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
* **Name / type / color the supplies properly.** Lexmark labels its supply
  rows with good human strings ("Black Cartridge", "Imaging Unit", "Fuser
  Maintenance Kit", "Waste Toner Bottle"), but two things break the generic
  poller's naming: (a) some Lexmark firmware returns
  ``prtMarkerSuppliesDescription`` as a binary OCTET STRING, which pysnmp
  renders as a raw ``0x…`` hex string -- that leaks into the dashboard verbatim
  ("0x426c61636b" instead of "Black"); and (b) the standard
  ``prtMarkerSuppliesType`` code on Lexmark is frequently the generic
  ``other(1)`` even for a real toner/drum/fuser, so the generic classifier
  drops everything to SupplyType "other" with no color. This provider decodes
  the hex back to text and re-classifies each supply from its (now readable)
  description, mapping Lexmark's vocabulary onto our SupplyType + color
  taxonomy. It only *upgrades*: a supply the standard MIB already typed
  correctly, or a color already set upstream, is never downgraded.
* Tag the reading with ``_supply_precision = "lexmark_native"`` so the UI
  can show a positive "real percentages from device" badge instead of the
  generic Printer-MIB question marks.

OIDs come from Lexmark's published Print MIB. The provider is fully
defensive: a printer that exposes only the standard MIB still works, and
this layer just adds a brand tag + supply precision label. No private-MIB
supply OIDs are invented here -- the supply rows are exactly the standard
``prtMarkerSupplies*`` rows the poller already walked; we only clean up their
description text and (type, color) classification.

Refs:
 * Lexmark enterprise OID: 1.3.6.1.4.1.641
 * Lexmark Print MIB: lexPrinterMIB at .6
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

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


def decode_supply_text(value: Optional[str]) -> Optional[str]:
    """Render a supply description into readable text.

    pysnmp renders a non-printable / binary OCTET STRING as a ``0x…`` hex
    string -- e.g. a Lexmark ``prtMarkerSuppliesDescription`` of "Black" comes
    back as ``0x426c61636b``. That hex leaks straight onto the dashboard as the
    supply name. This decodes such a hex string back to its UTF-8/Latin-1 text
    when it round-trips to something readable, and otherwise returns the
    already-readable input unchanged.

    Inline-equivalent of the decode-foundation sibling's
    ``snmp_parse.decode_supply_text``; kept self-contained so the Lexmark
    provider works whether or not that shared helper has landed in the tree
    yet. See integration_notes.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    if text.lower().startswith("0x"):
        hexpart = text[2:].replace(" ", "")
        if hexpart and len(hexpart) % 2 == 0:
            try:
                raw = bytes.fromhex(hexpart)
            except ValueError:
                return text
            decoded = _decode_bytes_as_label(raw)
            # Only accept the decode when it round-trips to a real label;
            # otherwise the field genuinely was binary and we leave the
            # original ``0x…`` string alone rather than emit mojibake.
            return decoded if decoded is not None else text
        return text

    return text


def _decode_bytes_as_label(raw: bytes) -> Optional[str]:
    """Decode SNMP OCTET-STRING bytes to a printable label, or None.

    A printer supply description is ASCII text ("Black", "Fuser Maintenance
    Kit"). We accept UTF-8 (covers ASCII) and require the result be
    overwhelmingly printable ASCII -- high/control bytes mean the field was
    really binary (a status bitmap, a serial blob), and turning 0xfffe into
    "ÿþ" would be worse than leaving the raw hex. Returns the trimmed text on
    success, else None.
    """
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    decoded = decoded.replace("\x00", "").strip()
    if not decoded:
        return None
    if not _mostly_printable_ascii(decoded):
        return None
    return decoded


def _mostly_printable_ascii(text: str) -> bool:
    """True when ``text`` is overwhelmingly printable 7-bit ASCII (a real label).

    Allows a little slack for the odd accented char in a model name, but a
    string dominated by non-ASCII/control bytes is treated as binary, not text.
    """
    if not text:
        return False
    good = sum(1 for ch in text if 0x20 <= ord(ch) <= 0x7E)
    return good / len(text) >= 0.8


# Lexmark's supply vocabulary -> (SupplyType, requires_color). Ordered most
# specific first so "Imaging Unit" / "Photoconductor" win over a bare match.
# The strings are matched case-insensitively as substrings of the (decoded)
# description. SupplyType strings match central's taxonomy:
#   toner / waste / drum / developer / fuser / ink / staples / other
_LEXMARK_SUPPLY_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    # Waste -- must come before "toner" so "Waste Toner Bottle" -> waste.
    ("waste toner", "waste"),
    ("waste bottle", "waste"),
    ("waste container", "waste"),
    ("waste", "waste"),
    # Drum / imaging unit / photoconductor (Lexmark's name for the OPC).
    ("imaging unit", "drum"),
    ("imaging kit", "drum"),
    ("photoconductor", "drum"),
    ("drum", "drum"),
    # Fuser.
    ("fuser", "fuser"),
    # Developer.
    ("developer", "developer"),
    # Maintenance kit / transfer (consumable bundles -- no single SupplyType,
    # surface as "other" but with a readable name preserved).
    ("maintenance kit", "other"),
    ("maintenance", "other"),
    ("transfer", "other"),
    ("separator", "other"),
    ("pick roller", "other"),
    # Toner / cartridge -- broadest; comes last so the kit/drum/waste rows above
    # win first. A "Black Cartridge" / "Cyan Toner" lands here -> toner.
    ("toner", "toner"),
    ("cartridge", "toner"),
)

# Color keywords (substring, case-insensitive). Kept local so the provider is
# self-contained; mirrors snmp_parse._COLOR_KEYWORDS' intent.
_COLOR_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ("black", "black"),
    ("cyan", "cyan"),
    ("magenta", "magenta"),
    ("yellow", "yellow"),
)


def classify_supply(description: Optional[str]) -> Tuple[str, Optional[str]]:
    """Map a (decoded) Lexmark supply description to ``(SupplyType, color)``.

    Returns ``("other", None)`` when nothing matches -- the caller keeps the
    readable description either way. Inline-equivalent of the decode-foundation
    sibling's ``snmp_parse.classify_supply``; see integration_notes.
    """
    if not description:
        return ("other", None)
    low = description.lower()

    matched_type = False
    supply_type = "other"
    for keyword, mapped in _LEXMARK_SUPPLY_KEYWORDS:
        if keyword in low:
            supply_type = mapped
            matched_type = True
            break

    color: Optional[str] = None
    for keyword, mapped in _COLOR_KEYWORDS:
        if keyword in low:
            color = mapped
            break

    if not matched_type and color is not None:
        # A bare colorant name with no other supply keyword -- e.g. the real
        # "0x426c61636b" => "Black" / "Cyan" rows -- is a toner cartridge.
        # (This is the common HP/Lexmark form where the description is just the
        # color word.) Map it to toner so it's not stranded as "other".
        supply_type = "toner"
    elif supply_type == "other" and color is not None:
        # A black-only colorant on a fuser/drum/waste row isn't a "color" in the
        # toner sense, but Lexmark's "Black Drum" / "Black Imaging Unit"
        # genuinely identify the K channel's drum -- keep the color so the
        # dashboard can pair it with the K toner. Only toner-less *bundle* rows
        # (maintenance/transfer kits) drop the color, since "Black Maintenance
        # Kit" doesn't exist.
        if any(k in low for k in ("maintenance", "transfer", "kit")):
            color = None
    return (supply_type, color)


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

        # Clean up the supply rows the standard poller built. Lexmark firmware
        # sometimes returns the description as a binary OCTET STRING (pysnmp ->
        # "0x…") and frequently reports prtMarkerSuppliesType as generic
        # other(1), so the generic classifier drops real toner/drum/fuser rows
        # to "other" with no color and a hex name. Decode + re-classify here.
        self._clean_supplies(reading)

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

    @staticmethod
    def _clean_supplies(reading: dict) -> None:
        """Decode + re-classify each supply row in place.

        Decodes any ``0x…`` hex description to readable text, then re-derives
        ``type`` / ``color`` from that text. Only *upgrades*: a row already
        typed as something other than "other" by the standard MIB keeps its
        type, and an existing color is never cleared.
        """
        for supply in reading.get("supplies", []) or []:
            readable = decode_supply_text(supply.get("description"))
            if readable is not None:
                supply["description"] = readable

            mapped_type, mapped_color = classify_supply(readable)

            current_type = supply.get("type")
            # Upgrade "other"/missing to a specific type when we recognize the
            # name. Never downgrade a type the standard MIB already nailed.
            if mapped_type != "other" and current_type in (None, "other", ""):
                supply["type"] = mapped_type

            # Fill color only when not already set upstream.
            if mapped_color and not supply.get("color"):
                supply["color"] = mapped_color


register(LexmarkProvider())
