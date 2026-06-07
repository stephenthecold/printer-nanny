"""Agent poller: error-bit decoding, supply assembly, reading building, polling."""

from __future__ import annotations

from printer_nanny_agent import oids
from printer_nanny_agent.poller import (
    build_reading,
    build_supplies,
    parse_error_bits,
    poll_printer,
)
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend, canned_printer


def test_parse_error_bits():
    assert parse_error_bits("0x2000") == [2]   # low toner (bit 2)
    assert parse_error_bits("0x0400") == [5]   # jammed (bit 5)
    assert parse_error_bits("0x0000") == []
    assert parse_error_bits("") == []
    assert parse_error_bits(None) == []


def test_build_supplies_percentage_and_color():
    d = oids.PRT_MARKER_SUPPLIES_DESCRIPTION
    t = oids.PRT_MARKER_SUPPLIES_TYPE
    mx = oids.PRT_MARKER_SUPPLIES_MAX_CAPACITY
    lv = oids.PRT_MARKER_SUPPLIES_LEVEL
    walks = {
        d: {f"{d}.1.1": "Cyan Toner", f"{d}.1.2": "Black Toner"},
        t: {f"{t}.1.1": "3", f"{t}.1.2": "3"},
        mx: {f"{mx}.1.1": "1000", f"{mx}.1.2": "1000"},
        lv: {f"{lv}.1.1": "250", f"{lv}.1.2": "-3"},  # second: "some remaining"
    }
    supplies = build_supplies(walks)
    by_color = {s["color"]: s for s in supplies}
    assert by_color["cyan"]["level_pct"] == 25.0
    assert by_color["cyan"]["type"] == "toner"
    assert by_color["black"]["level_pct"] is None  # sentinel handled


def test_build_reading_ok_device():
    device = canned_printer()
    reading = build_reading(
        "10.0.0.5", device["scalars"], device["walks"]
    )
    assert reading["ip"] == "10.0.0.5"
    assert reading["status"] == "ok"
    assert reading["brand"] == "HP"
    assert reading["model"] == "HP LaserJet M404"
    assert reading["page_count"] == 84231
    assert reading["supplies"][0]["level_pct"] == 25.0
    assert reading["events"] == []


def test_build_reading_error_state_sets_status_and_events():
    device = canned_printer(error_state="0x0400")  # jammed → critical
    reading = build_reading("10.0.0.6", device["scalars"], device["walks"])
    assert reading["status"] == "error"
    assert any(e["severity"] == "critical" for e in reading["events"])

    device2 = canned_printer(error_state="0x2000")  # low toner → warning
    reading2 = build_reading("10.0.0.7", device2["scalars"], device2["walks"])
    assert reading2["status"] == "warning"
    assert reading2["events"][0]["severity"] == "warning"


async def test_poll_printer_with_fake_backend():
    backend = FakeSnmpBackend({"10.0.0.5": canned_printer()})
    reading = await poll_printer(backend, "10.0.0.5", SnmpParams())
    assert reading["model"] == "HP LaserJet M404"
    assert reading["supplies"][0]["color"] == "black"


def test_offline_bit_does_not_flag_error_on_reachable_printer():
    # 0x02 = detected-error bit 6 ("offline") — real Brother behavior while idle.
    # A device we just polled is reachable, so this must be info, not a critical error.
    device = canned_printer(error_state="0x02")
    reading = build_reading("10.0.0.8", device["scalars"], device["walks"])
    assert reading["status"] == "ok"
    assert all(e["severity"] != "critical" for e in reading["events"])
    assert any(e["code"] == "offline" and e["severity"] == "info" for e in reading["events"])


def test_status_ok_when_reachable_and_clean_regardless_of_printer_status():
    device = canned_printer(error_state="0x0000")
    device["scalars"][oids.HR_PRINTER_STATUS] = "1"  # other(1) — Brother idle
    reading = build_reading("10.0.0.9", device["scalars"], device["walks"])
    assert reading["status"] == "ok"


def test_build_supplies_includes_level_row_without_description():
    lv = oids.PRT_MARKER_SUPPLIES_LEVEL
    mx = oids.PRT_MARKER_SUPPLIES_MAX_CAPACITY
    # A device that reports a level but no description row must not be dropped.
    walks = {lv: {f"{lv}.1.1": "500"}, mx: {f"{mx}.1.1": "1000"}}
    supplies = build_supplies(walks)
    assert len(supplies) == 1
    assert supplies[0]["level_pct"] == 50.0
    assert supplies[0]["description"] is None


def test_supply_status_note_for_sentinel_toner():
    d = oids.PRT_MARKER_SUPPLIES_DESCRIPTION
    lv = oids.PRT_MARKER_SUPPLIES_LEVEL
    walks = {d: {f"{d}.1.1": "Black Toner Cartridge"}, lv: {f"{lv}.1.1": "-3"}}
    sup = build_supplies(walks)[0]
    assert sup["level_pct"] is None
    assert sup["status_note"] == "some remaining"
