"""Color/mono + per-function page-meter split, end to end.

Covers the billing-grade meter pipeline added on top of the existing total
``page_count``:
  1. ``record_meters`` (agent provider toolkit) -- the single, validated place a
     vendor decoder writes the split; junk/negative/bool values are dropped so a
     bad decode can never write a bogus meter.
  2. ``apply_reading`` persists the split to the Printer cache + the append-only
     Reading row.
  3. The monthly billing CSV carries mono/color columns (blank, never 0, when a
     device reports no split).
  4. The printer detail page shows the split when present and says so when not.
"""

from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient

from central import models as m
from central import reports
from central import schemas as s
from central import services
from central.main import app
from central.security import hash_password
from printer_nanny_agent.providers import record_meters


# --------------------------------------------------------------------------- #
# 1) record_meters helper (agent side)
# --------------------------------------------------------------------------- #
def test_record_meters_stores_valid_split_and_snapshot():
    reading = {"page_count": 1000, "mono_count": None, "color_count": None, "meter_snapshot": None}
    snap = record_meters(
        reading, total=1000, mono=700, color=300,
        functions={"print": 900, "copy": 80, "fax": 20},
    )
    assert reading["mono_count"] == 700
    assert reading["color_count"] == 300
    assert snap == {"total": 1000, "mono": 700, "color": 300, "print": 900, "copy": 80, "fax": 20}
    assert reading["meter_snapshot"] == snap
    # page_count is the standard-MIB total; record_meters must not clobber it.
    assert reading["page_count"] == 1000


def test_record_meters_drops_invalid_values():
    reading = {"mono_count": None, "color_count": None, "meter_snapshot": None}
    # bool is not a valid count (isinstance(True, int) is True -- must be excluded),
    # negatives are nonsense, strings/floats are junk from a bad decode.
    snap = record_meters(
        reading, total=-5, mono=True, color="300",
        functions={"print": 1.5, "copy": -2, "fax": 10},
    )
    # Only the one valid function count survives; mono/color stay unset.
    assert reading["mono_count"] is None
    assert reading["color_count"] is None
    assert snap == {"fax": 10}


def test_record_meters_returns_none_when_nothing_valid():
    reading = {"meter_snapshot": None}
    assert record_meters(reading, total=None, mono=None, color=None) is None
    assert reading.get("meter_snapshot") in (None,)


def test_ingest_schema_clamps_out_of_range_meters_to_none():
    """The ingest trust boundary drops a negative or INT4-overflowing meter to
    None (billing integrity), even though record_meters already guards the agent
    side -- a misbehaving authenticated agent must not write a bogus meter."""
    bad = s.ReadingIn(
        ip="10.0.0.1", page_count=-1, mono_count=2_147_483_648, color_count=-99,
    )
    assert bad.page_count is None
    assert bad.mono_count is None  # 2^31 overflows INT4 -> treated as not reported
    assert bad.color_count is None
    good = s.ReadingIn(ip="10.0.0.1", page_count=0, mono_count=700, color_count=2_147_483_647)
    assert (good.page_count, good.mono_count, good.color_count) == (0, 700, 2_147_483_647)


def test_record_meters_merges_into_existing_snapshot():
    reading = {"meter_snapshot": {"total": 1000, "mono": 700}}
    record_meters(reading, color=300)
    assert reading["meter_snapshot"] == {"total": 1000, "mono": 700, "color": 300}
    assert reading["color_count"] == 300


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
def _approved_printer(db, ip="10.0.0.7", model="HP Color E72430"):
    # Unique client name per call so a test that seeds several printers doesn't
    # trip the clients.name unique constraint.
    client = m.Client(name=f"Acme-{ip}")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip=ip, model=model,
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    return printer


