"""Defensive providers for Xerox, Kyocera, Canon, Ricoh, Konica Minolta.

Same shape as the HP/Lexmark provider tests: detection on the documented
enterprise prefix, brand tag, supply-precision tag, and a status-message
read that surfaces non-idle text as an event.
"""

from __future__ import annotations

import pytest

from printer_nanny_agent.providers._vendors import (
    CanonProvider,
    KonicaMinoltaProvider,
    KyoceraProvider,
    RicohProvider,
    XeroxProvider,
)
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend

# (provider_cls, enterprise_prefix, expected_brand, supply_precision_tag,
#  panel_oid, sample_idle_text, sample_actionable_text)
VENDORS = [
    (XeroxProvider, "253", "Xerox", "xerox_standard",
     "1.3.6.1.4.1.253.8.53.13.2.1.6.1.1", "Ready", "Replace Toner Cartridge"),
    (KyoceraProvider, "1347", "Kyocera", "kyocera_standard",
     "1.3.6.1.4.1.1347.43.5.2.1.5.1.1", "Ready", "Add Paper Cassette 1"),
    (CanonProvider, "1602", "Canon", "canon_standard",
     "1.3.6.1.4.1.1602.1.11.1.3.1.4.1", "Ready to print", "Paper Jam in Output Tray"),
    (RicohProvider, "367", "Ricoh", "ricoh_standard",
     "1.3.6.1.4.1.367.3.2.1.2.24.1.1", "Energy Save Mode", "Toner Almost Empty"),
    (KonicaMinoltaProvider, "18334", "Konica Minolta", "konica_minolta_standard",
     "1.3.6.1.4.1.18334.1.1.1.5.7.1.1.4.1", "Sleep mode", "Toner Low (Cyan)"),
]


def _sys_oid(prefix: str) -> str:
    return f"SNMPv2-SMI::enterprises.{prefix}.1.2.3"


@pytest.mark.parametrize("provider_cls,prefix,brand,precision,panel_oid,idle,actionable", VENDORS)
def test_detect_by_enterprise_prefix_only(provider_cls, prefix, brand, precision,
                                          panel_oid, idle, actionable):
    p = provider_cls()
    assert p.detect({}, _sys_oid(prefix)) is True
    assert p.detect({}, f"1.3.6.1.4.1.{prefix}.1") is True
    # Not the other vendors. Skip Xerox/Ricoh against the trickier ".11" check
    # since some other enterprises share the substring; we test concrete
    # negatives instead.
    assert p.detect({}, "SNMPv2-SMI::enterprises.2435.2.3.9") is False  # Brother
    assert p.detect({}, None) is False


@pytest.mark.parametrize("provider_cls,prefix,brand,precision,panel_oid,idle,actionable", VENDORS)
async def test_augment_brand_and_precision_tags_set(provider_cls, prefix, brand, precision,
                                                    panel_oid, idle, actionable):
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    reading = {"supplies": [], "events": []}
    out = await provider_cls().augment(
        backend, "10.0.0.1", SnmpParams(), reading, _sys_oid(prefix),
    )
    assert out["identity"]["brand"] == brand
    assert out["_supply_precision"] == precision


@pytest.mark.parametrize("provider_cls,prefix,brand,precision,panel_oid,idle,actionable", VENDORS)
async def test_augment_idle_panel_message_suppressed(provider_cls, prefix, brand, precision,
                                                     panel_oid, idle, actionable):
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {panel_oid: idle}, "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await provider_cls().augment(
        backend, "10.0.0.1", SnmpParams(), reading, _sys_oid(prefix),
    )
    assert out["device_status_text"] == idle
    assert out["events"] == []  # noise filtered


@pytest.mark.parametrize("provider_cls,prefix,brand,precision,panel_oid,idle,actionable", VENDORS)
async def test_augment_actionable_panel_message_surfaces_as_event(
        provider_cls, prefix, brand, precision, panel_oid, idle, actionable):
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {panel_oid: actionable}, "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await provider_cls().augment(
        backend, "10.0.0.1", SnmpParams(), reading, _sys_oid(prefix),
    )
    assert out["device_status_text"] == actionable
    assert any(actionable in e["message"] for e in out["events"])


@pytest.mark.parametrize("provider_cls,prefix,brand,precision,panel_oid,idle,actionable", VENDORS)
async def test_augment_no_panel_oid_is_quiet(provider_cls, prefix, brand, precision,
                                             panel_oid, idle, actionable):
    """Models that don't expose the panel OID -- silent fallback, brand + tag still set."""
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {panel_oid: "No Such Object available on this device"},
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await provider_cls().augment(
        backend, "10.0.0.1", SnmpParams(), reading, _sys_oid(prefix),
    )
    assert out.get("device_status_text") is None
    assert out["events"] == []
    assert out["identity"]["brand"] == brand
