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
        # bool checkbox present → True
        "oidc.enabled": "on",
    })
    loaded = runtime.load_settings(db)
    assert loaded["smtp.host"] == "smtp.acme.test"
    assert loaded["smtp.port"] == 2525          # coerced to int
    assert loaded["alerts.low_supply_pct"] == 15.0
    assert loaded["oidc.enabled"] is True
    # No `sections` scope → this is a partial update, so a bool that isn't in
    # the payload was never submitted and keeps its value (here, its default).
    assert loaded["oidc.auto_provision"] is True


# --------------------------------------------------------------------------- #
# Bool semantics: `sections` is what makes an absent checkbox mean "unchecked"
# --------------------------------------------------------------------------- #
def test_unscoped_save_leaves_absent_bools_alone(db):
    """A partial write must not reset every other bool on the system.

    Regression for the settings-clobber bug: callers that post a couple of keys
    with no `sections` scope (logo upload, OAuth token refresh, seed, tooling)
    used to switch off STARTTLS, every channel, SSO and SCIM as a side effect.
    """
    runtime.save_settings(db, {
        "smtp.use_tls": "on", "email.enabled": "on", "slack.enabled": "on",
        "oidc.enabled": "on", "scim.enabled": "on",
    })
    runtime.save_settings(db, {"app.logo_url": "/branding/logo"})
    loaded = runtime.load_settings(db)
    assert loaded["app.logo_url"] == "/branding/logo"   # the write took
    for key in ("smtp.use_tls", "email.enabled", "slack.enabled",
                "oidc.enabled", "scim.enabled"):
        assert loaded[key] is True, key                 # nothing else moved


def test_scoped_save_unchecks_absent_bools_in_scope(db):
    """The other half of the contract: with `sections` given the payload IS the
    rendered form, so an absent checkbox genuinely means the operator cleared
    it. Locks the fix out of making checkboxes un-uncheckable."""
    runtime.save_settings(db, {"email.enabled": "on", "slack.enabled": "on"})
    runtime.save_settings(
        db, {"smtp.host": "mail.example.com"},   # email.enabled checkbox absent
        sections={"Email (SMTP)"},
    )
    loaded = runtime.load_settings(db)
    assert loaded["email.enabled"] is False          # unchecked, in scope
    assert loaded["slack.enabled"] is True           # different section, untouched


def test_set_scim_token_does_not_disable_scim(db):
    """Rotating the SCIM bearer token must not deprovision the IdP integration."""
    runtime.save_settings(db, {"scim.enabled": "on"})
    runtime.set_scim_token(db, "brand-new-idp-token")
    loaded = runtime.load_settings(db)
    assert loaded["scim.enabled"] is True
    assert runtime.scim_token_matches(db, "brand-new-idp-token") is True


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
