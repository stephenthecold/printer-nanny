"""HP provider: detection, brand + model tagging, status message readout."""

from __future__ import annotations

from printer_nanny_agent.providers.hp import (
    OID_HP_DEVICE_MODEL,
    OID_HP_DEVICE_STATUS_MSG,
    HPProvider,
)
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend


def test_detect_matches_hp_printers_subtree():
    p = HPProvider()
    assert p.detect({}, "SNMPv2-SMI::enterprises.11.2.3.9.1") is True
    assert p.detect({}, "1.3.6.1.4.1.11.2.3.9.1") is True
    # Plain HP enterprise without the printers subtree does NOT match -- HP
    # makes a lot of non-printer hardware on .11.
    assert p.detect({}, "SNMPv2-SMI::enterprises.11.1.2.3") is False
    assert p.detect({}, "SNMPv2-SMI::enterprises.2435.2.3.9") is False  # Brother
    assert p.detect({}, "SNMPv2-SMI::enterprises.641.1.4.1") is False  # Lexmark
    assert p.detect({}, None) is False


async def test_augment_tags_brand_and_supply_precision():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    reading = {"supplies": [], "events": []}
    out = await HPProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.11.2.3.9.1",
    )
    assert out["identity"]["brand"] == "HP"
    assert out["_supply_precision"] == "hp_native"


async def test_augment_reads_status_msg_and_model():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            OID_HP_DEVICE_STATUS_MSG: "Replace Black Cartridge",
            OID_HP_DEVICE_MODEL: "HP LaserJet Pro M404dn",
        },
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await HPProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.11.2.3.9.1",
    )
    assert out["device_status_text"] == "Replace Black Cartridge"
    assert out["identity"]["model"] == "HP LaserJet Pro M404dn"
    assert any(
        e["code"] == "hp-panel" and "Replace Black Cartridge" in e["message"]
        for e in out["events"]
    )


async def test_augment_suppresses_noise_panel_messages():
    """Ready / Sleeping / Energy Save are not worth opening tickets over."""
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {OID_HP_DEVICE_STATUS_MSG: "Sleeping"},
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await HPProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.11.2.3.9.1",
    )
    assert out["device_status_text"] == "Sleeping"
    assert out["events"] == []  # noise suppressed


async def test_augment_does_not_overwrite_existing_model():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {OID_HP_DEVICE_MODEL: "HP LaserJet Pro M404dn"},
        "walks": {},
    }
    reading = {"identity": {"model": "Custom-Set Model"}, "events": []}
    out = await HPProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.11.2.3.9.1",
    )
    # Operator-set model takes priority.
    assert out["identity"]["model"] == "Custom-Set Model"


async def test_augment_swallows_no_such_object():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            OID_HP_DEVICE_STATUS_MSG: "No Such Object available on this device",
            OID_HP_DEVICE_MODEL: "",
        },
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await HPProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.11.2.3.9.1",
    )
    assert out.get("device_status_text") is None
    assert out.get("identity", {}).get("model") is None
    # Provider still always sets the brand + precision.
    assert out["identity"]["brand"] == "HP"
    assert out["_supply_precision"] == "hp_native"
