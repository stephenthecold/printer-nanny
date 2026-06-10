"""Friendly printer names + the per-client/per-site overview additions.

display_name must flow everywhere a printer is named: dashboards, alert
titles (worker), recent activity, weekly reports -- so notifications say
"Front Desk toner low" instead of a model number and IP.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import queries
from central.main import app
from central.security import hash_password
from central.worker import jobs


def _admin(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("pw"),
                  role=m.UserRole.admin))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"},
             follow_redirects=False)
    return cli


def _seed(db, *, display_name="Front Desk"):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.10",
        brand="Brother", model="HL-L2460DW", display_name=display_name,
        status=m.PrinterStatus.ok, discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.commit()
    return client, site, printer


# ---------- display_name in labels ----------

def test_alert_title_uses_friendly_name(db):
    """The worker's alert pipeline names printers with display_name first --
    this is what lands in alert emails / Slack."""
    _client, _site, printer = _seed(db)
    db.add(m.Supply(printer_id=printer.id, type=m.SupplyType.toner,
                    color="black", level_pct=4.0))
    db.add(m.AlertRule(
        name="low supply", condition_type=m.AlertConditionType.supply_below,
        threshold=10.0, severity=m.EventSeverity.warning,
    ))
    db.commit()
    jobs.evaluate_alerts(db)
    alert = db.scalar(select(m.Alert))
    assert alert is not None
    assert "Front Desk @ 10.0.0.10" in alert.title
    assert "HL-L2460DW" not in alert.title  # friendly name replaced the model


def test_recent_activity_uses_friendly_name(db):
    _client, _site, printer = _seed(db)
    db.add(m.PrinterEvent(
        printer_id=printer.id, severity=m.EventSeverity.warning,
        source=m.EventSource.snmp_alert, message="Replace Drum",
    ))
    db.commit()
    rows = queries.recent_activity(db, 10)
    assert any("Replace Drum — Front Desk @ 10.0.0.10" == r["message"]
               for r in rows if r["kind"] == "event")


def test_client_page_prefers_friendly_name(db):
    client, _site, _printer = _seed(db)
    cli = _admin(db)
    body = cli.get(f"/clients/{client.id}").text
    assert "Front Desk" in body


def test_printer_form_round_trips_display_name(db):
    client, site, printer = _seed(db, display_name=None)
    cli = _admin(db)
    resp = cli.post(f"/manage/printers/{printer.id}", data={
        "site_id": str(site.id), "ip": printer.ip,
        "display_name": "Lab Copier",
        "hostname": "", "brand": "Brother", "model": "HL-L2460DW",
        "serial": "", "location": "", "snmp_version": "2c",
        "snmp_community": "public", "asset_tag": "", "tags": "", "notes": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    db.refresh(printer)
    assert printer.display_name == "Lab Copier"
    # Clearing it falls back to model in displays.
    cli.post(f"/manage/printers/{printer.id}", data={
        "site_id": str(site.id), "ip": printer.ip, "display_name": "",
        "hostname": "", "brand": "Brother", "model": "HL-L2460DW",
        "serial": "", "location": "", "snmp_version": "2c",
        "snmp_community": "public", "asset_tag": "", "tags": "", "notes": "",
    }, follow_redirects=False)
    db.refresh(printer)
    assert printer.display_name is None


def test_inventory_csv_includes_display_name_as_last_column(db):
    """Appended LAST so existing CSV consumers' column indexes stay stable."""
    _seed(db)
    cli = _admin(db)
    resp = cli.get("/api/v1/reports/export/inventory.csv")
    assert resp.status_code == 200
    import csv as _csv
    import io as _io
    rows = list(_csv.reader(_io.StringIO(resp.text)))
    header, data = rows[0], rows[1:]
    assert header[-1] == "display_name"
    assert data[0][-1] == "Front Desk"


# ---------- per-client / per-site overview ----------

def test_client_page_summary_cards(db):
    client, site, printer = _seed(db)
    printer.status = m.PrinterStatus.warning
    # A second, downed printer with a low supply and an open alert.
    p2 = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.11",
        model="M404", status=m.PrinterStatus.offline,
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(p2)
    db.flush()
    db.add(m.Supply(printer_id=p2.id, type=m.SupplyType.toner,
                    color="black", level_pct=3.0))
    db.add(m.Alert(
        printer_id=p2.id, type=m.AlertConditionType.supply_below,
        severity=m.EventSeverity.warning, state=m.AlertState.open,
        title="Low black", dedupe_key="x",
    ))
    db.commit()
    cli = _admin(db)
    body = cli.get(f"/clients/{client.id}").text
    # Client-level summary header cards.
    assert "Printers" in body and "Down" in body and "Warnings" in body
    assert "Low supplies" in body
    assert "Next supply order due" in body
    # Site chips include the warning count now.
    assert "1 warning" in body
    assert "1 down" in body


def test_client_page_lowest_supply_column(db):
    client, _site, printer = _seed(db)
    db.add(m.Supply(printer_id=printer.id, type=m.SupplyType.toner,
                    color="black", level_pct=42.0, description="Black Toner"))
    db.add(m.Supply(printer_id=printer.id, type=m.SupplyType.drum,
                    color=None, level_pct=90.0, description="Drum Unit"))
    db.commit()
    cli = _admin(db)
    body = cli.get(f"/clients/{client.id}").text
    assert "Lowest supply" in body
    assert "42%" in body          # the most-depleted supply's bar value
    assert 'title="Black Toner"' in body  # which supply it is, on hover


def test_runway_stable_when_history_but_no_depletion(db):
    """Enough history + flat levels -> 'stable', not a confusing dash."""
    client, _site, printer = _seed(db)
    now = datetime.now(timezone.utc)
    for offset_days in (5, 3, 0):  # flat 80% for 5 days
        db.add(m.Reading(
            printer_id=printer.id, ts=now - timedelta(days=offset_days),
            status=m.PrinterStatus.ok,
            supply_snapshot=[{"type": "toner", "color": "black",
                              "level_pct": 80.0}],
        ))
    db.commit()
    cli = _admin(db)
    body = cli.get(f"/clients/{client.id}").text
    assert "stable" in body


def test_runway_estimate_eta_while_history_builds(db):
    """Only one day of history -> tell the operator when the estimate lands."""
    client, _site, printer = _seed(db)
    now = datetime.now(timezone.utc)
    db.add(m.Reading(
        printer_id=printer.id, ts=now - timedelta(days=1),
        status=m.PrinterStatus.ok,
        supply_snapshot=[{"type": "toner", "color": "black", "level_pct": 80.0}],
    ))
    db.commit()
    cli = _admin(db)
    body = cli.get(f"/clients/{client.id}").text
    assert "est. in ~2d" in body
