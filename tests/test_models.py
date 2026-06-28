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


def test_apply_reading_prunes_orphaned_duplicate_supply(db):
    """A stale (type, color) row superseded by a same-type row this cycle is dropped.

    Reproduces the dashboard bug where a colorless generic 'Black Toner Cartridge'
    (color=None, level not reported) lingered next to the real colored one after an
    agent/parser version started reporting the cartridge with a real color.
    """
    client, site = _client_site(db)
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.11",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    # Orphan rows left by an older parser: black toner + drum reported colorless,
    # with no level ("not reported").
    db.add_all([
        m.Supply(printer_id=printer.id, type=m.SupplyType.toner, color=None,
                 description="Black Toner Cartridge", level_pct=None),
        m.Supply(printer_id=printer.id, type=m.SupplyType.drum, color=None,
                 description="Drum Unit", level_pct=None),
    ])
    db.commit()

    # The current agent reports the same cartridges with real colors + levels.
    services.apply_reading(db, site.id, s.ReadingIn(
        ip="10.0.0.11",
        supplies=[
            s.SupplyIn(type=m.SupplyType.toner, color="black",
                       description="Black Toner Cartridge", level_pct=27),
            s.SupplyIn(type=m.SupplyType.drum, color="black",
                       description="Drum Unit", level_pct=5),
        ],
    ))
    db.commit()

    supplies = db.query(m.Supply).filter_by(printer_id=printer.id).all()
    # The two colorless orphans are gone; only the real colored rows remain.
    assert len(supplies) == 2
    assert {(x.type, x.color) for x in supplies} == {
        (m.SupplyType.toner, "black"), (m.SupplyType.drum, "black")
    }
    assert all(x.level_pct is not None for x in supplies)


def test_apply_reading_keeps_intermittent_unique_supply(db):
    """A unique-type supply missing from one poll is NOT pruned (no flapping).

    Only same-type duplicates are dropped; a fuser absent from a degraded poll
    that reported only toner has no same-type sibling, so it must survive.
    """
    client, site = _client_site(db)
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.12",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    db.add(m.Supply(printer_id=printer.id, type=m.SupplyType.fuser, color=None,
                    description="Fuser Unit", level_pct=90))
    db.commit()

    # This poll reports only toner (e.g. the maintenance blob fetch failed).
    services.apply_reading(db, site.id, s.ReadingIn(
        ip="10.0.0.12",
        supplies=[s.SupplyIn(type=m.SupplyType.toner, color="black", level_pct=50)],
    ))
    db.commit()

    types = {x.type for x in db.query(m.Supply).filter_by(printer_id=printer.id)}
    assert m.SupplyType.fuser in types  # untouched, not flapped away
    assert m.SupplyType.toner in types


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