# --------------------------------------------------------------------------- #
# 2) apply_reading persistence
# --------------------------------------------------------------------------- #
def test_apply_reading_persists_meter_split(db):
    printer = _approved_printer(db)
    reading = s.ReadingIn(
        ip=printer.ip, status=m.PrinterStatus.ok, page_count=1000,
        mono_count=720, color_count=280,
        meter_snapshot={"total": 1000, "mono": 720, "color": 280, "copy": 50},
    )
    out = services.apply_reading(db, printer.site_id, reading)
    db.flush()
    assert out is printer
    # Cached on the printer for dashboards/reports.
    assert printer.page_count == 1000
    assert printer.mono_count == 720
    assert printer.color_count == 280
    # Stored on the append-only reading (what billing diffs across a period).
    row = db.query(m.Reading).filter_by(printer_id=printer.id).one()
    assert (row.page_count, row.mono_count, row.color_count) == (1000, 720, 280)
    assert row.meter_snapshot["copy"] == 50


def test_apply_reading_without_split_leaves_meters_null(db):
    printer = _approved_printer(db)
    reading = s.ReadingIn(ip=printer.ip, status=m.PrinterStatus.ok, page_count=500)
    services.apply_reading(db, printer.site_id, reading)
    db.flush()
    assert printer.page_count == 500
    assert printer.mono_count is None and printer.color_count is None
    row = db.query(m.Reading).filter_by(printer_id=printer.id).one()
    assert row.mono_count is None and row.color_count is None
    assert row.meter_snapshot is None


def test_apply_reading_caches_only_reported_meter(db):
    """A later reading that reports only total must not wipe a previously-known
    mono/color cache (we only overwrite the cache with values that are present)."""
    printer = _approved_printer(db)
    services.apply_reading(
        db, printer.site_id,
        s.ReadingIn(ip=printer.ip, page_count=1000, mono_count=700, color_count=300),
    )
    db.flush()
    services.apply_reading(
        db, printer.site_id, s.ReadingIn(ip=printer.ip, page_count=1100),
    )
    db.flush()
    assert printer.page_count == 1100
    assert printer.mono_count == 700 and printer.color_count == 300


# --------------------------------------------------------------------------- #
# 3) Billing CSV
# --------------------------------------------------------------------------- #
def test_billing_csv_has_meter_columns(db):
    p1 = _approved_printer(db, ip="10.0.0.10")
    p1.page_count, p1.mono_count, p1.color_count = 1000, 700, 300
    # A device with no split: meter cells must be BLANK, not 0 (never bill a
    # missing meter as zero impressions).
    p2 = _approved_printer(db, ip="10.0.0.11")
    p2.page_count, p2.mono_count, p2.color_count = 500, None, None
    db.flush()

    rows = list(csv.reader(io.StringIO(reports.build_monthly_billing_csv(db).decode())))
    header = rows[0]
    assert "mono_count" in header and "color_count" in header
    mi, ci, ipi = header.index("mono_count"), header.index("color_count"), header.index("ip")
    by_ip = {r[ipi]: r for r in rows[1:]}
    assert by_ip["10.0.0.10"][mi] == "700" and by_ip["10.0.0.10"][ci] == "300"
    assert by_ip["10.0.0.11"][mi] == "" and by_ip["10.0.0.11"][ci] == ""


# --------------------------------------------------------------------------- #
# 4) Dashboard meter card
# --------------------------------------------------------------------------- #
def _login(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("pw"), role=m.UserRole.admin))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"}, follow_redirects=False)
    return cli


def test_printer_detail_shows_meter_split(db):
    printer = _approved_printer(db)
    printer.page_count, printer.mono_count, printer.color_count = 1000, 720, 280
    cli = _login(db)
    body = cli.get(f"/printers/{printer.id}", follow_redirects=False).text
    assert "Mono" in body and "720" in body
    assert "Color" in body and "280" in body
    assert "mono/color split not reported" not in body


def test_printer_detail_says_when_split_missing(db):
    printer = _approved_printer(db)
    printer.page_count, printer.mono_count, printer.color_count = 1000, None, None
    cli = _login(db)
    body = cli.get(f"/printers/{printer.id}", follow_redirects=False).text
    assert "mono/color split not reported" in body
