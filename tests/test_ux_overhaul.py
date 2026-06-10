"""UX overhaul: supply-runway forecasts, conditional Approvals nav, grouped
settings (with the absent-checkbox clobber regression), discovery-on-agents.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from central import models as m
from central import queries, runtime
from central.main import app
from central.security import hash_password


def _admin(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("pw"),
                  role=m.UserRole.admin))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "pw"},
             follow_redirects=False)
    return cli


def _seed_printer(db, *, with_history: bool = False):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.10",
        brand="HP", model="M404", status=m.PrinterStatus.ok,
        discovery_state=m.DiscoveryState.approved, page_count=1000,
    )
    db.add(printer)
    db.flush()
    if with_history:
        # 60% -> 30% over 10 days = 3%/day -> ~10 days left from 30%.
        now = datetime.now(timezone.utc)
        for offset_days, level in ((10, 60.0), (5, 45.0), (0, 30.0)):
            db.add(m.Reading(
                printer_id=printer.id,
                ts=now - timedelta(days=offset_days),
                status=m.PrinterStatus.ok,
                supply_snapshot=[{"type": "toner", "color": "black",
                                  "level_pct": level}],
            ))
    db.commit()
    return client, site, printer


# ---------- supply runway ----------

def test_supply_runway_forecasts_days(db):
    _client, _site, printer = _seed_printer(db, with_history=True)
    runway = queries.supply_runway(db, [printer.id])
    assert runway[printer.id] == 10.0  # 30% left at 3%/day


def test_supply_runway_none_without_history(db):
    _client, _site, printer = _seed_printer(db, with_history=False)
    assert queries.supply_runway(db, [printer.id])[printer.id] is None


def test_client_page_shows_runway_not_page_count(db):
    client, _site, _printer = _seed_printer(db, with_history=True)
    cli = _admin(db)
    body = cli.get(f"/clients/{client.id}").text
    # Forecast column replaces the raw page count.
    assert "Supplies last" in body
    assert "~10 days" in body
    assert "1,000" not in body  # page count no longer in the listing


def test_client_page_runway_unknown_renders_dash(db):
    client, _site, _printer = _seed_printer(db, with_history=False)
    cli = _admin(db)
    body = cli.get(f"/clients/{client.id}").text
    assert "Not enough polling history" in body


def test_client_page_site_chips(db):
    client, _site, printer = _seed_printer(db, with_history=False)
    printer.status = m.PrinterStatus.offline
    db.add(m.Supply(printer_id=printer.id, type=m.SupplyType.toner,
                    color="black", level_pct=5.0))
    db.commit()
    cli = _admin(db)
    body = cli.get(f"/clients/{client.id}").text
    assert "1 down" in body
    assert "1 low supply" in body


# ---------- conditional approvals nav ----------

def test_approvals_nav_hidden_without_pending(db):
    _seed_printer(db)
    cli = _admin(db)
    body = cli.get("/").text
    assert "/approvals" not in body


def test_approvals_nav_shows_with_count_when_pending(db):
    client, site, _p = _seed_printer(db)
    db.add(m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.99",
        discovery_state=m.DiscoveryState.pending,
    ))
    db.commit()
    cli = _admin(db)
    body = cli.get("/").text
    assert "/approvals" in body
    assert ">1</span>" in body  # count badge


# ---------- grouped settings ----------

def test_settings_page_shows_only_active_group_sections(db):
    cli = _admin(db)
    branding = cli.get("/settings?group=branding").text
    assert "App name" in branding
    assert "SMTP host" not in branding  # notifications group not rendered
    notifications = cli.get("/settings?group=notifications").text
    assert "SMTP host" in notifications
    assert "App name" not in notifications
    # Tab nav lists every group on both pages.
    for label in ("Branding", "Notifications", "Alerts &amp; Reports",
                  "Polling &amp; SNMP", "Authentication", "Agents"):
        assert label in branding


def test_settings_unknown_group_falls_back_to_default(db):
    cli = _admin(db)
    resp = cli.get("/settings?group=nonsense")
    assert resp.status_code == 200
    assert "App name" in resp.text  # default = branding


def test_group_scoped_save_does_not_clobber_other_groups_bools(db):
    """THE regression that group-scoped saving exists to prevent: posting the
    branding form (which contains no email.enabled checkbox) must not flip
    email.enabled back to False."""
    runtime.save_settings(db, {"email.enabled": "on",
                               "email.default_recipients": "ops@x.com"})
    assert runtime.load_settings(db)["email.enabled"] is True

    cli = _admin(db)
    resp = cli.post("/settings", data={
        "_group": "branding",
        "app.name": "CT Printers",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings?group=branding"

    values = runtime.load_settings(db)
    assert values["app.name"] == "CT Printers"          # branding change took
    assert values["email.enabled"] is True              # other group untouched
    assert values["email.default_recipients"] == "ops@x.com"


def test_group_scoped_save_unchecks_bools_within_its_own_group(db):
    """Within the posted group, an absent checkbox still means unchecked."""
    runtime.save_settings(db, {"email.enabled": "on"})
    cli = _admin(db)
    cli.post("/settings", data={
        "_group": "notifications",
        "smtp.host": "mail.example.com",
        # email.enabled checkbox absent -> operator unchecked it
    }, follow_redirects=False)
    assert runtime.load_settings(db)["email.enabled"] is False


# ---------- overview clarity ----------

def test_recent_activity_includes_printer_label(db):
    _client, _site, printer = _seed_printer(db)
    db.add(m.PrinterEvent(
        printer_id=printer.id, severity=m.EventSeverity.warning,
        source=m.EventSource.snmp_alert, message="Replace Drum",
    ))
    db.commit()
    rows = queries.recent_activity(db, 10)
    event_rows = [r for r in rows if r["kind"] == "event"]
    assert any("Replace Drum — M404 @ 10.0.0.10" == r["message"] for r in event_rows)


def test_recent_activity_dedupes_identical_messages(db):
    _client, _site, printer = _seed_printer(db)
    for _ in range(5):
        db.add(m.PrinterEvent(
            printer_id=printer.id, severity=m.EventSeverity.warning,
            source=m.EventSource.snmp_alert, message="Replace Drum",
        ))
    db.commit()
    rows = queries.recent_activity(db, 10)
    matching = [r for r in rows if "Replace Drum" in r["message"]]
    assert len(matching) == 1
