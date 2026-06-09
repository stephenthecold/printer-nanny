"""Overview enhancements: per-client rollup, recent activity, version footer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from central import models as m
from central import queries
from central.main import app
from central.security import hash_password


def _seed_two_clients(db):
    acme = m.Client(name="Acme")
    beta = m.Client(name="Beta")
    db.add_all([acme, beta])
    db.flush()
    acme_hq = m.Site(client_id=acme.id, name="HQ")
    acme_br = m.Site(client_id=acme.id, name="Branch")
    beta_hq = m.Site(client_id=beta.id, name="HQ")
    db.add_all([acme_hq, acme_br, beta_hq])
    db.flush()
    # Acme: 2 printers approved, 1 offline; 1 supply low; 1 open alert.
    p1 = m.Printer(
        client_id=acme.id, site_id=acme_hq.id, ip="10.0.0.10",
        brand="HP", model="M404", status=m.PrinterStatus.ok,
        discovery_state=m.DiscoveryState.approved,
    )
    p2 = m.Printer(
        client_id=acme.id, site_id=acme_hq.id, ip="10.0.0.11",
        brand="Brother", model="MFC", status=m.PrinterStatus.offline,
        discovery_state=m.DiscoveryState.approved,
    )
    # Beta: 1 printer approved, OK; 1 pending (excluded).
    p3 = m.Printer(
        client_id=beta.id, site_id=beta_hq.id, ip="10.0.1.10",
        brand="HP", model="M404", status=m.PrinterStatus.ok,
        discovery_state=m.DiscoveryState.approved,
    )
    p4 = m.Printer(
        client_id=beta.id, site_id=beta_hq.id, ip="10.0.1.99",
        status=m.PrinterStatus.unknown,
        discovery_state=m.DiscoveryState.pending,
    )
    db.add_all([p1, p2, p3, p4])
    db.flush()
    # Acme low supply on p1.
    db.add(m.Supply(printer_id=p1.id, type=m.SupplyType.toner, color="black",
                    level_pct=8.0))
    # Acme open alert on p2.
    db.add(m.Alert(
        printer_id=p2.id, type=m.AlertConditionType.offline_minutes,
        severity=m.EventSeverity.critical, state=m.AlertState.open,
        title="Printer offline", dedupe_key="p2-offline",
    ))
    db.commit()
    return acme, beta


def _login_admin(db) -> TestClient:
    db.add(m.User(
        username="admin", password_hash=hash_password("pw"),
        role=m.UserRole.admin,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"},
             follow_redirects=False)
    return cli


def test_per_client_rollup_counts_correctly(db):
    acme, beta = _seed_two_clients(db)
    rows = queries.per_client_rollup(db)
    by_name = {r["client"].name: r for r in rows}
    assert by_name["Acme"]["printer_count"] == 2  # approved only
    assert by_name["Acme"]["offline_count"] == 1
    assert by_name["Acme"]["low_supplies"] == 1
    assert by_name["Acme"]["open_alerts"] == 1
    assert by_name["Acme"]["sites_count"] == 2
    assert by_name["Beta"]["printer_count"] == 1  # pending excluded
    assert by_name["Beta"]["offline_count"] == 0
    assert by_name["Beta"]["low_supplies"] == 0
    assert by_name["Beta"]["open_alerts"] == 0
    assert by_name["Beta"]["sites_count"] == 1


def test_recent_activity_merges_events_alerts_pending(db):
    acme, beta = _seed_two_clients(db)
    # Add a warning event on Acme/p1.
    p1 = [p for p in acme.printers if p.ip == "10.0.0.10"][0]
    now = datetime.now(timezone.utc)
    db.add(m.PrinterEvent(
        printer_id=p1.id, ts=now, severity=m.EventSeverity.warning,
        source=m.EventSource.snmp_alert, message="Toner low",
    ))
    db.commit()
    rows = queries.recent_activity(db, 12)
    kinds = [r["kind"] for r in rows]
    # Saw at least one of each kind (warning event, the open alert, the pending printer).
    assert "event" in kinds
    assert "alert" in kinds
    assert "discovery" in kinds
    # Newest-first ordering: the most recent event we added is at the top.
    assert rows[0]["ts"] >= rows[-1]["ts"]


def test_recent_activity_respects_limit(db):
    _seed_two_clients(db)
    # 20 events all at slightly different timestamps.
    p = db.scalar(__import__("sqlalchemy").select(m.Printer).where(m.Printer.ip == "10.0.0.10"))
    base = datetime.now(timezone.utc)
    for i in range(20):
        db.add(m.PrinterEvent(
            printer_id=p.id, ts=base - timedelta(minutes=i),
            severity=m.EventSeverity.critical, source=m.EventSource.status,
            message=f"event {i}",
        ))
    db.commit()
    rows = queries.recent_activity(db, limit=5)
    assert len(rows) == 5


def test_overview_page_renders_rollup_and_footer_has_version(db):
    _seed_two_clients(db)
    cli = _login_admin(db)
    resp = cli.get("/", follow_redirects=False)
    assert resp.status_code == 200
    body = resp.text
    # Per-client rollup card is rendered.
    assert "Acme" in body
    assert "Beta" in body
    # The Acme offline count surfaces as the warning-styled cell.
    assert "Down" in body
    # Footer shows the central version.
    from central import __version__ as cv
    assert f"v{cv}" in body


def test_client_readonly_only_sees_their_client_in_rollup(db):
    acme, beta = _seed_two_clients(db)
    db.add(m.User(
        username="reader", password_hash=hash_password("pw"),
        role=m.UserRole.client_readonly, client_id=acme.id,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "reader", "password": "pw"},
             follow_redirects=False)
    resp = cli.get("/", follow_redirects=False)
    body = resp.text
    assert "Acme" in body
    assert "Beta" not in body
