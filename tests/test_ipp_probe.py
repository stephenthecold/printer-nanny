"""IPP capability probe: encoding, defensive parsing, and driver classification.

The classification is the point: it decides whether a workstation queue needs a
driver installed at all. Getting it wrong in the permissive direction produces a
queue that silently cannot print; getting it wrong in the strict direction sends
a technician to install a driver that was never needed -- which is the exact
setup friction this feature exists to remove.
"""

from __future__ import annotations

import struct

import pytest

from printer_nanny_agent import ipp


# --- helpers to synthesise responses ---------------------------------------


def _attr(tag: int, name: str, value: bytes) -> bytes:
    n = name.encode()
    return struct.pack("!B H", tag, len(n)) + n + struct.pack("!H", len(value)) + value


def _additional(tag: int, value: bytes) -> bytes:
    return struct.pack("!B H", tag, 0) + struct.pack("!H", len(value)) + value


def make_response(**kwargs) -> bytes:
    """Build a printer-attributes response from {name: [str values]}."""
    out = bytearray()
    out += struct.pack("!BB H I", 2, 0, 0x0000, 1)  # version, status ok, request id
    out.append(ipp.TAG_PRINTER_ATTRIBUTES)
    for name, values in kwargs.items():
        name = name.replace("_", "-")
        for i, v in enumerate(values):
            if isinstance(v, int):
                tag, encoded = ipp.TAG_INTEGER, struct.pack("!i", v)
            else:
                tag, encoded = ipp.TAG_KEYWORD, v.encode()
            out += _attr(tag, name, encoded) if i == 0 else _additional(tag, encoded)
    out.append(ipp.TAG_END_OF_ATTRIBUTES)
    return bytes(out)


# --- request encoding -------------------------------------------------------


def test_request_is_well_formed():
    body = ipp.build_get_printer_attributes("ipp://10.0.0.5:631/ipp/print")
    major, minor, op, req_id = struct.unpack("!BB H I", body[:8])
    assert (major, minor) == (2, 0)
    assert op == ipp.OP_GET_PRINTER_ATTRIBUTES
    assert req_id == 1
    assert body[8] == ipp.TAG_OPERATION_ATTRIBUTES
    assert body[-1] == ipp.TAG_END_OF_ATTRIBUTES


def test_charset_and_language_come_first_and_in_order():
    """Some devices reject the request outright if this order is wrong."""
    body = ipp.build_get_printer_attributes("ipp://x/ipp/print")
    charset = body.index(b"attributes-charset")
    language = body.index(b"attributes-natural-language")
    uri = body.index(b"printer-uri")
    assert charset < language < uri


def test_requested_attributes_use_additional_value_encoding():
    """Repeated values must use a zero-length name, not a repeated name."""
    body = ipp.build_get_printer_attributes("ipp://x/ipp/print")
    assert body.count(b"requested-attributes") == 1
    for attr in ("document-format-supported", "ipp-versions-supported"):
        assert attr.encode() in body


# --- parsing ----------------------------------------------------------------


def test_parses_single_and_multi_valued_attributes():
    data = make_response(
        printer_make_and_model=["Acme LaserJet 9000"],
        ipp_versions_supported=["1.1", "2.0"],
    )
    attrs = ipp.parse_response(data)
    assert attrs["printer-make-and-model"] == ["Acme LaserJet 9000"]
    assert attrs["ipp-versions-supported"] == ["1.1", "2.0"]


def test_parses_integers_and_booleans():
    out = bytearray(struct.pack("!BB H I", 2, 0, 0, 1))
    out.append(ipp.TAG_PRINTER_ATTRIBUTES)
    out += _attr(ipp.TAG_ENUM, "printer-state", struct.pack("!i", 3))
    out += _attr(ipp.TAG_BOOLEAN, "mopria-certified", b"\x01")
    out.append(ipp.TAG_END_OF_ATTRIBUTES)
    attrs = ipp.parse_response(bytes(out))
    assert attrs["printer-state"] == [3]
    assert attrs["mopria-certified"] == [True]


