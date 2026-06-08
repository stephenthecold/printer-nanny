"""A fake SNMP backend for agent tests and the end-to-end demo — no real network."""

from __future__ import annotations

from typing import Dict, List, Optional

from printer_nanny_agent import oids
from printer_nanny_agent.snmp import SnmpBackend, SnmpError, SnmpParams


def canned_printer(
    *,
    name: str = "hp-front",
    model: str = "HP LaserJet M404",
    serial: str = "VNB1234567",
    pages: int = 84231,
    black_level: int = 2500,
    black_max: int = 10000,
    error_state: str = "0x0000",
    device_index: int = 1,
) -> dict:
    """Build a fake device's SNMP data (scalars + supply walks).

    ``device_index`` controls the hrDeviceIndex used for the prtGeneral table
    rows. Real printers use whatever index they like (often 1, but 2/5/65535
    happen) — tests pass non-default values to prove discovery doesn't depend
    on the .1 form.
    """
    d_base = oids.PRT_MARKER_SUPPLIES_DESCRIPTION
    t_base = oids.PRT_MARKER_SUPPLIES_TYPE
    m_base = oids.PRT_MARKER_SUPPLIES_MAX_CAPACITY
    l_base = oids.PRT_MARKER_SUPPLIES_LEVEL
    idx = ".1.1"
    di = device_index
    return {
        "scalars": {
            oids.SYS_NAME: name,
            oids.SYS_DESCR: f"{model} ETHERNET MULTI-ENVIRONMENT",
            oids.PRT_GENERAL_PRINTER_NAME: model,
            oids.PRT_GENERAL_SERIAL_NUMBER: serial,
            oids.HR_DEVICE_DESCR: model,
            oids.PRT_MARKER_LIFE_COUNT: str(pages),
            oids.HR_PRINTER_STATUS: "3",
            oids.HR_PRINTER_DETECTED_ERROR_STATE: error_state,
        },
        "walks": {
            d_base: {f"{d_base}{idx}": "Black Cartridge HP CF259A"},
            t_base: {f"{t_base}{idx}": "3"},
            m_base: {f"{m_base}{idx}": str(black_max)},
            l_base: {f"{l_base}{idx}": str(black_level)},
            # Discovery uses these walks for the printer fingerprint, indexed
            # by hrDeviceIndex (di) — letting tests vary the index.
            oids.PRT_GENERAL_PRINTER_NAME_BASE: {
                f"{oids.PRT_GENERAL_PRINTER_NAME_BASE}.{di}": model,
            },
            oids.PRT_GENERAL_SERIAL_NUMBER_BASE: {
                f"{oids.PRT_GENERAL_SERIAL_NUMBER_BASE}.{di}": serial,
            },
            oids.HR_DEVICE_DESCR_BASE: {f"{oids.HR_DEVICE_DESCR_BASE}.{di}": model},
        },
    }


class FakeSnmpBackend(SnmpBackend):
    """Responds from an in-memory {ip: canned_printer()} map; unknown ips time out."""

    def __init__(self, devices: Optional[Dict[str, dict]] = None):
        self.devices = devices or {}

    def add(self, ip: str, device: Optional[dict] = None) -> None:
        self.devices[ip] = device or canned_printer()

    async def get(
        self, host: str, oid_list: List[str], params: SnmpParams
    ) -> Dict[str, Optional[str]]:
        device = self.devices.get(host)
        if device is None:
            raise SnmpError(f"{host}: No SNMP response")
        scalars = device["scalars"]
        return {oid: scalars.get(oid) for oid in oid_list}

    async def walk(self, host: str, base_oid: str, params: SnmpParams) -> Dict[str, str]:
        device = self.devices.get(host)
        if device is None:
            raise SnmpError(f"{host}: No SNMP response")
        return dict(device["walks"].get(base_oid, {}))
