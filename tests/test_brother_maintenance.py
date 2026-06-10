"""Brother maintenance-blob provider -- the BRAdmin data path.

Decode tests use synthetic blobs built record-by-record (7 bytes -> 14 hex
chars: ID + 2 reserved bytes + 4-byte big-endian value). Percent records
store value*100, exactly like the firmware emits them.
"""

from __future__ import annotations

from printer_nanny_agent.providers.brother import BrotherProvider, OID_ACTIVE_ALERT_TEXT
from printer_nanny_agent.providers.brother_maintenance import (
    OID_MAINTENANCE,
    OID_NEXTCARE,
    BrotherMaintenanceProvider,
    decode_maintenance,
    decode_nextcare,
)
from printer_nanny_agent.snmp import SnmpParams

from tests.fakes import FakeSnmpBackend

SYS_OID = "SNMPv2-SMI::enterprises.2435.2.3.9.1"


def rec(rec_id: str, value: int) -> str:
    """One 14-hex-char maintenance record: ID + 2 reserved bytes + 4-byte value."""
    return f"{rec_id}0104{value:08x}"


def blob(*records: str) -> str:
    """A pysnmp-rendered binary octet string: 0x-prefixed concatenated records."""
    return "0x" + "".join(records)


def _backend(maintenance: str | None = None, nextcare: str | None = None,
             alert: str | None = None) -> FakeSnmpBackend:
    scalars: dict = {}
    if maintenance is not None:
        scalars[OID_MAINTENANCE] = maintenance
    if nextcare is not None:
        scalars[OID_NEXTCARE] = nextcare
    if alert is not None:
        scalars[OID_ACTIVE_ALERT_TEXT + ".0"] = alert
    backend = FakeSnmpBackend()
    backend.devices["10.0.0.1"] = {"scalars": scalars, "walks": {}}
    return backend


# ---------- decode_maintenance ----------

def test_decode_black_toner_percent():
    # ID 6f, value 8200 -> 82.00%
    out = decode_maintenance(blob(rec("6f", 8200)))
    assert out == {"toner:black": 82.0}


def test_decode_newer_firmware_a1_a4_ids():
    out = decode_maintenance(blob(
        rec("a1", 9100), rec("a2", 7300), rec("a3", 6500), rec("a4", 4200),
    ))
    assert out == {
        "toner:black": 91.0, "toner:cyan": 73.0,
        "toner:magenta": 65.0, "toner:yellow": 42.0,
    }


def test_decode_drum_and_maintenance_parts():
    out = decode_maintenance(blob(
        rec("41", 9000),   # drum 90%
        rec("69", 8800),   # belt
        rec("6a", 9700),   # fuser
        rec("6b", 9900),   # laser
        rec("6c", 7600),   # PF kit MP
        rec("6d", 8100),   # PF kit 1
    ))
    assert out["drum"] == 90.0
    assert out["belt"] == 88.0
    assert out["fuser"] == 97.0
    assert out["laser"] == 99.0
    assert out["pf_kit_mp"] == 76.0
    assert out["pf_kit_1"] == 81.0


def test_decode_per_color_drums():
    out = decode_maintenance(blob(
        rec("80", 9500), rec("79", 9400), rec("7a", 9300), rec("7b", 9200),
    ))
    assert out["drum:black"] == 95.0
    assert out["drum:cyan"] == 94.0
    assert out["drum:magenta"] == 93.0
    assert out["drum:yellow"] == 92.0


def test_decode_skips_not_applicable_sentinel():
    """0xFFFFFFFF means 'this part doesn't exist on this model'."""
    out = decode_maintenance(blob(rec("6f", 8200), rec("70", 0xFFFFFFFF)))
    assert out == {"toner:black": 82.0}  # cyan sentinel dropped


def test_decode_skips_out_of_range_values():
    """A value that decodes above 100% is garbage -- never ship a bad number."""
    out = decode_maintenance(blob(rec("6f", 250_00 + 1)))  # 250.01%
    assert "toner:black" not in out


def test_decode_collects_unknown_ids_for_diagnostics():
    out = decode_maintenance(blob(rec("6f", 5000), rec("ff", 1234), rec("ee", 5)))
    assert out["toner:black"] == 50.0
    assert set(out["_unknown"].split(",")) == {"ff", "ee"}


def test_decode_tolerates_prefix_whitespace_and_garbage():
    assert decode_maintenance(None) == {}
    assert decode_maintenance("") == {}
    assert decode_maintenance("not-hex-at-all") == {}
    assert decode_maintenance("0x") == {}
    # Whitespace / colon separators stripped.
    spaced = "0x 6f 01 04 00 00 20 08".replace(" ", " ")
    assert decode_maintenance(spaced)["toner:black"] == 82.0
    # Truncated trailing record (not a full 14 chars) is ignored.
    assert decode_maintenance(blob(rec("6f", 8200)) + "6f01")["toner:black"] == 82.0


