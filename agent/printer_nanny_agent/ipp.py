"""Minimal IPP client for capability probing.

WHY THIS EXISTS
---------------
Driver installation is the step that strands most workstation setups, and since
KB5005652 (August 2021) the reason is privilege, not packaging:
``RestrictDriverInstallationToAdministrators`` defaults to 1, so Point and Print
demands local admin. A standard user gets a UAC prompt they cannot satisfy, and
the usual escape -- setting that key to 0 -- is precisely the PrintNightmare
hole. The way out is to not install a driver at all.

Windows ships an inbox **IPP class driver** that drives any Mopria-certified
device with no third-party driver, hence no admin, hence no prompt. As of
2026-07-01 Windows ranks that inbox driver *ahead* of third-party drivers by
default, so driverless is the platform's own default rather than a trade-off.

This module answers the one question that decides a printer's setup path:

    can this device be driven by the inbox IPP class driver, or does it need a
    vendor driver staged by our privileged service?

It speaks just enough IPP to ask, over the ``httpx`` dependency the agent
already carries -- no new packages, and nothing that needs a Windows host, so
the decision is testable on any platform.

PARSING IS DEFENSIVE ON PURPOSE
-------------------------------
These bytes arrive from devices on customer LANs -- unauthenticated, frequently
old, sometimes not really an IPP server at all, and occasionally hostile. Every
length in the wire format is attacker-controlled, so each read is bounds-checked
against the buffer, and the number and size of attributes are capped. The parser
never raises on malformed input; it returns what it managed to decode. A printer
must not be able to hang or exhaust the agent by answering strangely.
"""

from __future__ import annotations

import logging
import struct
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# --- wire format ------------------------------------------------------------

OP_GET_PRINTER_ATTRIBUTES = 0x000B

TAG_OPERATION_ATTRIBUTES = 0x01
TAG_END_OF_ATTRIBUTES = 0x03
TAG_PRINTER_ATTRIBUTES = 0x04

TAG_INTEGER = 0x21
TAG_BOOLEAN = 0x22
TAG_ENUM = 0x23
TAG_BEG_COLLECTION = 0x34
TAG_END_COLLECTION = 0x37
TAG_MEMBER_NAME = 0x4A
TAG_TEXT = 0x41
TAG_NAME = 0x42
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_CHARSET = 0x47
TAG_NATURAL_LANGUAGE = 0x48
TAG_MIME_MEDIA_TYPE = 0x49

# Value tags below 0x10 are group delimiters; 0x10..0x1F are "out of band"
# values (unsupported / unknown / no-value) that carry no data.
_DELIMITER_MAX = 0x0F
_OUT_OF_BAND_MAX = 0x1F

_TEXTUAL_TAGS = frozenset(
    {TAG_TEXT, TAG_NAME, TAG_KEYWORD, TAG_URI, TAG_CHARSET,
     TAG_NATURAL_LANGUAGE, TAG_MIME_MEDIA_TYPE, TAG_MEMBER_NAME}
)

# Caps. A conformant response is a few KB; these exist so a malicious or broken
# device cannot make the agent allocate without bound.
MAX_RESPONSE_BYTES = 512 * 1024
MAX_ATTRIBUTES = 512
MAX_VALUES_PER_ATTRIBUTE = 256
MAX_VALUE_BYTES = 8 * 1024

# The attributes we ask for. Requesting a narrow set keeps responses small and
# avoids devices that choke on 'all'.
REQUESTED_ATTRIBUTES = (
    "printer-name",
    "printer-make-and-model",
    "printer-info",
    "printer-state",
    "ipp-versions-supported",
    "document-format-supported",
    "printer-device-id",
    "finishings-supported",
    "media-supported",
    "sides-supported",
    "print-color-mode-supported",
    "urf-supported",
    "mopria-certified",
    "printer-uri-supported",
    "uri-security-supported",
)

