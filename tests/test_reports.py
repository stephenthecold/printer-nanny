"""Scheduled reports: content builders, due-check/marker logic, delivery seam."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from central import models as m
from central import reports
from central.channels import Notification
from central.channels.email import EmailChannel
from central.runtime import save_settings


def _seed_fleet(db):
    acme = m.Client(name="Acme")
    beta = m.Client(name="Beta")
    db.add_all([acme, beta])
    db.flush()
    acme_hq = m.Site(client_id=acme.id, name="HQ")
    beta_hq = m.Site(client_id=beta.id, name="HQ")
    db.add_all([acme_hq, beta_hq])
    db.flush()
    p1 = m.Printer(
        client_id=acme.id, site_id=acme_hq.id, ip="10.0.0.10",
        brand="HP", model="M404", serial="S1", page_count=12345,
        status=m.PrinterStatus.ok, discovery_state=m.DiscoveryState.approved,
    )
    p2 = m.Printer(
        client_id=beta.id, site_id=beta_hq.id, ip="10.0.1.10",
        brand="Brother", model="HL-L2460DW", serial="S2", page_count=1547,
        status=m.PrinterStatus.offline, discovery_state=m.DiscoveryState.approved,
    )
    pending = m.Printer(
        client_id=beta.id, site_id=beta_hq.id, ip="10.0.1.99",
        discovery_state=m.DiscoveryState.pending,
    )
    db.add_all([p1, p2, pending])
    db.flush()
    db.add(m.Supply(printer_id=p1.id, type=m.SupplyType.toner, color="black",
                    level_pct=8.0, description="Black Cartridge"))
    db.commit()


# ---------- content builders ----------

def test_weekly_summary_contains_fleet_and_rollup(db):
    _seed_fleet(db)
    subject, body = reports.build_weekly_summary(db)
    assert "2 printers" in subject
    assert "Printers monitored : 2" in body
    assert "Acme" in body and "Beta" in body
    # Beta's offline printer counts as down.
    assert "down=1" in body
    # The 8% black cartridge shows in low supplies with its location.
    assert "Black Cartridge" in body
    assert "10.0.0.10" in body


def test_monthly_csv_has_billing_columns_and_excludes_pending(db):
    _seed_fleet(db)
    raw = reports.build_monthly_billing_csv(db).decode("utf-8")
    rows = list(csv.reader(io.StringIO(raw)))
    header, data = rows[0], rows[1:]
    assert header[:3] == ["client", "site", "ip"]
    assert "page_count" in header
    ips = [r[header.index("ip")] for r in data]
    assert sorted(ips) == ["10.0.0.10", "10.0.1.10"]  # pending 10.0.1.99 excluded
    by_ip = {r[header.index("ip")]: r for r in data}
    assert by_ip["10.0.0.10"][header.index("page_count")] == "12345"
    assert by_ip["10.0.1.10"][header.index("page_count")] == "1547"


# ---------- due-check / marker logic ----------

def _stub_delivery(monkeypatch, ok=True):
    sent = []

    def fake(db, rt, subject, body, attachments=None):
        sent.append({"subject": subject, "body": body, "attachments": attachments})
        return ok, "stubbed"

    monkeypatch.setattr(reports, "_deliver", fake)
    return sent


def test_weekly_skipped_when_disabled(db, monkeypatch):
    sent = _stub_delivery(monkeypatch)
    out = reports.run_scheduled_reports(
        db, now=datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc),  # a Monday
    )
    assert out["weekly_report"] == "skipped"
    assert sent == []


def test_weekly_fires_once_on_configured_day(db, monkeypatch):
    _seed_fleet(db)
    save_settings(db, {"reports.weekly_day": "mon", "reports.send_hour": "7"})
    save_settings(db, {"reports.weekly_enabled": "on"})
    sent = _stub_delivery(monkeypatch)
    monday_9am = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)

    # Before the send hour: skipped.
    out = reports.run_scheduled_reports(
        db, now=datetime(2026, 6, 8, 5, 0, tzinfo=timezone.utc))
    assert out["weekly_report"] == "skipped"

    # Wrong day: skipped.
    out = reports.run_scheduled_reports(
        db, now=datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc))  # Tuesday
    assert out["weekly_report"] == "skipped"

    # Right day at/after the hour: sends.
    out = reports.run_scheduled_reports(db, now=monday_9am)
    assert out["weekly_report"] == "sent"
    assert len(sent) == 1
    assert "Weekly fleet summary" in sent[0]["subject"]

    # Second cycle the same day: marker blocks a double-send.
    out = reports.run_scheduled_reports(db, now=monday_9am)
    assert out["weekly_report"] == "skipped"
    assert len(sent) == 1

    # Next Monday: fires again.
    out = reports.run_scheduled_reports(
        db, now=datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc))
    assert out["weekly_report"] == "sent"
    assert len(sent) == 2


def test_weekly_failure_leaves_marker_unset_for_retry(db, monkeypatch):
    _seed_fleet(db)
    save_settings(db, {"reports.weekly_day": "mon"})
    save_settings(db, {"reports.weekly_enabled": "on"})
    _stub_delivery(monkeypatch, ok=False)
    monday = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)
    out = reports.run_scheduled_reports(db, now=monday)
    assert out["weekly_report"].startswith("failed")
    # Marker not set: the next cycle retries instead of silently dropping a week.
    assert reports._get_marker(db, reports.MARKER_WEEKLY) is None


def test_monthly_fires_with_csv_attachment_on_configured_day(db, monkeypatch):
    _seed_fleet(db)
    save_settings(db, {"reports.monthly_day": "10"})
    save_settings(db, {"reports.monthly_enabled": "on"})
    sent = _stub_delivery(monkeypatch)
    out = reports.run_scheduled_reports(
        db, now=datetime(2026, 6, 10, 8, 0, tzinfo=timezone.utc))
    assert out["monthly_report"] == "sent"
    assert len(sent) == 1
    (filename, content_type, payload) = sent[0]["attachments"][0]
    assert filename == "printer-nanny-billing-2026-06.csv"
    assert content_type == "text/csv"
    assert b"10.0.0.10" in payload
    # Wrong day of month: skipped.
    out = reports.run_scheduled_reports(
        db, now=datetime(2026, 7, 11, 8, 0, tzinfo=timezone.utc))
    assert out["monthly_report"] == "skipped"


# ---------- delivery ----------

def test_deliver_requires_recipients(db):
    from central.runtime import load_settings
    ok, detail = reports._deliver(db, load_settings(db), "s", "b")
    assert ok is False
    assert "recipients" in detail


def test_deliver_falls_back_to_alert_recipients(db, monkeypatch):
    """reports.recipients empty -> email.default_recipients is used."""
    captured = {}

    def fake_send(self, note):
        captured["to"] = self.config.get("to")
        captured["note"] = note
        from central.channels.base import ChannelResult
        return ChannelResult(ok=True, detail="ok")

    monkeypatch.setattr(EmailChannel, "send", fake_send)
    rt = {"reports.recipients": "", "email.default_recipients": "ops@msp.com"}
    ok, _ = reports._deliver(db, rt, "subject", "body")
    assert ok is True
    assert captured["to"] == "ops@msp.com"
    assert captured["note"].title == "subject"


def test_email_message_carries_attachment():
    """EmailChannel.build_message turns Notification.attachments into real
    MIME attachments."""
    ch = EmailChannel("r", {"to": "a@x.com", "from": "noc@x.com"})
    note = Notification(
        title="Monthly billing report", body="see attachment", severity="info",
        attachments=[("billing.csv", "text/csv", b"client,ip\nAcme,10.0.0.1\n")],
    )
    msg = ch.build_message(note)
    parts = [p for p in msg.iter_attachments()]
    assert len(parts) == 1
    assert parts[0].get_filename() == "billing.csv"
    assert parts[0].get_content_type() == "text/csv"
    assert b"Acme,10.0.0.1" in parts[0].get_content().encode()