# ---------- decode_nextcare ----------

def test_decode_nextcare_remaining_pages():
    out = decode_nextcare(blob(rec("82", 11500), rec("89", 49800)))
    assert out == {"drum": 11500, "fuser": 49800}


def test_decode_nextcare_skips_sentinels_and_unknown():
    out = decode_nextcare(blob(rec("82", 0xFFFFFFFF), rec("ff", 5)))
    assert out == {}


# ---------- provider augment ----------

async def test_augment_sets_exact_toner_percent_on_mono_printer():
    """The HL-L2460DW case: standard MIB says 'some remaining', the
    maintenance blob carries the exact percentage the EWS gauge renders."""
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": None,
             "status_note": "some remaining", "description": "Black Toner Cartridge"},
            {"type": "drum", "color": None, "level_pct": 90.0,
             "status_note": None, "description": "Drum Unit"},
        ],
        "events": [],
    }
    backend = _backend(
        maintenance=blob(rec("6f", 8200), rec("41", 9000)),
        nextcare=blob(rec("82", 11500)),
    )
    out = await BrotherMaintenanceProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading, SYS_OID,
    )
    black = next(s for s in out["supplies"] if s.get("color") == "black")
    assert black["level_pct"] == 82.0
    assert black["status_note"] is None
    assert black["_maintenance_sourced"] is True
    assert black["_brother_estimated"] is False
    assert out["_supply_precision"] == "brother_maintenance"
    # Drum agrees with standard MIB (90 vs 90) -> value untouched but protected.
    drum = next(s for s in out["supplies"] if s.get("type") == "drum")
    assert drum["level_pct"] == 90.0
    assert drum["_maintenance_sourced"] is True
    # Drum got the pages-remaining enrichment.
    assert drum["status_note"] == "~11,500 pages left"


async def test_augment_color_printer_four_toners():
    reading = {
        "supplies": [
            {"type": "toner", "color": c, "level_pct": None, "status_note": "some remaining"}
            for c in ("black", "cyan", "magenta", "yellow")
        ],
        "events": [],
    }
    backend = _backend(maintenance=blob(
        rec("a1", 9100), rec("a2", 7300), rec("a3", 6500), rec("a4", 4200),
    ))
    out = await BrotherMaintenanceProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading, SYS_OID,
    )
    by_color = {s["color"]: s for s in out["supplies"]}
    assert by_color["black"]["level_pct"] == 91.0
    assert by_color["cyan"]["level_pct"] == 73.0
    assert by_color["magenta"]["level_pct"] == 65.0
    assert by_color["yellow"]["level_pct"] == 42.0


async def test_augment_adds_rows_for_long_life_parts():
    """Belt/fuser/laser/PF kits aren't in prtMarkerSupplies on most models --
    the maintenance blob is the only place their life % exists. Each added
    'other'-typed row needs a distinct color slug because central upserts
    supplies keyed on (type, color)."""
    reading = {"supplies": [], "events": []}
    backend = _backend(
        maintenance=blob(
            rec("69", 8800), rec("6a", 9700), rec("6b", 9900),
            rec("6c", 7600), rec("6d", 8100),
        ),
        nextcare=blob(rec("88", 44000), rec("89", 49800)),
    )
    out = await BrotherMaintenanceProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading, SYS_OID,
    )
    by_descr = {s["description"]: s for s in out["supplies"]}
    assert by_descr["Belt Unit"]["level_pct"] == 88.0
    assert by_descr["Belt Unit"]["type"] == "other"
    assert by_descr["Belt Unit"]["status_note"] == "~44,000 pages left"
    assert by_descr["Fuser Unit"]["level_pct"] == 97.0
    assert by_descr["Fuser Unit"]["type"] == "fuser"
    assert by_descr["Fuser Unit"]["status_note"] == "~49,800 pages left"
    assert by_descr["Laser Unit"]["level_pct"] == 99.0
    assert by_descr["PF Kit MP"]["level_pct"] == 76.0
    assert by_descr["PF Kit 1"]["level_pct"] == 81.0
    # Distinct (type, color) keys so central's upsert can't collide them.
    other_rows = [s for s in out["supplies"] if s["type"] == "other"]
    other_keys = {(s["type"], s["color"]) for s in other_rows}
    assert len(other_rows) == 4  # belt, laser, pf-kit-mp, pf-kit-1 (fuser is type=fuser)
    assert len(other_keys) == 4  # every row keeps its own (type, color) identity


async def test_augment_overrides_bucket_estimate():
    """A bucket estimate (15% 'low' from the alert text) must yield to the
    exact firmware percentage."""
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": 15.0,
             "status_note": "low", "_brother_estimated": True},
        ],
        "events": [],
    }
    backend = _backend(maintenance=blob(rec("6f", 4700)))
    out = await BrotherMaintenanceProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading, SYS_OID,
    )
    black = out["supplies"][0]
    assert black["level_pct"] == 47.0
    assert black["_brother_estimated"] is False