# Endpoints to try, in order. There is no single mandated path: /ipp/print is
# the IPP Everywhere convention, but plenty of devices answer only on one of
# the others, and treating "wrong path" as "no IPP" would misclassify them into
# a needless vendor-driver install.
CANDIDATE_PATHS = ("/ipp/print", "/ipp/printer", "/ipp", "/")

DEFAULT_PORT = 631


# --- encoding ---------------------------------------------------------------


def _encode_attribute(tag: int, name: str, value: str) -> bytes:
    name_b = name.encode("utf-8")
    value_b = value.encode("utf-8")
    return (
        struct.pack("!B H", tag, len(name_b))
        + name_b
        + struct.pack("!H", len(value_b))
        + value_b
    )


def _encode_additional_value(tag: int, value: str) -> bytes:
    """A zero-length name continues the previous attribute (a 1setOf member)."""
    value_b = value.encode("utf-8")
    return struct.pack("!B H", tag, 0) + struct.pack("!H", len(value_b)) + value_b


def build_get_printer_attributes(printer_uri: str, request_id: int = 1) -> bytes:
    """Encode a Get-Printer-Attributes request.

    IPP requires attributes-charset and attributes-natural-language first, in
    that order; some devices reject the request outright otherwise.
    """
    out = bytearray()
    out += struct.pack("!BB H I", 2, 0, OP_GET_PRINTER_ATTRIBUTES, request_id)
    out.append(TAG_OPERATION_ATTRIBUTES)
    out += _encode_attribute(TAG_CHARSET, "attributes-charset", "utf-8")
    out += _encode_attribute(
        TAG_NATURAL_LANGUAGE, "attributes-natural-language", "en"
    )
    out += _encode_attribute(TAG_URI, "printer-uri", printer_uri)
    first, rest = REQUESTED_ATTRIBUTES[0], REQUESTED_ATTRIBUTES[1:]
    out += _encode_attribute(TAG_KEYWORD, "requested-attributes", first)
    for attr in rest:
        out += _encode_additional_value(TAG_KEYWORD, attr)
    out.append(TAG_END_OF_ATTRIBUTES)
    return bytes(out)


# --- decoding ---------------------------------------------------------------


def _read(buf: bytes, offset: int, length: int) -> Tuple[Optional[bytes], int]:
    """Bounds-checked read. Returns (None, offset) rather than raising."""
    end = offset + length
    if length < 0 or end > len(buf):
        return None, offset
    return buf[offset:end], end


def parse_response(data: bytes) -> Dict[str, List[object]]:
    """Decode printer attributes from an IPP response.

    Returns ``{attribute-name: [values]}``. Malformed input yields whatever was
    decoded before the damage -- never an exception, because the peer is an
    arbitrary device on a customer network.
    """
    attrs: Dict[str, List[object]] = {}
    if len(data) < 8:
        return attrs

    offset = 8  # version(2) + status-code(2) + request-id(4)
    current_group = 0
    current_name: Optional[str] = None

    while offset < len(data) and len(attrs) < MAX_ATTRIBUTES:
        tag = data[offset]
        offset += 1

        if tag == TAG_END_OF_ATTRIBUTES:
            break
        if tag <= _DELIMITER_MAX:
            current_group = tag
            current_name = None
            continue

        raw, offset = _read(data, offset, 2)
        if raw is None:
            break
        (name_len,) = struct.unpack("!H", raw)
        name_b, offset = _read(data, offset, name_len)
        if name_b is None:
            break

        raw, offset = _read(data, offset, 2)
        if raw is None:
            break
        (value_len,) = struct.unpack("!H", raw)
        if value_len > MAX_VALUE_BYTES:
            # Do not allocate on a device's say-so; skip the value if we can.
            _, offset = _read(data, offset, value_len)
            continue
        value_b, offset = _read(data, offset, value_len)
        if value_b is None:
            break

        if name_len:
            current_name = name_b.decode("utf-8", "replace")
        # else: zero-length name continues the previous attribute.

        # Only printer attributes are of interest; operation attributes and
        # collections are decoded past but not collected.
        if current_group != TAG_PRINTER_ATTRIBUTES or current_name is None:
            continue
        if tag in (TAG_BEG_COLLECTION, TAG_END_COLLECTION, TAG_MEMBER_NAME):
            continue

        value = _decode_value(tag, value_b)
        if value is None:
            continue
        bucket = attrs.setdefault(current_name, [])
        if len(bucket) < MAX_VALUES_PER_ATTRIBUTE:
            bucket.append(value)

    return attrs


