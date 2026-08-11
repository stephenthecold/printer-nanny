"""Staff-reported issues, immediate delivery, dedupe, and manual lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import runtime
from central.channels.base import ChannelResult, Notification
from central.channels.email import EmailChannel
from central.channels.freescout import FreeScoutChannel
from central.main import app
from central.security import hash_password
from central.worker import jobs


def _fleet(db):
    client = m.Client(name="LCR", timezone="UTC")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="Downtown")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip="10.0.0.20",
        model="HP LaserJet M611dn",
        display_name="Reception",
        location="Front desk alcove",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.commit()
    return client, printer


def _login(db, *, username="admin", role=m.UserRole.admin, client_id=None):
    db.add(
        m.User(
            username=username,
            password_hash=hash_password("pw12345678"),
            role=role,
            client_id=client_id,
        )
    )
    db.commit()
    http = TestClient(app)
    http.post(
        "/login",
        data={"username": username, "password": "pw12345678"},
        follow_redirects=False,
    )
    return http


def _enable_channels(db):
    runtime.save_settings(
        db,
        {
            "freescout.enabled": "on",
            "freescout.base_url": "https://help.example.com",
            "freescout.api_key": "secret",
            "freescout.mailbox_id": "1",
            "email.enabled": "on",
            "email.default_recipients": "techs@example.com",
            "smtp.host": "smtp.example.com",
            "smtp.from": "printer@example.com",
        },
        sections={"FreeScout", "Email (SMTP)"},
    )


def test_critical_manual_issue_immediately_notifies_email_and_freescout(
    db, monkeypatch
):
    _, printer = _fleet(db)
    _enable_channels(db)
    sent = []

    def freescout_send(_self, note):
        sent.append(("freescout", note))
        return ChannelResult(ok=True, detail="ticket created", external_ref="4242")

    def email_send(_self, note):
        sent.append(("email", note))
        return ChannelResult(ok=True, detail="sent")

    monkeypatch.setattr(FreeScoutChannel, "send", freescout_send)
    monkeypatch.setattr(EmailChannel, "send", email_send)
    http = _login(db)
    happened = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    response = http.post(
        "/alerts/manual",
        data={
            "printer_id": printer.id,
            "impact": "stopped",
            "title": "Paper jam will not clear",
            "detail": "Tray 2 reports jam after paper was removed.",
            "occurred_at": happened.strftime("%Y-%m-%dT%H:%M"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    alert = db.scalar(select(m.Alert))
    assert alert.type == m.AlertConditionType.manual_issue
    assert alert.severity == m.EventSeverity.critical
    assert alert.external_ref == "4242"
    assert {name for name, _ in sent} == {"email", "freescout"}
    for _, note in sent:
        assert note.severity == "critical"
        assert note.printer_label == "HP LaserJet M611dn @ 10.0.0.20"
        assert "Occurred:" in note.body
        assert note.site_name == "Downtown · Front desk alcove"
    deliveries = list(db.scalars(select(m.NotificationDelivery)))
    assert len(deliveries) == 2
    assert {delivery.status for delivery in deliveries} == {m.DeliveryStatus.delivered}
    assert "manual_issue.create" in {
        row.action for row in db.scalars(select(m.AuditLog))
    }
    closed = []
    monkeypatch.setattr(
        FreeScoutChannel,
        "close_ticket",
        lambda _self, ref, _note: closed.append(ref),
    )
    http.post(f"/alerts/{alert.id}/resolve", follow_redirects=False)
    db.expire_all()
    assert db.get(m.Alert, alert.id).state == m.AlertState.resolved
    assert closed == []  # technician closes the FreeScout ticket deliberately


def test_same_printer_and_minute_share_ticket_but_later_time_is_new(db, monkeypatch):
    _, printer = _fleet(db)
    monkeypatch.setattr(
        FreeScoutChannel,
        "send",
        lambda _self, _note: ChannelResult(ok=True, detail="sent", external_ref="1"),
    )
    http = _login(db)
    happened = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    payload = {
        "printer_id": printer.id,
        "impact": "degraded",
        "title": "Smearing output",
        "detail": "Left edge",
        "occurred_at": happened.strftime("%Y-%m-%dT%H:%M"),
    }
    http.post("/alerts/manual", data=payload, follow_redirects=False)
    payload["title"] = "Output cannot be used"
    payload["detail"] = "A second observer described the same occurrence differently"
    http.post("/alerts/manual", data=payload, follow_redirects=False)
    assert len(list(db.scalars(select(m.Alert)))) == 1
    assert "manual_issue.duplicate" in {
        row.action for row in db.scalars(select(m.AuditLog))
    }

    payload["occurred_at"] = (happened - timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M"
    )
    http.post("/alerts/manual", data=payload, follow_redirects=False)
    assert len(list(db.scalars(select(m.Alert)))) == 2


def test_manual_issue_is_not_auto_resolved_and_dashboard_copy_is_escaped(db):
    _, printer = _fleet(db)
    http = _login(db)
    happened = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    http.post(
        "/alerts/manual",
        data={
            "printer_id": printer.id,
            "impact": "information",
            "title": "<script>alert(1)</script>",
            "detail": "<img src=x onerror=alert(2)>",
            "occurred_at": happened.strftime("%Y-%m-%dT%H:%M"),
        },
        follow_redirects=False,
    )
    jobs.evaluate_alerts(db)
    alert = db.scalar(select(m.Alert))
    assert alert.state == m.AlertState.open
    body = http.get("/alerts").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "Report a printer issue" in body
    assert 'data-timezone="UTC"' in body
    assert "Reception · 10.0.0.20 · Front desk alcove" in body
    assert "data-issue-time" in body


def test_readonly_cannot_report_or_resolve_manual_issues(db):
    client, printer = _fleet(db)
    http = _login(
        db,
        username="viewer",
        role=m.UserRole.client_readonly,
        client_id=client.id,
    )
    response = http.post(
        "/alerts/manual",
        data={
            "printer_id": printer.id,
            "impact": "stopped",
            "title": "No print",
            "occurred_at": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert db.scalar(select(m.Alert)) is None


def test_critical_freescout_payload_uses_supported_urgent_tag():
    payload = FreeScoutChannel("FreeScout", {}).build_payload(
        Notification(title="Printer stopped", body="Jam", severity="critical")
    )
    assert payload["subject"].startswith("[CRITICAL]")
    assert payload["tags"] == ["printer-nanny", "critical", "urgent"]
