"""Agent discovery: host enumeration and subnet sweep filtering."""

from __future__ import annotations

from printer_nanny_agent import oids
from printer_nanny_agent.discovery import _vendor_from, discover_subnet, hosts_in
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend, canned_printer


def test_hosts_in_cidr():
    assert hosts_in("10.0.0.0/30") == ["10.0.0.1", "10.0.0.2"]
    assert len(hosts_in("192.168.1.0/24")) == 254


async def test_discover_only_returns_printers():
    backend = FakeSnmpBackend()
    # A real printer (has fingerprint).
    backend.add("10.0.0.1", canned_printer(name="hp-1"))
    # A non-printer SNMP device: answers sysDescr but no printer fingerprint
    # and no vendor name in sysDescr → must not be classified as a printer.
    backend.devices["10.0.0.2"] = {
        "scalars": {oids.SYS_DESCR: "Cisco Catalyst 2960", oids.SYS_NAME: "sw1"},
        "walks": {},
    }
    # 10.0.0.3 is absent → times out.

    devices = await discover_subnet(backend, "10.0.0.0/29", SnmpParams(), concurrency=8)
    ips = {d["ip"] for d in devices}
    assert ips == {"10.0.0.1"}
    assert devices[0]["brand"] == "HP"
    assert devices[0]["subnet_cidr"] == "10.0.0.0/29"


async def test_discover_finds_printer_with_nonone_device_index():
    """RFC 3805 says the printer device is keyed by hrDeviceIndex; many vendors
    do NOT pick 1. The old discovery code GET-ed prtGeneralPrinterName.1 directly
    and missed every such printer. Walking the table base fixes it."""
    backend = FakeSnmpBackend()
    backend.add("10.0.0.1", canned_printer(name="brother-mfp", model="Brother MFC-L8900CDW", device_index=5))
    devices = await discover_subnet(backend, "10.0.0.0/30", SnmpParams(), concurrency=4)
    assert len(devices) == 1
    assert devices[0]["brand"] == "Brother"
    assert devices[0]["model"] == "Brother MFC-L8900CDW"


async def test_discover_falls_back_to_vendor_in_sysdescr():
    """Some older / cheaper printers expose sysDescr but no Printer-MIB at all
    (or it's been disabled on the device). A sysDescr containing a known brand
    is still a strong-enough signal."""
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            oids.SYS_DESCR: "Brother NC-8400h, Firmware Ver.1.23",
            oids.SYS_NAME: "BRN0011223344",
        },
        "walks": {},  # No Printer-MIB whatsoever.
    }
    devices = await discover_subnet(backend, "10.0.0.0/30", SnmpParams(), concurrency=4)
    assert len(devices) == 1
    assert devices[0]["brand"] == "Brother"
    # Model is extracted best-effort from sysDescr when prtGeneralPrinterName is absent.
    assert "Brother" in devices[0]["model"]


async def test_discover_skips_non_printer_with_no_vendor():
    """Switch / server that answers SNMP but isn't a printer — must be skipped."""
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {oids.SYS_DESCR: "Linux server-01 5.15.0", oids.SYS_NAME: "srv01"},
        "walks": {},
    }
    devices = await discover_subnet(backend, "10.0.0.0/30", SnmpParams(), concurrency=4)
    assert devices == []


def test_vendor_from_matches_whole_words():
    # Real-world sysDescr strings.
    assert _vendor_from("Brother NC-8400h") == "Brother"
    assert _vendor_from("Hewlett-Packard JetDirect ex+3") == "HP"
    assert _vendor_from("KONICA MINOLTA bizhub C360i") == "Konica"
    assert _vendor_from("Lexmark MX622adhe") == "Lexmark"
    # Must not false-match substrings.
    assert _vendor_from("device cannot recover") is None
    assert _vendor_from("") is None
    assert _vendor_from(None) is None