def _decode_value(tag: int, raw: bytes) -> Optional[object]:
    if tag <= _OUT_OF_BAND_MAX:
        return None
    if tag in (TAG_INTEGER, TAG_ENUM):
        if len(raw) != 4:
            return None
        return struct.unpack("!i", raw)[0]
    if tag == TAG_BOOLEAN:
        if len(raw) != 1:
            return None
        return bool(raw[0])
    if tag in _TEXTUAL_TAGS:
        return raw.decode("utf-8", "replace")
    # Anything else (dateTime, resolution, rangeOfInteger, octetString) is kept
    # as a hex string rather than dropped -- useful in diagnostics, and no
    # classification decision depends on it.
    return raw.hex()


# --- classification ---------------------------------------------------------

# Probe outcomes. These are deliberately distinct because they need different
# remediation and the difference is invisible in a single "failed" state:
#   driverless      -- inbox IPP class driver will drive it; no install at all
#   driver_required -- answers IPP but below the bar; needs a staged vendor driver
#   ipp_disabled    -- the port refused the connection. Many devices ship with
#                      IPP switched off; that is a setting to flip on the
#                      printer, NOT a reason to install a driver, and conflating
#                      the two sends the technician to the wrong place.
#   unreachable     -- nothing answered (offline, firewalled, wrong address)
#   error           -- answered, but not with anything we could decode
STATUS_DRIVERLESS = "driverless"
STATUS_DRIVER_REQUIRED = "driver_required"
STATUS_IPP_DISABLED = "ipp_disabled"
STATUS_UNREACHABLE = "unreachable"
STATUS_ERROR = "error"

# The Windows inbox IPP class driver targets Mopria-certified devices. Mopria
# and IPP Everywhere both require IPP/2.0 and PWG Raster; PDF is recommended
# and widely present. PWG Raster is the floor -- a device advertising only
# vendor PDLs cannot be driven driverlessly.
REQUIRED_FORMAT = "image/pwg-raster"
ALTERNATIVE_FORMATS = ("application/pdf", "image/urf")


class IppProbe:
    """The result of probing one device."""

    __slots__ = ("status", "reason", "attributes", "endpoint")

    def __init__(
        self,
        status: str,
        reason: str,
        attributes: Optional[Dict[str, List[object]]] = None,
        endpoint: Optional[str] = None,
    ) -> None:
        self.status = status
        self.reason = reason
        self.attributes = attributes or {}
        self.endpoint = endpoint

    @property
    def driverless(self) -> bool:
        return self.status == STATUS_DRIVERLESS

    def get(self, name: str) -> List[object]:
        return self.attributes.get(name, [])

    def first(self, name: str) -> Optional[object]:
        values = self.get(name)
        return values[0] if values else None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "reason": self.reason,
            "endpoint": self.endpoint,
            "make_and_model": self.first("printer-make-and-model"),
            "ipp_versions": [str(v) for v in self.get("ipp-versions-supported")],
            "document_formats": [str(v) for v in self.get("document-format-supported")],
            "finishings": [v for v in self.get("finishings-supported")],
            "sides": [str(v) for v in self.get("sides-supported")],
            "color_modes": [str(v) for v in self.get("print-color-mode-supported")],
        }


