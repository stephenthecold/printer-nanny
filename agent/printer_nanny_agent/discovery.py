"""Discover printers on a subnet by SNMP-probing each host.

A host is reported as a printer when EITHER:
  * the Printer-MIB (RFC 3805, ``1.3.6.1.2.1.43.*``) responds — definitive, since
    that MIB exists only on printers, OR
  * ``sysDescr`` contains a known printer vendor — fallback for older / cheaper
    devices that ship a partial or broken Printer-MIB.

The old check (.1-indexed scalar GET against ``prtGeneralPrinterName.1`` /
``hrDeviceDescr.1``) was unreliable: real printers route their printer device
through whatever ``hrDeviceIndex`` they choose (often 2, 5, 10, or 65535), so a
GET at instance .1 returns nothing and discovery silently misses them. Walking
the table base is the right idiom for tables.

Probes run concurrently with a bounded semaphore so a /24 sweep stays fast
without flooding the network. Each probe emits a DEBUG line explaining its
verdict so ``printer-nanny-agent run -v`` can be used to diagnose installs.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from typing import List, Optional

from printer_nanny_agent import oids
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams

log = logging.getLogger("printer_nanny_agent.discovery")

_VENDORS = [
    "HP", "Hewlett-Packard", "Brother", "Canon", "Xerox", "Lexmark",
    "Konica", "Minolta", "Ricoh", "Kyocera", "Epson", "Samsung", "Dell",
    "OKI", "Sharp", "Toshiba", "Pantum",
]
# Match vendor names as whole words so "Canon" doesn't hit "cannot",
# "OKI" doesn't hit "Tokio", etc. Compiled once at import.
_VENDOR_RE = re.compile(
    r"\b(" + "|".join(re.escape(v) for v in _VENDORS) + r")\b", re.IGNORECASE
)
# Canonical form for the brand field — first match wins.
_VENDOR_CANONICAL = {v.lower(): v for v in _VENDORS}
_VENDOR_CANONICAL["hewlett-packard"] = "HP"
_VENDOR_CANONICAL["minolta"] = "Konica"


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
        m = _VENDOR_RE.search(text)
        if m:
            return _VENDOR_CANONICAL.get(m.group(1).lower(), m.group(1))
    return None


async def _walk_first(
    backend: SnmpBackend, ip: str, base_oid: str, params: SnmpParams
) -> Optional[str]:
    """Walk a table base and return the first non-empty value, or None."""
    try:
        rows = await backend.walk(ip, base_oid, params)
    except SnmpError:
        return None
    for value in rows.values():
        if value and not value.startswith(("No Such Object", "No Such Instance")):
            return value
    return None


async def _probe_host(
    backend: SnmpBackend, ip: str, params: SnmpParams, cidr: str
) -> Optional[dict]:
    """Identify whether ``ip`` is a printer and return a device dict, or None.

    Returns a special sentinel ``{"__no_snmp__": True}`` when the host doesn't
    speak SNMP at all (used only for the per-subnet summary; the caller filters
    these out of the discovered-device list).
    """
    try:
        ident = await backend.get(
            ip, [oids.SYS_DESCR, oids.SYS_NAME, oids.SYS_OBJECT_ID], params
        )
    except SnmpError as exc:
        log.debug("probe %s: no SNMP response (%s)", ip, exc)
        return None
    sys_descr = ident.get(oids.SYS_DESCR)
    if not sys_descr:
        log.debug("probe %s: empty sysDescr — skipping", ip)
        return {"__no_snmp__": True}

    # Stage 2: Printer-MIB presence is the definitive signal. The Printer-MIB
    # (RFC 3805, subtree 1.3.6.1.2.1.43.*) only exists on printers, and walking
    # the table tolerates whatever hrDeviceIndex the vendor chose.
    model = await _walk_first(backend, ip, oids.PRT_GENERAL_PRINTER_NAME_BASE, params)
    serial = await _walk_first(backend, ip, oids.PRT_GENERAL_SERIAL_NUMBER_BASE, params)
    has_printer_mib = model is not None or serial is not None

    # Stage 3: vendor in sysDescr as a fallback for devices with no/partial MIB.
    vendor = _vendor_from(model, sys_descr)

    if not has_printer_mib and not vendor:
        log.debug(
            "probe %s: not a printer (sysDescr=%r, no Printer-MIB, no vendor match)",
            ip, sys_descr,
        )
        return {"__no_snmp__": False}

    if not model:
        # No prtGeneralPrinterName row — best-effort: pull a model from sysDescr.
        # Many vendors put a usable model name as the first semicolon-delimited
        # token (e.g. "Brother NC-8400h, Firmware Ver.X" → "Brother NC-8400h").
        first = sys_descr.split(";", 1)[0].strip()
        model = first[:120] if first else sys_descr[:120]

    log.info(
        "probe %s: PRINTER vendor=%s model=%r serial=%s mib=%s",
        ip, vendor, model, serial or "-", "yes" if has_printer_mib else "no",
    )
    return {
        "ip": ip,
        "hostname": ident.get(oids.SYS_NAME),
        "brand": vendor,
        "model": model,
        "serial": serial,
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
    log.info(
        "discovering %s (%d hosts, community=%r v%s timeout=%ss concurrency=%d)",
        cidr, len(hosts), params.community, params.version, params.timeout, concurrency,
    )

    async def guarded(ip: str):
        async with sem:
            return await _probe_host(backend, ip, params, cidr)

    results = await asyncio.gather(*(guarded(ip) for ip in hosts), return_exceptions=True)
    devices: List[dict] = []
    responded = 0
    errors = 0
    for r in results:
        if isinstance(r, dict):
            if "__no_snmp__" in r:
                if r["__no_snmp__"] is False:
                    responded += 1  # SNMP-capable but not a printer
                continue
            devices.append(r)
            responded += 1
        elif isinstance(r, BaseException):
            errors += 1
            log.debug("probe raised: %s", r)
    log.info(
        "discovered %d printer(s) on %s (probed=%d, SNMP-responded=%d, errors=%d)",
        len(devices), cidr, len(hosts), responded, errors,
    )
    return devices
