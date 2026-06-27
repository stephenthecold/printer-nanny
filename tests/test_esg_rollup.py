"""ESG / sustainability rollup: math, factor configurability, tenant scoping,
weekly-report section, and the customer-portal panel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from central import models as m
from central import queries, reports
from central.main import app
from central.runtime import save_settings
from central.security import hash_password


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _printer(db, client, site, ip="10.0.0.10"):
    p = m.Printer(
        client_id=client.id, site_id=site.id, ip=ip,
        brand="HP", model="M404", display_name="Front Desk",
        status=m.PrinterStatus.ok, discovery_state=m.DiscoveryState.approved,
    )
    db.add(p)
    db.flush()
    return p


def _readings(db, printer, page_counts, start=None):
    """Append page-count readings oldest→newest, one per day."""
    start = start or (_now() - timedelta(days=len(page_counts)))
    for i, pc in enumerate(page_counts):
        db.add(m.Reading(
            printer_id=printer.id, ts=start + timedelta(days=i),
            page_count=pc, status=m.PrinterStatus.ok,
        ))


def _seed_client(db, name="Acme"):
    client = m.Client(name=name)
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    return client, site


# --------------------------------------------------------------------------- #
# Rollup math
# --------------------------------------------------------------------------- #
def test_rollup_sums_positive_deltas(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    # 1000 -> 1100 -> 1300 == 300 pages printed.
    _readings(db, p, [1000, 1100, 1300])
    db.commit()

    out = queries.sustainability_rollup(db)
    assert out["pages"] == 300
    assert out["sheets"] > 0
    assert out["paper_g"] > 0
    assert out["co2_kg"] > 0
    assert out["kwh"] > 0
    assert out["trees"] > 0
    assert out["estimated"] is True
    assert out["printers"] == 1


def test_rollup_co2_scales_with_sheets(db):
    """Internal consistency: double the prints -> double the CO2e and paper."""
    client, site = _seed_client(db)
    p1 = _printer(db, client, site, ip="10.0.0.11")
    _readings(db, p1, [0, 100])  # 100 pages
    db.commit()
    base = queries.sustainability_rollup(db)

    p2 = _printer(db, client, site, ip="10.0.0.12")
    _readings(db, p2, [0, 100])  # another 100 pages, same client
    db.commit()
    doubled = queries.sustainability_rollup(db)

    assert doubled["pages"] == 2 * base["pages"]
    assert abs(doubled["co2_kg"] - 2 * base["co2_kg"]) < 1e-9
    assert abs(doubled["paper_g"] - 2 * base["paper_g"]) < 1e-9
    assert abs(doubled["kwh"] - 2 * base["kwh"]) < 1e-9


def test_counter_reset_never_negative(db):
    """A firmware reflash / printer swap drops page_count; the reset step must
    contribute 0, never a negative delta that erases real prints."""
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    # Climbs to 1300, then resets to 50 and climbs again to 250.
    # Real prints captured: (1300-1000) + (250-50) = 500. The 50<<1300 drop is ignored.
    _readings(db, p, [1000, 1100, 1300, 50, 150, 250])
    db.commit()

    out = queries.sustainability_rollup(db)
    assert out["pages"] == 500
    assert out["pages"] >= 0
    assert out["sheets"] >= 0


def test_since_filters_history(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    start = _now() - timedelta(days=5)
    # day0=0, day1=100, day2=200, day3=300, day4=400
    _readings(db, p, [0, 100, 200, 300, 400], start=start)
    db.commit()

    # Only readings on/after day3 (>=300) count: delta 300->400 == 100 pages.
    out = queries.sustainability_rollup(db, since=start + timedelta(days=3))
    assert out["pages"] == 100


def test_empty_fleet_is_zero_not_error(db):
    out = queries.sustainability_rollup(db)
    assert out["pages"] == 0
    assert out["sheets"] == 0
    assert out["co2_kg"] == 0
    assert out["trees"] == 0


# --------------------------------------------------------------------------- #
# Factor configurability via runtime SPECS
# --------------------------------------------------------------------------- #
def test_factors_are_configurable(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _readings(db, p, [0, 1000])  # 1000 pages
    db.commit()

    before = queries.sustainability_rollup(db)
    # Bump the CO2 factor and double the paper mass; sheets-per-page to 1.0 so
    # sheets == pages and the arithmetic is exact.
    save_settings(db, {
        "esg.sheets_per_page": "1.0",
        "esg.co2_g_per_sheet": "10.0",
        "esg.paper_g_per_sheet": "5.0",
    })
    after = queries.sustainability_rollup(db)

    assert after["duplex_nudge"] == 1.0
    assert after["sheets"] == 1000.0
    assert after["co2_kg"] == 1000.0 * 10.0 / 1000.0   # 10 kg
    assert after["paper_g"] == 1000.0 * 5.0            # 5000 g
    assert after["factors"]["co2_g_per_sheet"] == 10.0
    # The config actually changed the output.
    assert after["co2_kg"] != before["co2_kg"]


# --------------------------------------------------------------------------- #
# Tenant scoping
# --------------------------------------------------------------------------- #
def test_client_scope_isolates_totals(db):
    acme, acme_site = _seed_client(db, "Acme")
    beta, beta_site = _seed_client(db, "Beta")
    ap = _printer(db, acme, acme_site, ip="10.0.0.10")
    bp = _printer(db, beta, beta_site, ip="10.1.0.10")
    _readings(db, ap, [0, 100])    # Acme: 100 pages
    _readings(db, bp, [0, 400])    # Beta: 400 pages
    db.commit()

    fleet = queries.sustainability_rollup(db)
    acme_only = queries.sustainability_rollup(db, client_id=acme.id)
    beta_only = queries.sustainability_rollup(db, client_id=beta.id)

    assert acme_only["pages"] == 100
    assert beta_only["pages"] == 400
    assert fleet["pages"] == 500
    # A client only sees its own slice.
    assert acme_only["pages"] < fleet["pages"]
    assert acme_only["co2_kg"] < fleet["co2_kg"]


def test_pending_printers_excluded(db):
    client, site = _seed_client(db)
    approved = _printer(db, client, site, ip="10.0.0.10")
    _readings(db, approved, [0, 100])
    pending = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.99",
        discovery_state=m.DiscoveryState.pending, status=m.PrinterStatus.unknown,
    )
    db.add(pending)
    db.flush()
    _readings(db, pending, [0, 9999])  # would dominate if counted
    db.commit()

    out = queries.sustainability_rollup(db)
    assert out["pages"] == 100
    assert out["printers"] == 1


# --------------------------------------------------------------------------- #
# Weekly report section
# --------------------------------------------------------------------------- #
def test_weekly_report_has_esg_section(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _readings(db, p, [0, 500])
    db.commit()

    _subject, body = reports.build_weekly_summary(db)
    assert "Sustainability footprint" in body
    assert "CO2e" in body
    assert "Tree-equivalents" in body


# --------------------------------------------------------------------------- #
# Portal panel renders (tenant-scoped, logged in as client_readonly)
# --------------------------------------------------------------------------- #
def _portal_seed(db):
    client, site = _seed_client(db, "Acme")
    p = _printer(db, client, site)
    _readings(db, p, [1000, 1100, 1300])  # 300 pages
    db.add(m.User(
        username="acme-ro", password_hash=hash_password("pw"),
        role=m.UserRole.client_readonly, client_id=client.id,
    ))
    db.commit()
    return client


def _login(username="acme-ro") -> TestClient:
    cli = TestClient(app)
    cli.post("/login", data={"username": username, "password": "pw"},
             follow_redirects=False)
    return cli


def test_portal_renders_esg_panel(db):
    _portal_seed(db)
    cli = _login()
    body = cli.get("/portal").text
    assert "Sustainability footprint" in body
    assert "kg CO" in body            # the CO2e label
    assert "tree-equivalents" in body
    assert "sheets printed" in body


def test_portal_esg_is_tenant_scoped(db):
    """The panel must show only the logged-in client's pages, not the fleet."""
    client = _portal_seed(db)
    # Another client with far more print volume that must not bleed in.
    other, other_site = _seed_client(db, "Other Co")
    op = _printer(db, other, other_site, ip="10.99.0.10")
    _readings(db, op, [0, 100000])
    db.commit()

    scoped = queries.sustainability_rollup(db, client_id=client.id)
    assert scoped["pages"] == 300  # only Acme's prints

    cli = _login()
    body = cli.get("/portal").text
    # Acme's own page total appears; the huge "Other Co" number does not.
    assert "300 pages" in body
    assert "100,000" not in body
    # No fleet-wide total leaks into a single tenant's portal: Other Co's 100k
    # pages would dominate the sheet/CO2 figures if scoping were wrong.
    assert "84,915" not in body  # would be the fleet sheet total at 0.85x
