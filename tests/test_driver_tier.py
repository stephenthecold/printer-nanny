"""Driver tier: persistence, the operator override, and probe throttling.

The tier decides whether a workstation queue needs a driver installed. Two rules
carry the weight here and both fail silently if broken:

1. A reading WITHOUT driver fields must not disturb the stored tier. Probing is
   throttled and older agents never send them, so treating absent as "unknown"
   would blank a printer that probed cleanly last week on the next routine SNMP
   poll -- and nothing would look wrong until a rollout used the empty value.
2. A re-probe must never overwrite the operator's override. That is the entire
   reason the two live in separate columns.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central import schemas as s
from central import services
from central.main import app
from central.security import hash_password
from printer_nanny_agent import driver_probe


# --- fixtures ---------------------------------------------------------------


def _printer(db) -> m.Printer:
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip="10.0.0.5",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.commit()
    return printer


@pytest.fixture()
def http(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("admin"), role=m.UserRole.admin))
    db.commit()
    client = TestClient(app)
    resp = client.post(
        "/login", data={"username": "admin", "password": "admin"}, follow_redirects=False
    )
    assert resp.status_code == 303
    return client


def _reading(**kw) -> s.ReadingIn:
    return s.ReadingIn(ip="10.0.0.5", **kw)


# --- persistence ------------------------------------------------------------


def test_probe_result_is_persisted(db):
    printer = _printer(db)
    services.apply_reading(
        db,
        {printer.site_id},
        _reading(
            driver_tier=m.DriverTier.driverless,
            driver_tier_reason="IPP 2.0 with image/pwg-raster",
            ipp_endpoint="ipp://10.0.0.5:631/ipp/print",
            ipp_capabilities={"document_formats": ["image/pwg-raster"]},
        ),
    )
    db.commit()
    db.refresh(printer)
    assert printer.driver_tier == m.DriverTier.driverless
    assert "pwg-raster" in printer.driver_tier_reason
    assert printer.ipp_endpoint.endswith("/ipp/print")
    assert printer.ipp_capabilities["document_formats"] == ["image/pwg-raster"]
    assert printer.driver_probed_at is not None


def test_reading_without_driver_fields_does_not_blank_the_tier(db):
    """The rule that matters: absent means 'no new information', not 'unknown'.

    Probing is throttled to roughly daily while SNMP polls run every few
    minutes, so the overwhelming majority of readings carry no driver fields. If
    those cleared the column, a printer would be probed, look correct for one
    cycle, and silently revert.
    """
    printer = _printer(db)
    services.apply_reading(
        db, {printer.site_id},
        _reading(driver_tier=m.DriverTier.driverless, driver_tier_reason="probed"),
    )
    db.commit()

    # A routine poll -- no driver fields at all.
    services.apply_reading(db, {printer.site_id}, _reading(page_count=42))
    db.commit()
    db.refresh(printer)

    assert printer.driver_tier == m.DriverTier.driverless
    assert printer.driver_tier_reason == "probed"
    assert printer.page_count == 42, "the rest of the reading must still apply"


def test_reprobe_updates_observation_but_never_the_override(db):
    printer = _printer(db)
    printer.driver_tier = m.DriverTier.driverless
    printer.driver_tier_override = m.DriverTier.driver_required
    db.commit()

    services.apply_reading(
        db, {printer.site_id},
        _reading(driver_tier=m.DriverTier.ipp_disabled, driver_tier_reason="port refused"),
    )
    db.commit()
    db.refresh(printer)

    assert printer.driver_tier == m.DriverTier.ipp_disabled, "observation refreshes"
    assert printer.driver_tier_override == m.DriverTier.driver_required, "decision survives"
    assert printer.effective_driver_tier == m.DriverTier.driver_required


def test_unreachable_probe_keeps_the_last_known_endpoint(db):
    """An endpoint that stopped answering is what you need to diagnose it."""
    printer = _printer(db)
    services.apply_reading(
        db, {printer.site_id},
        _reading(driver_tier=m.DriverTier.driverless, ipp_endpoint="ipp://10.0.0.5:631/ipp/print"),
    )
    db.commit()
    services.apply_reading(
        db, {printer.site_id},
        _reading(driver_tier=m.DriverTier.unreachable, driver_tier_reason="timed out"),
    )
    db.commit()
    db.refresh(printer)
    assert printer.driver_tier == m.DriverTier.unreachable
    assert printer.ipp_endpoint == "ipp://10.0.0.5:631/ipp/print"


def test_effective_tier_and_overridden_flag(db):
    printer = _printer(db)
    assert printer.effective_driver_tier is None
    printer.driver_tier = m.DriverTier.driver_required
    assert printer.effective_driver_tier == m.DriverTier.driver_required
    assert not printer.driver_tier_is_overridden

    printer.driver_tier_override = m.DriverTier.driver_required
    assert not printer.driver_tier_is_overridden, "agreeing with the probe is not an override"

    printer.driver_tier_override = m.DriverTier.driverless
    assert printer.driver_tier_is_overridden
    assert printer.effective_driver_tier == m.DriverTier.driverless


# --- the override route -----------------------------------------------------


def test_operator_can_pin_and_clear_the_tier(http, db):
    printer = _printer(db)
    printer.driver_tier = m.DriverTier.driver_required
    db.commit()

    http.post(
        f"/manage/printers/{printer.id}/driver-tier",
        data={"driver_tier_override": "driverless"},
        follow_redirects=False,
    )
    db.refresh(printer)
    assert printer.driver_tier_override == m.DriverTier.driverless

    http.post(
        f"/manage/printers/{printer.id}/driver-tier",
        data={"driver_tier_override": ""},
        follow_redirects=False,
    )
    db.refresh(printer)
    assert printer.driver_tier_override is None


@pytest.mark.parametrize("tier", ["ipp_disabled", "unreachable", "error"])
def test_failure_states_cannot_be_pinned(http, db, tier):
    """These describe a failure to reach the device, not an opinion to record.

    Accepting them would let an operator pin a diagnosis the workstation client
    cannot act on -- there is no queue you can build from "unreachable".
    """
    printer = _printer(db)
    http.post(
        f"/manage/printers/{printer.id}/driver-tier",
        data={"driver_tier_override": tier},
        follow_redirects=False,
    )
    db.refresh(printer)
    assert printer.driver_tier_override is None


def test_garbage_tier_is_rejected_without_500(http, db):
    printer = _printer(db)
    resp = http.post(
        f"/manage/printers/{printer.id}/driver-tier",
        data={"driver_tier_override": "'; DROP TABLE printers;--"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db.refresh(printer)
    assert printer.driver_tier_override is None


def test_override_is_audited_with_what_we_observed(http, db):
    printer = _printer(db)
    printer.driver_tier = m.DriverTier.driver_required
    db.commit()
    http.post(
        f"/manage/printers/{printer.id}/driver-tier",
        data={"driver_tier_override": "driverless"},
        follow_redirects=False,
    )
    from sqlalchemy import select

    row = db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "printer.driver_tier_override")
    ).first()
    assert row is not None
    # Both sides recorded: "who decided this, and what were we detecting then".
    assert "set=driverless" in row.detail
    assert "observed=driver_required" in row.detail


def test_override_requires_a_manager(db):
    printer = _printer(db)
    anon = TestClient(app)
    resp = anon.post(
        f"/manage/printers/{printer.id}/driver-tier",
        data={"driver_tier_override": "driverless"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/login")
    db.refresh(printer)
    assert printer.driver_tier_override is None


# --- agent-side throttling --------------------------------------------------


def test_probe_cache_respects_the_interval():
    cache = driver_probe.DriverProbeCache(interval_s=3600)
    assert cache.due("10.0.0.5", now=0.0)
    cache.mark("10.0.0.5", now=0.0)
    assert not cache.due("10.0.0.5", now=1000.0)
    assert cache.due("10.0.0.5", now=3600.0)
    assert cache.due("10.0.0.9", now=1000.0), "other devices are unaffected"


def test_probe_can_be_disabled_with_a_non_positive_interval():
    assert not driver_probe.DriverProbeCache(interval_s=0).due("10.0.0.5", now=0.0)


def test_attach_merges_when_due_and_skips_when_not(monkeypatch):
    calls = []

    def fake_probe(ip, timeout=5.0):
        calls.append(ip)
        return ipp_result()

    def ipp_result():
        from printer_nanny_agent import ipp

        return ipp.IppProbe(
            ipp.STATUS_DRIVERLESS, "IPP 2.0", {"ipp-versions-supported": ["2.0"]},
            "ipp://10.0.0.5:631/ipp/print",
        )

    monkeypatch.setattr(driver_probe.ipp, "probe", fake_probe)
    cache = driver_probe.DriverProbeCache(interval_s=3600)

    first = driver_probe.attach({"ip": "10.0.0.5"}, cache, "10.0.0.5")
    assert first["driver_tier"] == "driverless"
    assert first["ipp_endpoint"].endswith("/ipp/print")

    second = driver_probe.attach({"ip": "10.0.0.5"}, cache, "10.0.0.5")
    assert "driver_tier" not in second, "throttled: no new probe, no fields"
    assert len(calls) == 1


def test_attach_never_lets_a_probe_failure_cost_the_reading(monkeypatch):
    """The SNMP poll is the agent's primary job; a probe rides along."""

    def boom(ip, timeout=5.0):
        raise RuntimeError("device did something exotic")

    monkeypatch.setattr(driver_probe.ipp, "probe", boom)
    reading = driver_probe.attach(
        {"ip": "10.0.0.5", "page_count": 99}, driver_probe.DriverProbeCache(), "10.0.0.5"
    )
    assert reading["page_count"] == 99
    assert "driver_tier" not in reading


def test_a_refusing_device_is_not_reprobed_every_poll(monkeypatch):
    """'It refused' is a durable answer -- caching it is the point of the class."""
    calls = []

    def refusing(ip, timeout=5.0):
        from printer_nanny_agent import ipp

        calls.append(ip)
        return ipp.IppProbe(ipp.STATUS_IPP_DISABLED, "port 631 refused")

    monkeypatch.setattr(driver_probe.ipp, "probe", refusing)
    cache = driver_probe.DriverProbeCache(interval_s=3600)
    for _ in range(5):
        driver_probe.attach({"ip": "10.0.0.9"}, cache, "10.0.0.9")
    assert len(calls) == 1