async def test_augment_snmp_error_is_non_fatal():
    backend = FakeSnmpBackend()  # 10.0.0.1 not present -> SnmpError on get
    reading = {"supplies": [{"type": "toner", "color": "black", "level_pct": None}]}
    out = await BrotherMaintenanceProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading, SYS_OID,
    )
    assert out["supplies"][0]["level_pct"] is None
    assert out["_brother_maintenance"] == "snmp-error"
    assert "_supply_precision" not in out  # nothing claimed


async def test_augment_no_blob_reports_no_data():
    """Models that don't populate the blob (legacy inkjets) degrade silently --
    PJL / EWS fallbacks still get their chance downstream."""
    backend = _backend(maintenance=None)
    reading = {"supplies": [{"type": "toner", "color": "black", "level_pct": None}]}
    out = await BrotherMaintenanceProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading, SYS_OID,
    )
    assert out["supplies"][0]["level_pct"] is None
    assert out["_brother_maintenance"].startswith("no-data")


# ---------- pipeline interactions ----------

async def test_umbrella_bucket_pass_does_not_clobber_maintenance_values(monkeypatch):
    """Consolidated pipeline: maintenance pass first, bucket pass second --
    inside the single registered BrotherProvider. Even when the live alert
    says 'Toner Low', the exact percentage must survive (the bucket pass only
    fills when level_pct is None)."""
    from printer_nanny_agent.providers import brother as brother_mod

    fallbacks_called = []

    async def tracking_passthrough(backend, ip, params, reading, sys_object_id):
        fallbacks_called.append("net")
        return reading

    monkeypatch.setattr(brother_mod, "_pjl_step", tracking_passthrough)
    monkeypatch.setattr(brother_mod, "_ews_step", tracking_passthrough)

    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": None,
             "status_note": "some remaining", "description": "Black Toner Cartridge"},
        ],
        "events": [],
    }
    backend = _backend(
        maintenance=blob(rec("6f", 1200)),  # genuinely low: 12%
        alert="Toner Low",
    )
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading, SYS_OID,
    )
    black = next(s for s in out["supplies"] if s.get("color") == "black")
    assert black["level_pct"] == 12.0  # exact %, not the 15% bucket guess
    # Precision tag not downgraded by the bucket pass.
    assert out["_supply_precision"] == "brother_maintenance"
    assert out["_brother_source"] == "maintenance"
    # Maintenance answered, so the network fallbacks were never attempted.
    assert fallbacks_called == []


async def test_umbrella_runs_fallbacks_only_when_toner_gaps_remain(monkeypatch):
    """No maintenance blob + no live alert: the umbrella must give the legacy
    channels (PJL then EWS) their chance, in order."""
    from printer_nanny_agent.providers import brother as brother_mod

    calls: list[str] = []

    async def fake_pjl(backend, ip, params, reading, sys_object_id):
        calls.append("pjl")
        return reading  # PJL found nothing -> gap remains

    async def fake_ews(backend, ip, params, reading, sys_object_id):
        calls.append("ews")
        for s in reading.get("supplies", []):
            if s.get("type") == "toner":
                s["level_pct"] = 64.0
                s["_ews_sourced"] = True
        return reading

    monkeypatch.setattr(brother_mod, "_pjl_step", fake_pjl)
    monkeypatch.setattr(brother_mod, "_ews_step", fake_ews)

    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": None,
             "status_note": "some remaining"},
        ],
        "events": [],
    }
    backend = _backend()  # no blob, no alert
    out = await BrotherProvider().augment(
        backend, "10.0.0.1", SnmpParams(), reading, SYS_OID,
    )
    assert calls == ["pjl", "ews"]
    assert out["supplies"][0]["level_pct"] == 64.0
    assert out["_brother_source"] == "ews"


async def test_ews_defers_to_maintenance_sourced_values(monkeypatch):
    """EWS gauge scraping must never overwrite a maintenance-blob value --
    the blob is the same firmware counter without the pixel-math fragility."""
    from printer_nanny_agent.providers import brother_ews as mod
    from printer_nanny_agent.providers.brother_ews import BrotherEwsProvider

    async def html(ip: str):
        return '<script>TonerInfo[0]="55";</script>'  # would say 55%

    monkeypatch.setattr(mod, "_fetch_ews_html", html)
    reading = {
        "supplies": [
            {"type": "toner", "color": "black", "level_pct": 82.0,
             "status_note": None, "_maintenance_sourced": True},
        ],
    }
    out = await BrotherEwsProvider().augment(
        FakeSnmpBackend(), "10.0.0.1", SnmpParams(), reading, SYS_OID,
    )
    black = out["supplies"][0]
    assert black["level_pct"] == 82.0  # maintenance value survives
    # EWS made no change, so it must not relabel the precision source.
    assert out.get("_supply_precision") != "brother_ews"
