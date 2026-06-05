"""DB-backed settings store: defaults, save/load, secret handling, bool checkboxes."""

from __future__ import annotations

from central import runtime
from central.worker import jobs


def test_defaults_present():
    defaults = runtime.default_settings()
    assert defaults["smtp.host"] is not None
    assert defaults["alerts.low_supply_pct"] == 20.0
    assert defaults["oidc.enabled"] is False


def test_save_and_load_roundtrip(db):
    runtime.save_settings(db, {
        "smtp.host": "smtp.acme.test",
        "smtp.port": "2525",
        "alerts.low_supply_pct": "15",
        # bool checkbox present → True; absent ones become False
        "oidc.enabled": "on",
    })
    loaded = runtime.load_settings(db)
    assert loaded["smtp.host"] == "smtp.acme.test"
    assert loaded["smtp.port"] == 2525          # coerced to int
    assert loaded["alerts.low_supply_pct"] == 15.0
    assert loaded["oidc.enabled"] is True
    assert loaded["oidc.auto_provision"] is False  # bool absent from form → False


def test_secret_placeholder_keeps_existing(db):
    runtime.save_settings(db, {"smtp.password": "s3cret"})
    assert runtime.load_settings(db)["smtp.password"] == "s3cret"
    # Re-save with the placeholder → unchanged.
    runtime.save_settings(db, {"smtp.password": runtime.SECRET_PLACEHOLDER})
    assert runtime.load_settings(db)["smtp.password"] == "s3cret"


def test_masked_for_form_hides_secrets(db):
    runtime.save_settings(db, {"freescout.api_key": "abc123"})
    masked = runtime.masked_for_form(runtime.load_settings(db))
    assert masked["freescout.api_key"] == runtime.SECRET_PLACEHOLDER


def test_offline_grace_read_from_settings(db):
    from datetime import datetime, timedelta, timezone

    from central import models as m

    # Agent last seen 10 min ago; with a 5-min grace it should flip offline.
    runtime.save_settings(db, {"alerts.offline_grace_seconds": "300"})
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    db.add(m.Agent(
        site_id=site.id, name="a", api_key_hash="x", status=m.AgentStatus.online,
        last_heartbeat=datetime.now(timezone.utc) - timedelta(minutes=10),
    ))
    db.commit()
    assert jobs.mark_offline_agents(db)["agents_updated"] == 1
