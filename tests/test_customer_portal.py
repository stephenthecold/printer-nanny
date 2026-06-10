"""Customer portal for client_readonly users."""

from __future__ import annotations

from fastapi.testclient import TestClient

from central import models as m
from central.main import app
from central.runtime import save_settings
from central.security import hash_password


def _seed(db):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.10",
        brand="HP", model="M404", display_name="Front Desk",
        status=m.PrinterStatus.ok, discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.add(m.User(
        username="acme-ro", password_hash=hash_password("pw"),
        role=m.UserRole.client_readonly, client_id=client.id,
    ))
    db.commit()
    return client, site, printer


def _login(username="acme-ro") -> TestClient:
    cli = TestClient(app)
    cli.post("/login", data={"username": username, "password": "pw"},
             follow_redirects=False)
    return cli


def test_client_readonly_root_redirects_to_portal(db):
    _seed(db)
    cli = _login()
    resp = cli.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal"


def test_portal_shows_their_fleet_with_friendly_names(db):
    _seed(db)
    cli = _login()
    body = cli.get("/portal").text
    assert "Front Desk" in body            # friendly name, not model
    assert "Report a problem" in body
    assert "Send to support" in body       # form present


def test_portal_only_shows_users_client_data(db):
    """Another client's printer and supplies must not leak."""
    client, _site, _printer = _seed(db)
    other = m.Client(name="Other Co")
    db.add(other)
    db.flush()
    other_site = m.Site(client_id=other.id, name="HQ")
    db.add(other_site)
    db.flush()
    other_printer = m.Printer(
        client_id=other.id, site_id=other_site.id, ip="10.99.0.10",
        brand="HP", model="OtherModel", display_name="Other Client Printer",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(other_printer)
    db.flush()
    db.add(m.Supply(printer_id=other_printer.id, type=m.SupplyType.toner,
                    color="black", level_pct=2.0))
    db.commit()
    cli = _login()
    body = cli.get("/portal").text
    assert "Other Client Printer" not in body
    assert "10.99.0.10" not in body


def test_portal_report_falls_back_to_email_when_no_freescout(db):
    """Without FreeScout configured, the form still reaches a destination
    (the configured alert-email recipients) instead of silently dropping."""
    _seed(db)
    save_settings(db, {"email.default_recipients": "ops@msp.local"})
    sent = []

    from central.channels import email as _email_mod
    from central.channels.base import ChannelResult

    real_send = _email_mod.EmailChannel.send

    def fake_send(self, note):
        sent.append({"to": self.config.get("to"), "title": note.title,
                     "body": note.body})
        return ChannelResult(ok=True, detail="ok")

    _email_mod.EmailChannel.send = fake_send
    try:
        cli = _login()
        resp = cli.post("/portal/report", data={
            "printer_id": "", "subject": "Stuck paper",
            "body": "Front desk is jammed",
        }, follow_redirects=False)
        assert resp.status_code == 303
        assert len(sent) == 1
        assert sent[0]["to"] == "ops@msp.local"
        assert "Stuck paper" in sent[0]["title"]
        assert "Front desk is jammed" in sent[0]["body"]
    finally:
        _email_mod.EmailChannel.send = real_send


def test_portal_report_audited(db):
    _seed(db)
    save_settings(db, {"email.default_recipients": "ops@msp.local"})

    from central.channels import email as _email_mod
    from central.channels.base import ChannelResult

    real_send = _email_mod.EmailChannel.send
    _email_mod.EmailChannel.send = lambda self, note: ChannelResult(ok=True, detail="ok")
    try:
        cli = _login()
        cli.post("/portal/report", data={
            "printer_id": "", "subject": "X", "body": "Y",
        }, follow_redirects=False)
        row = db.execute(
            __import__("sqlalchemy").select(m.AuditLog).where(m.AuditLog.action == "portal.report")
        ).scalar_one()
        assert row.username == "acme-ro"
        assert "client:" in (row.target or "")
    finally:
        _email_mod.EmailChannel.send = real_send
