"""Discover printers on a subnet by SNMP-probing each host.

A host is reported as a printer if it answers ``sysDescr`` AND exposes the
printer-MIB fingerprint OID (``prtGeneralPrinterName``). Probes run concurrently
with a bounded semaphore so a /24 sweep stays fast without flooding the network.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import List, Optional

from printer_nanny_agent import oids
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams

log = logging.getLogger("printer_nanny_agent.discovery")

_IDENTITY_OIDS = [
    oids.SYS_DESCR,
    oids.SYS_NAME,
    oids.PRT_GENERAL_PRINTER_NAME,
    oids.PRT_GENERAL_SERIAL_NUMBER,
    oids.HR_DEVICE_DESCR,
]

_VENDORS = [
    "HP", "Brother", "Canon", "Xerox", "Lexmark", "Konica", "Ricoh",
    "Kyocera", "Epson", "Samsung", "Dell", "OKI", "Sharp", "Toshiba",
]


def hosts_in(cidr: str) -> List[str]:
    """Usable host addresses in a CIDR (excludes network/broadcast for IPv4)."""
    net = ipaddress.ip_network(cidr, strict=False)
    if net.num_addresses <= 2:
        return [str(net.network_address)]
    return [str(ip) for ip in net.hosts()]


def _vendor_from(*texts: Optional[str]) -> Optional[str]:
    for text in texts:
        if not text:
            continue
        low = text.lower()
        for vendor in _VENDORS:
            if vendor.lower() in low:
                return vendor
    return None


async def _probe_host(
    backend: SnmpBackend, ip: str, params: SnmpParams, cidr: str
) -> Optional[dict]:
    try:
        ident = await backend.get(ip, _IDENTITY_OIDS, params)
    except SnmpError:
        return None  # no SNMP response → not a managed device
    if not ident.get(oids.SYS_DESCR):
        return None
    # Fingerprint: must look like a printer, not just any SNMP device.
    if not ident.get(oids.PRT_GENERAL_PRINTER_NAME) and not ident.get(oids.HR_DEVICE_DESCR):
        return None
    model = ident.get(oids.PRT_GENERAL_PRINTER_NAME) or ident.get(oids.HR_DEVICE_DESCR)
    return {
        "ip": ip,
        "hostname": ident.get(oids.SYS_NAME),
        "brand": _vendor_from(model, ident.get(oids.SYS_DESCR)),
        "model": model,
        "serial": ident.get(oids.PRT_GENERAL_SERIAL_NUMBER),
        "subnet_cidr": cidr,
    }


async def discover_subnet(
    backend: SnmpBackend,
    cidr: str,
    params: SnmpParams,
    *,
    concurrency: int = 64,
) -> List[dict]:
    """Sweep ``cidr`` and return discovered printer device dicts."""
    sem = asyncio.Semaphore(concurrency)
    hosts = hosts_in(cidr)
    log.info("discovering %s (%d hosts, concurrency=%d)", cidr, len(hosts), concurrency)

    async def guarded(ip: str):
        async with sem:
            return await _probe_host(backend, ip, params, cidr)

    results = await asyncio.gather(*(guarded(ip) for ip in hosts), return_exceptions=True)
    devices = [r for r in results if isinstance(r, dict)]
    log.info("discovered %d printer(s) on %s", len(devices), cidr)
    return devices
