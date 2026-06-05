"""Model constraints and the shared ingest service path."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from central import models as m
from central import schemas as s
from central import services


def _client_site(db):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    return client, site


def test_unique_printer_per_site_ip(db):
    _, site = _client_site(db)
    db.add(m.Printer(client_id=site.client_id, site_id=site.id, ip="10.0.0.5"))
    db.commit()
    db.add(m.Printer(client_id=site.client_id, site_id=site.id, ip="10.0.0.5"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_apply_reading_only_for_approved_printer(db):
    client, site = _client_site(db)
    pending = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip="10.0.0.9",
        discovery_state=m.DiscoveryState.pending,
    )
    db.add(pending)
    db.commit()

    reading = s.ReadingIn(ip="10.0.0.9", status=m.PrinterStatus.ok, page_count=10)
    assert services.apply_reading(db, site.id, reading) is None  # pending → skipped

    pending.discovery_state = m.DiscoveryState.approved
    db.commit()
    out = services.apply_reading(db, site.id, reading)
    db.commit()
    assert out is not None
    assert out.page_count == 10
    assert db.query(m.Reading).count() == 1


def test_apply_reading_upserts_supplies(db):
    client, site = _client_site(db)
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip="10.0.0.10",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.commit()

    r1 = s.ReadingIn(
        ip="10.0.0.10",
        supplies=[s.SupplyIn(type=m.SupplyType.toner, color="black", level_pct=80)],
    )
    services.apply_reading(db, site.id, r1)
    db.commit()
    r2 = s.ReadingIn(
        ip="10.0.0.10",
        supplies=[s.SupplyIn(type=m.SupplyType.toner, color="black", level_pct=60)],
    )
    services.apply_reading(db, site.id, r2)
    db.commit()

    supplies = db.query(m.Supply).filter_by(printer_id=printer.id).all()
    assert len(supplies) == 1  # upserted, not duplicated
    assert supplies[0].level_pct == 60


def test_record_discovered_is_idempotent(db):
    client, site = _client_site(db)
    agent = m.Agent(site_id=site.id, name="a", api_key_hash="x")
    db.add(agent)
    db.commit()

    dev = s.DiscoveredIn(ip="10.0.0.50", hostname="hp-1")
    _, created1 = services.record_discovered(db, agent, dev)
    db.commit()
    _, created2 = services.record_discovered(db, agent, dev)
    db.commit()
    assert created1 is True
    assert created2 is False
    assert db.query(m.Printer).filter_by(ip="10.0.0.50").count() == 1
