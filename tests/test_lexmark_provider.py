"""Lexmark provider: detection, brand tag, op-panel readout, supply precision."""

from __future__ import annotations

from printer_nanny_agent.providers.lexmark import (
    OID_OPERATOR_PANEL_FALLBACK,
    OID_OPERATOR_PANEL_LINE1,
    OID_OPERATOR_PANEL_LINE2,
    LexmarkProvider,
)
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend


def test_detect_matches_lexmark_enterprise():
    p = LexmarkProvider()
    assert p.detect({}, "SNMPv2-SMI::enterprises.641.1.4.1") is True
    assert p.detect({}, "1.3.6.1.4.1.641.1.4.1") is True
    assert p.detect({}, "SNMPv2-SMI::enterprises.2435.2.3.9") is False  # Brother
    assert p.detect({}, "SNMPv2-SMI::enterprises.11.2.3.9") is False  # HP
    assert p.detect({}, None) is False


async def test_augment_tags_brand_and_supply_precision():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    reading = {"supplies": [], "events": []}
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    assert out["identity"]["brand"] == "Lexmark"
    assert out["_supply_precision"] == "lexmark_native"


async def test_augment_does_not_overwrite_existing_brand():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": {}, "walks": {}}
    reading = {"identity": {"brand": "Lexmark International"}, "events": []}
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    # The provider must respect a brand that's already been set upstream.
    assert out["identity"]["brand"] == "Lexmark International"


async def test_augment_reads_operator_panel_two_lines():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            OID_OPERATOR_PANEL_LINE1: "88 Cyan Cartridge",
            OID_OPERATOR_PANEL_LINE2: "Low",
        },
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    assert out["device_status_text"] == "88 Cyan Cartridge Low"
    assert any(
        e["code"] == "lexmark-panel" and "88 Cyan Cartridge Low" in e["message"]
        for e in out["events"]
    )


async def test_augment_skips_event_when_panel_says_ready():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            OID_OPERATOR_PANEL_LINE1: "Ready",
            OID_OPERATOR_PANEL_LINE2: "",
        },
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    assert out["device_status_text"] == "Ready"
    # "Ready" is not worth surfacing as an event.
    assert out["events"] == []


async def test_augment_uses_fallback_oid_when_primary_empty():
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {OID_OPERATOR_PANEL_FALLBACK: "Paper Jam Tray 1"},
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    assert out["device_status_text"] == "Paper Jam Tray 1"


async def test_augment_handles_no_such_object_strings():
    """pysnmp renders absent OIDs as 'No Such Object/Instance...' - treat as None."""
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {
        "scalars": {
            OID_OPERATOR_PANEL_LINE1: "No Such Instance currently exists at this OID",
            OID_OPERATOR_PANEL_LINE2: "",
        },
        "walks": {},
    }
    reading = {"supplies": [], "events": []}
    out = await LexmarkProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading,
        "SNMPv2-SMI::enterprises.641.1.4.1",
    )
    assert out.get("device_status_text") is None
