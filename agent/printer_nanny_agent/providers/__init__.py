"""Vendor-specific provider plugins per the project's design doc.

A PrinterProvider gets a chance to enrich the reading dict the standard
Printer-MIB poller built. Providers are best-effort: they may do additional
SNMP walks (Brother's private MIB), HTTP fetches (EWS scraping), etc. A
provider that raises is logged and skipped; the standard reading still ships.

Detection is by sysObjectID enterprise prefix. The registry runs providers
in registration order; the generic provider is always present and is a no-op.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from printer_nanny_agent.snmp import SnmpBackend, SnmpParams

log = logging.getLogger("printer_nanny_agent.providers")


class PrinterProvider:
    name = "generic"
    enterprise_prefixes: tuple = ()  # e.g. ("2435",) for Brother

    def detect(self, reading: dict, sys_object_id: Optional[str]) -> bool:
        """Return True if this provider applies to this device."""
        if not self.enterprise_prefixes:
            return False
        sys_oid = sys_object_id or ""
        # Match both the pysnmp-rendered "enterprises.<N>" form and the raw
        # numeric "1.3.6.1.4.1.<N>" form (with or without a leading dot).
        return any(
            f"enterprises.{p}" in sys_oid
            or f"1.3.6.1.4.1.{p}." in sys_oid
            or sys_oid.endswith(f"1.3.6.1.4.1.{p}")
            for p in self.enterprise_prefixes
        )

    async def augment(
        self,
        backend: SnmpBackend,
        ip: str,
        params: SnmpParams,
        reading: dict,
        sys_object_id: Optional[str],
    ) -> dict:
        """Optionally mutate ``reading`` in place and return it."""
        return reading


_REGISTRY: List[PrinterProvider] = []


def register(provider: PrinterProvider) -> None:
    _REGISTRY.append(provider)


def providers() -> List[PrinterProvider]:
    return list(_REGISTRY)


async def run_providers(
    backend: SnmpBackend,
    ip: str,
    params: SnmpParams,
    reading: dict,
    sys_object_id: Optional[str],
) -> dict:
    """Apply every detect()-matching provider to ``reading``.

    Provider exceptions are logged and swallowed - the standard reading still
    ships even if a vendor provider is broken on a particular printer.
    """
    for provider in _REGISTRY:
        try:
            if provider.detect(reading, sys_object_id):
                reading = await provider.augment(backend, ip, params, reading, sys_object_id)
        except Exception as exc:  # noqa: BLE001 - never fail the poll over a provider
            log.warning("provider %s failed for %s: %s", provider.name, ip, exc)
    return reading


# Import side-effect: every provider module that wants to be registered
# imports `register` and calls it at module load. Built-in providers are
# loaded here so they're always available. Order matters:
#  1. brother (SNMP MIB): always runs, seeds bucket-state UI hints from the
#     active-alert text.
#  2. brother_pjl (TCP/9100 PJL): the channel BRAdmin Pro uses; runs second
#     so its precise percentages take priority over EWS.
#  3. brother_ews (HTTP scrape): fragile per-model gauge math, only fills in
#     when PJL didn't have data for that supply.
from printer_nanny_agent.providers import brother  # noqa: E402,F401
from printer_nanny_agent.providers import brother_pjl  # noqa: E402,F401
from printer_nanny_agent.providers import brother_ews  # noqa: E402,F401
