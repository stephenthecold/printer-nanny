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

    Each provider run leaves a small trace dict on ``reading["provider_trace"]``
    so the central server's printer detail page can surface diagnostics
    (which provider matched, did it succeed, what did it change). This is the
    primary way an operator can see why a Brother is still showing buckets
    instead of percentages without dropping into agent logs.
    """
    for provider in _REGISTRY:
        try:
            if not provider.detect(reading, sys_object_id):
                continue
        except Exception as exc:  # noqa: BLE001 - never fail the poll over a provider
            log.warning("provider %s detect raised for %s: %s", provider.name, ip, exc)
            continue
        # Snapshot the supply state before the provider runs so we can diff
        # afterward and tell the operator what the provider actually changed.
        before_pcts = {
            (s.get("type"), s.get("color")): s.get("level_pct")
            for s in reading.get("supplies", [])
        }
        before_status = {
            (s.get("type"), s.get("color")): s.get("status_note")
            for s in reading.get("supplies", [])
        }
        trace = {
            "name": provider.name,
            "ok": True,
            "error": None,
            "changed": [],  # ["yellow toner: 73% via brother_pjl", ...]
            "summary": "",
        }
        try:
            reading = await provider.augment(backend, ip, params, reading, sys_object_id)
        except Exception as exc:  # noqa: BLE001 - never fail the poll over a provider
            log.warning("provider %s failed for %s: %s", provider.name, ip, exc)
            trace["ok"] = False
            trace["error"] = f"{type(exc).__name__}: {exc}"
        else:
            for supply in reading.get("supplies", []):
                key = (supply.get("type"), supply.get("color"))
                before_pct = before_pcts.get(key)
                after_pct = supply.get("level_pct")
                before_note = before_status.get(key)
                after_note = supply.get("status_note")
                label = supply.get("color") or supply.get("type") or "supply"
                if before_pct is None and after_pct is not None:
                    trace["changed"].append(f"{label}: set to {after_pct:.0f}%")
                elif before_pct != after_pct and after_pct is not None:
                    trace["changed"].append(
                        f"{label}: {before_pct:.0f}% -> {after_pct:.0f}%"
                    )
                elif before_note != after_note and after_note:
                    trace["changed"].append(f"{label}: status '{after_note}'")
            precision = reading.get("_supply_precision")
            parts = []
            if precision:
                parts.append(f"precision={precision}")
            # Brother provider leaves diagnostic breadcrumbs so the dashboard
            # can show WHY no change was made (e.g. live alert was "Sleep").
            for key, label in (
                ("_brother_maintenance", "maintenance"),
                ("_brother_active_alert", "alert"),
                ("_brother_parsed_severity", "parsed"),
                ("_brother_source", "source"),
            ):
                val = reading.get(key)
                if val is not None:
                    parts.append(f"{label}={val}")
            trace["summary"] = " ".join(parts)
        reading.setdefault("provider_trace", []).append(trace)
    return reading


# Import side-effect: every provider module that wants to be registered
# imports `register` and calls it at module load. Built-in providers are
# loaded here so they're always available.
#
# Brother is ONE consolidated provider (brother.py) that internally
# sequences four passes -- maintenance blob, alert/status, PJL, EWS --
# skipping the network-heavy fallbacks once real percentages exist. The
# sub-modules (brother_maintenance / brother_pjl / brother_ews) keep their
# classes and parsers for unit testing but do not self-register, so a
# Brother printer produces one diagnostics row instead of four.
from printer_nanny_agent.providers import brother  # noqa: E402,F401
from printer_nanny_agent.providers import hp  # noqa: E402,F401
from printer_nanny_agent.providers import lexmark  # noqa: E402,F401
# Long-tail vendor providers (Xerox / Kyocera / Canon / Ricoh / Konica
# Minolta): brand tag + front-panel status text. Real percentages still
# come from the standard Printer-MIB on these.
from printer_nanny_agent.providers import _vendors  # noqa: E402,F401