def test_operation_attributes_are_not_collected():
    """Only the printer-attributes group describes the device."""
    out = bytearray(struct.pack("!BB H I", 2, 0, 0, 1))
    out.append(ipp.TAG_OPERATION_ATTRIBUTES)
    out += _attr(ipp.TAG_CHARSET, "attributes-charset", b"utf-8")
    out.append(ipp.TAG_PRINTER_ATTRIBUTES)
    out += _attr(ipp.TAG_KEYWORD, "printer-name", b"lobby")
    out.append(ipp.TAG_END_OF_ATTRIBUTES)
    attrs = ipp.parse_response(bytes(out))
    assert "attributes-charset" not in attrs
    assert attrs["printer-name"] == ["lobby"]


# --- hostile / malformed input ---------------------------------------------
#
# Every length on the wire is controlled by the device. A printer on a customer
# LAN must not be able to crash or hang the agent by answering strangely.


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x02\x00",
        b"\x02\x00\x00\x00\x00\x00\x00",           # header truncated
        b"\x02\x00\x00\x00\x00\x00\x00\x01\x04",   # group tag then nothing
        bytes([2, 0, 0, 0, 0, 0, 0, 1, 4, 0x44, 0xFF, 0xFF]),  # name len past end
    ],
)
def test_truncated_input_never_raises(data):
    assert ipp.parse_response(data) == {} or isinstance(ipp.parse_response(data), dict)


def test_value_length_beyond_buffer_is_not_trusted():
    """A 64KB declared value in a 20-byte packet must not over-read."""
    out = bytearray(struct.pack("!BB H I", 2, 0, 0, 1))
    out.append(ipp.TAG_PRINTER_ATTRIBUTES)
    name = b"printer-name"
    out += struct.pack("!B H", ipp.TAG_KEYWORD, len(name)) + name
    out += struct.pack("!H", 0xFFFF)  # claims 65535 bytes
    out += b"short"
    attrs = ipp.parse_response(bytes(out))
    assert attrs == {}  # nothing decoded, no exception, no over-read


def test_attribute_count_is_capped():
    out = bytearray(struct.pack("!BB H I", 2, 0, 0, 1))
    out.append(ipp.TAG_PRINTER_ATTRIBUTES)
    for i in range(ipp.MAX_ATTRIBUTES + 200):
        out += _attr(ipp.TAG_KEYWORD, "attr-{}".format(i), b"v")
    out.append(ipp.TAG_END_OF_ATTRIBUTES)
    assert len(ipp.parse_response(bytes(out))) <= ipp.MAX_ATTRIBUTES


def test_repeated_values_are_capped():
    out = bytearray(struct.pack("!BB H I", 2, 0, 0, 1))
    out.append(ipp.TAG_PRINTER_ATTRIBUTES)
    out += _attr(ipp.TAG_KEYWORD, "media-supported", b"v0")
    for _ in range(ipp.MAX_VALUES_PER_ATTRIBUTE + 50):
        out += _additional(ipp.TAG_KEYWORD, b"v")
    out.append(ipp.TAG_END_OF_ATTRIBUTES)
    attrs = ipp.parse_response(bytes(out))
    assert len(attrs["media-supported"]) <= ipp.MAX_VALUES_PER_ATTRIBUTE


def test_invalid_utf8_does_not_raise():
    out = bytearray(struct.pack("!BB H I", 2, 0, 0, 1))
    out.append(ipp.TAG_PRINTER_ATTRIBUTES)
    out += _attr(ipp.TAG_NAME, "printer-info", b"\xff\xfe bad bytes")
    out.append(ipp.TAG_END_OF_ATTRIBUTES)
    attrs = ipp.parse_response(bytes(out))
    assert isinstance(attrs["printer-info"][0], str)


# --- classification ---------------------------------------------------------


def test_modern_mopria_device_is_driverless():
    probe = ipp.classify(
        ipp.parse_response(
            make_response(
                ipp_versions_supported=["1.1", "2.0"],
                document_format_supported=["application/pdf", "image/pwg-raster"],
            )
        )
    )
    assert probe.status == ipp.STATUS_DRIVERLESS
    assert probe.driverless
    assert "image/pwg-raster" in probe.reason


def test_ipp_1_1_only_device_needs_a_driver():
    """The inbox class driver requires IPP/2.0; 1.1-only devices predate it."""
    probe = ipp.classify(
        ipp.parse_response(
            make_response(
                ipp_versions_supported=["1.1"],
                document_format_supported=["image/pwg-raster"],
            )
        )
    )
    assert probe.status == ipp.STATUS_DRIVER_REQUIRED
    assert "1.1" in probe.reason


