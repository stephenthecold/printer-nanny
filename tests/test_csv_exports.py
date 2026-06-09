"""CSV export endpoints: inventory, supplies, alerts.

Operators pull these for billing, supply ordering, and PSA imports. Tenant
scoping must hold (client_readonly users only see their pinned client).
"""

from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient

from central import models as m
from central.main import app
from central.security import hash_password


def _login(db, *, role: m.UserRole = m.UserRole.admin, client_id: int | None = None) -> TestClient:
    pwd = "pw" + role.value
    db.add(m.User(
        username=f"u_{role.value}",
        password_hash=hash_password(pwd),
        role=role,
        client_id=client_id,
    ))
    db.commit()
    cli = TestClient(app)
    resp = cli.post(
        "/login", data={"username": f"u_{role.value}", "password": pwd},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return cli


def _seed_two_clients_with_printers(db) -> tuple[m.Client, m.Client]:
    acme = m.Client(name="Acme")
    beta = m.Client(name="Beta")
    db.add_all([acme, beta])
    db.flush()
    acme_site = m.Site(client_id=acme.id, name="HQ")
    beta_site = m.Site(client_id=beta.id, name="HQ")
    db.add_all([acme_site, beta_site])
    db.flush()

    db.add_all([
        m.Printer(
            client_id=acme.id, site_id=acme_site.id, ip="10.0.0.10",
            brand="Brother", model="MFC-L8900CDW", serial="ACME001",
            status=m.PrinterStatus.ok, discovery_state=m.DiscoveryState.approved,
            page_count=12345,
        ),
        m.Printer(
            client_id=acme.id, site_id=acme_site.id, ip="10.0.0.11",
            brand="Lexmark", model="MX622", serial="ACME002",
            status=m.PrinterStatus.warning, discovery_state=m.DiscoveryState.approved,
        ),
        # Beta has one printer + one pending (should NOT appear in inventory).
        m.Printer(
            client_id=beta.id, site_id=beta_site.id, ip="10.0.1.10",
            brand="HP", model="LJ M404", serial="BETA001",
            status=m.PrinterStatus.ok, discovery_state=m.DiscoveryState.approved,
        ),
        m.Printer(
            client_id=beta.id, site_id=beta_site.id, ip="10.0.1.99",
            brand="HP", model="LJ M404", serial="BETA002",
            status=m.PrinterStatus.unknown, discovery_state=m.DiscoveryState.pending,
        ),
    ])
    db.commit()
    return acme, beta


def _parse_csv(raw: str) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(raw))
    rows = list(reader)
    return rows[0], rows[1:]


# ---- Inventory ----

def test_inventory_export_admin_sees_all_approved_only(db):
    _seed_two_clients_with_printers(db)
    http = _login(db, role=m.UserRole.admin)
    r = http.get("/api/v1/reports/export/inventory.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "inventory" in r.headers["content-disposition"]

    header, rows = _parse_csv(r.text)
    assert header[0:5] == ["client", "site", "ip", "hostname", "brand"]
    ips = [row[2] for row in rows]
    # 3 approved across both clients; the pending Beta printer must be excluded.
    assert sorted(ips) == ["10.0.0.10", "10.0.0.11", "10.0.1.10"]


def test_inventory_export_client_id_filter(db):
    acme, beta = _seed_two_clients_with_printers(db)
    http = _login(db, role=m.UserRole.admin)
    r = http.get(f"/api/v1/reports/export/inventory.csv?client_id={beta.id}")
    assert r.status_code == 200
    _, rows = _parse_csv(r.text)
    assert [row[2] for row in rows] == ["10.0.1.10"]
    assert all(row[0] == "Beta" for row in rows)


def test_inventory_export_client_readonly_user_sees_only_their_client(db):
    acme, beta = _seed_two_clients_with_printers(db)
    http = _login(db, role=m.UserRole.client_readonly, client_id=acme.id)
    r = http.get("/api/v1/reports/export/inventory.csv")
    assert r.status_code == 200
    _, rows = _parse_csv(r.text)
    # Acme has 2 approved printers; Beta's row must NOT appear.
    assert {row[2] for row in rows} == {"10.0.0.10", "10.0.0.11"}
    assert all(row[0] == "Acme" for row in rows)


def test_inventory_export_client_readonly_cross_client_denied(db):
    acme, beta = _seed_two_clients_with_printers(db)
    http = _login(db, role=m.UserRole.client_readonly, client_id=acme.id)
    r = http.get(f"/api/v1/reports/export/inventory.csv?client_id={beta.id}")
    assert r.status_code == 403


def test_inventory_export_unauthenticated_401(db):
    _seed_two_clients_with_printers(db)
    cli = TestClient(app)
    r = cli.get("/api/v1/reports/export/inventory.csv")
    assert r.status_code == 401


# ---- Supplies ----

def test_supplies_export_includes_status_note_for_bucket_state(db):
    acme, _ = _seed_two_clients_with_printers(db)
    printer = db.scalar(
        __import__("sqlalchemy").select(m.Printer).where(m.Printer.ip == "10.0.0.10")
    )
    db.add_all([
        m.Supply(
            printer_id=printer.id, type=m.SupplyType.toner, color="black",
            description="Black Toner Cartridge", level_pct=45.0,
        ),
        m.Supply(
            printer_id=printer.id, type=m.SupplyType.toner, color="cyan",
            description="Cyan Toner Cartridge", level_pct=None,
            status_note="some remaining",
        ),
    ])
    db.commit()

    http = _login(db, role=m.UserRole.admin)
    r = http.get("/api/v1/reports/export/supplies.csv")
    assert r.status_code == 200
    header, rows = _parse_csv(r.text)
    assert "level_pct" in header
    assert "status_note" in header
    by_color = {row[header.index("color")]: row for row in rows}
    assert by_color["black"][header.index("level_pct")] == "45.0"
    assert by_color["cyan"][header.index("status_note")] == "some remaining"


# ---- Alerts ----

def test_alerts_export_open_by_default(db):
    acme, _ = _seed_two_clients_with_printers(db)
    printer = db.scalar(
        __import__("sqlalchemy").select(m.Printer).where(m.Printer.ip == "10.0.0.11")
    )
    from datetime import datetime, timezone
    db.add_all([
        m.Alert(
            printer_id=printer.id,
            type=m.AlertConditionType.supply_below,
            severity=m.EventSeverity.warning,
            state=m.AlertState.open,
            title="Black toner is low",
            detail="Level at 10%",
            dedupe_key="p-supply_low-toner-black",
        ),
        m.Alert(
            printer_id=printer.id,
            type=m.AlertConditionType.offline_minutes,
            severity=m.EventSeverity.critical,
            state=m.AlertState.resolved,
            title="Printer offline",
            dedupe_key="p-offline",
            resolved_at=datetime.now(timezone.utc),
        ),
    ])
    db.commit()

    http = _login(db, role=m.UserRole.admin)
    r = http.get("/api/v1/reports/export/alerts.csv")
    assert r.status_code == 200
    header, rows = _parse_csv(r.text)
    states = [row[header.index("state")] for row in rows]
    assert "open" in states
    assert "resolved" not in states  # filtered out by default

    r2 = http.get("/api/v1/reports/export/alerts.csv?include_resolved=true")
    _, rows2 = _parse_csv(r2.text)
    states2 = [row[header.index("state")] for row in rows2]
    assert "resolved" in states2