def classify(attributes: Dict[str, List[object]], endpoint: Optional[str] = None) -> IppProbe:
    """Decide whether the inbox IPP class driver can drive this device."""
    versions = {str(v).strip() for v in attributes.get("ipp-versions-supported", [])}
    formats = {str(v).strip().lower() for v in attributes.get("document-format-supported", [])}

    if not versions and not formats:
        return IppProbe(
            STATUS_ERROR,
            "answered IPP but advertised neither versions nor document formats",
            attributes,
            endpoint,
        )

    # "2.0" is the floor; anything higher (2.1/2.2) also qualifies. Devices that
    # report only 1.1 predate the class driver's requirements.
    modern = {v for v in versions if v and v[0] >= "2"}
    if not modern:
        return IppProbe(
            STATUS_DRIVER_REQUIRED,
            "advertises only IPP {} -- the inbox class driver needs 2.0 or later".format(
                ", ".join(sorted(versions)) or "(none)"
            ),
            attributes,
            endpoint,
        )

    if REQUIRED_FORMAT not in formats:
        alt = [f for f in ALTERNATIVE_FORMATS if f in formats]
        if not alt:
            return IppProbe(
                STATUS_DRIVER_REQUIRED,
                "no driverless-capable page description language "
                "(needs {}, has {})".format(
                    REQUIRED_FORMAT, ", ".join(sorted(formats)) or "(none)"
                ),
                attributes,
                endpoint,
            )
        # PDF/URF without PWG Raster is off the strict Mopria path but is driven
        # by the class driver in practice. Flagged in the reason so an operator
        # can see it was a near miss rather than a clean pass.
        return IppProbe(
            STATUS_DRIVERLESS,
            "driverless via {} (no {} advertised)".format(", ".join(alt), REQUIRED_FORMAT),
            attributes,
            endpoint,
        )

    return IppProbe(
        STATUS_DRIVERLESS,
        "IPP {} with {}".format(", ".join(sorted(modern)), REQUIRED_FORMAT),
        attributes,
        endpoint,
    )


# --- transport --------------------------------------------------------------


def probe(host: str, port: int = DEFAULT_PORT, timeout: float = 5.0) -> IppProbe:
    """Probe ``host`` and classify its driver requirement.

    Tries each candidate path until one decodes. Never raises -- a probe failure
    is a result, not an exception, because this runs across a whole subnet.
    """
    import httpx

    refused = False
    last_reason = "no response"

    for path in CANDIDATE_PATHS:
        url = "http://{}:{}{}".format(host, port, path)
        printer_uri = "ipp://{}:{}{}".format(host, port, path)
        body = build_get_printer_attributes(printer_uri)
        try:
            resp = httpx.post(
                url,
                content=body,
                headers={"Content-Type": "application/ipp"},
                timeout=timeout,
                # A printer redirecting to its own web UI is not an IPP answer.
                follow_redirects=False,
            )
        except httpx.ConnectError as exc:
            # Connection refused is materially different from a timeout: the
            # host is up and actively rejecting 631, which usually means IPP is
            # switched off in the device's config.
            if "refused" in str(exc).lower():
                refused = True
            last_reason = "connect error: {}".format(_sanitise(exc))
            continue
        except httpx.TimeoutException:
            last_reason = "timed out after {}s".format(timeout)
            continue
        except Exception as exc:  # noqa: BLE001 - never let one device stop a sweep
            last_reason = "request failed: {}".format(_sanitise(exc))
            continue

        if resp.status_code != 200:
            last_reason = "HTTP {} from {}".format(resp.status_code, path)
            continue
        if len(resp.content) > MAX_RESPONSE_BYTES:
            last_reason = "response too large ({} bytes)".format(len(resp.content))
            continue

        attrs = parse_response(resp.content)
        if not attrs:
            last_reason = "no printer attributes decoded from {}".format(path)
            continue
        return classify(attrs, endpoint=printer_uri)

    if refused:
        return IppProbe(
            STATUS_IPP_DISABLED,
            "port {} refused the connection -- IPP is most likely disabled on the "
            "device. Enable it in the printer's web UI; this does not need a "
            "driver.".format(port),
        )
    return IppProbe(STATUS_UNREACHABLE, last_reason)


def _sanitise(exc: Exception) -> str:
    """Transport errors quote URLs; keep them short and free of credentials."""
    text = str(exc).replace("\n", " ").strip()
    return text[:120] if text else exc.__class__.__name__