def test_vendor_pdl_only_device_needs_a_driver():
    probe = ipp.classify(
        ipp.parse_response(
            make_response(
                ipp_versions_supported=["2.0"],
                document_format_supported=["application/octet-stream", "application/vnd.hp-PCL"],
            )
        )
    )
    assert probe.status == ipp.STATUS_DRIVER_REQUIRED
    assert "page description language" in probe.reason


def test_pdf_without_pwg_raster_still_counts_as_driverless_but_says_so():
    """Off the strict Mopria path, driven by the class driver in practice."""
    probe = ipp.classify(
        ipp.parse_response(
            make_response(
                ipp_versions_supported=["2.0"],
                document_format_supported=["application/pdf"],
            )
        )
    )
    assert probe.status == ipp.STATUS_DRIVERLESS
    assert "no image/pwg-raster advertised" in probe.reason


def test_ipp_2_1_and_2_2_qualify():
    for version in ("2.1", "2.2"):
        probe = ipp.classify(
            ipp.parse_response(
                make_response(
                    ipp_versions_supported=[version],
                    document_format_supported=["image/pwg-raster"],
                )
            )
        )
        assert probe.driverless, version


def test_empty_advertisement_is_an_error_not_a_driver_verdict():
    """Answering IPP with nothing useful is a broken device, not a Tier 3 one."""
    probe = ipp.classify(ipp.parse_response(make_response(printer_name=["x"])))
    assert probe.status == ipp.STATUS_ERROR


def test_format_matching_is_case_insensitive_and_whitespace_tolerant():
    probe = ipp.classify(
        ipp.parse_response(
            make_response(
                ipp_versions_supported=["2.0"],
                document_format_supported=["  IMAGE/PWG-RASTER  "],
            )
        )
    )
    assert probe.driverless


# --- transport outcomes -----------------------------------------------------


def test_connection_refused_reports_ipp_disabled_not_driver_required(monkeypatch):
    """A refused port means IPP is switched off -- a printer setting, not a driver.

    Conflating the two sends a technician to install a driver when the actual
    fix is a checkbox in the device's web UI.
    """
    import httpx

    def boom(*a, **kw):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    monkeypatch.setattr(httpx, "post", boom)
    probe = ipp.probe("10.0.0.9", timeout=0.01)
    assert probe.status == ipp.STATUS_IPP_DISABLED
    assert "disabled" in probe.reason
    assert "driver" in probe.reason  # explicitly tells the reader it is not one


def test_timeout_reports_unreachable(monkeypatch):
    import httpx

    def boom(*a, **kw):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(httpx, "post", boom)
    probe = ipp.probe("10.0.0.9", timeout=0.01)
    assert probe.status == ipp.STATUS_UNREACHABLE


def test_probe_tries_alternate_paths_before_giving_up(monkeypatch):
    """Answering only on /ipp/printer must not be misread as 'needs a driver'."""
    import httpx

    seen = []

    class Resp:
        def __init__(self, status, content):
            self.status_code = status
            self.content = content

    def fake_post(url, **kw):
        seen.append(url)
        if url.endswith("/ipp/printer"):
            return Resp(200, make_response(
                ipp_versions_supported=["2.0"],
                document_format_supported=["image/pwg-raster"],
            ))
        return Resp(404, b"")

    monkeypatch.setattr(httpx, "post", fake_post)
    probe = ipp.probe("10.0.0.9")
    assert probe.driverless
    assert probe.endpoint.endswith("/ipp/printer")
    assert any(u.endswith("/ipp/print") for u in seen), "should try the standard path first"


def test_probe_never_raises_on_unexpected_transport_error(monkeypatch):
    import httpx

    def boom(*a, **kw):
        raise RuntimeError("something exotic")

    monkeypatch.setattr(httpx, "post", boom)
    assert ipp.probe("10.0.0.9").status == ipp.STATUS_UNREACHABLE


def test_as_dict_is_json_safe():
    probe = ipp.classify(
        ipp.parse_response(
            make_response(
                printer_make_and_model=["Acme 9000"],
                ipp_versions_supported=["2.0"],
                document_format_supported=["image/pwg-raster"],
                sides_supported=["one-sided", "two-sided-long-edge"],
            )
        )
    )
    import json

    payload = json.dumps(probe.as_dict())
    assert "Acme 9000" in payload
    assert "two-sided-long-edge" in payload
