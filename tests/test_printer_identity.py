"""Device identity: serial first, IP only as a fallback.

IP is a location, not an identity. These tests pin both halves of the DHCP
problem -- a printer that moves must stay one row, and a different printer that
inherits a recycled address must not adopt the first one's history.
"""

from __future__ import annotations

from central import models as m
from central import services
from central import schemas as s


def _site(db, name="HQ"):
    client = m.Client(name=f"Acme-{name}")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name=name)
    db.add(site)
    db.flush()
    return site


def _printer(db, site, ip, serial=None, **kw):
    printer = m.Printer(
        client_id=site.client_id,
        site_id=site.id,
        ip=ip,
        serial=serial,
        discovery_state=kw.pop("discovery_state", m.DiscoveryState.approved),
        **kw,
    )
    db.add(printer)
    db.flush()
    return printer


def test_serial_wins_over_ip(db):
    """The device moved to a new lease -- resolve it by serial, not address."""
    site = _site(db)
    moved = _printer(db, site, ip="10.0.0.5", serial="SN-AAA")
    db.commit()

    found = services.find_printer_in_sites(db, {site.id}, ip="10.0.0.99", serial="SN-AAA")

    assert found is not None and found.id == moved.id


def test_recycled_ip_with_a_different_serial_does_not_match(db):
    """New hardware on an old address must not inherit the old row."""
    site = _site(db)
    _printer(db, site, ip="10.0.0.5", serial="SN-OLD")
    db.commit()

    found = services.find_printer_in_sites(db, {site.id}, ip="10.0.0.5", serial="SN-NEW")

    assert found is None


def test_ip_fallback_when_no_serial_is_known(db):
    """Existing fleets with no serial recorded keep resolving by IP."""
    site = _site(db)
    legacy = _printer(db, site, ip="10.0.0.5", serial=None)
    db.commit()

    # Device reports a serial; the row has none yet -> still matches, and the
    # caller backfills the serial.
    found = services.find_printer_in_sites(db, {site.id}, ip="10.0.0.5", serial="SN-AAA")
    assert found is not None and found.id == legacy.id

    # Device reports no serial either -> plain IP match.
    assert services.find_printer_in_sites(db, {site.id}, ip="10.0.0.5") is not None


def test_lookup_is_scoped_to_the_agents_sites(db):
    """A serial in another tenant's site is not ours to match."""
    ours, theirs = _site(db, "HQ"), _site(db, "Other")
    _printer(db, theirs, ip="10.0.0.5", serial="SN-AAA")
    db.commit()

    assert services.find_printer_in_sites(db, {ours.id}, ip="10.0.0.5", serial="SN-AAA") is None


def test_discovery_follows_a_moved_printer_instead_of_duplicating(db):
    site = _site(db)
    agent = m.Agent(site_id=site.id, name="a", api_key_hash="x")
    db.add(agent)
    original = _printer(db, site, ip="10.0.0.5", serial="SN-AAA")
    db.commit()

    printer, created = services.record_discovered(
        db, agent, s.DiscoveredIn(ip="10.0.0.77", serial="SN-AAA")
    )
    db.commit()

    assert created is False
    assert printer.id == original.id
    assert printer.ip == "10.0.0.77"          # followed the lease
    assert db.query(m.Printer).count() == 1   # and did not duplicate


def test_discovery_creates_a_new_row_for_new_hardware_on_a_recycled_ip(db):
    site = _site(db)
    agent = m.Agent(site_id=site.id, name="a", api_key_hash="x")
    db.add(agent)
    old = _printer(db, site, ip="10.0.0.5", serial="SN-OLD", page_count=50_000)
    db.commit()

    printer, created = services.record_discovered(
        db, agent, s.DiscoveredIn(ip="10.0.0.5", serial="SN-NEW")
    )
    db.commit()

    assert created is True
    assert printer.id != old.id
    assert printer.page_count != 50_000  # did not inherit the old meter
    assert db.query(m.Printer).count() == 2


def test_moved_printer_keeps_both_rows_when_the_address_is_taken(db):
    """Two rows for one device is an operator merge, not a guess we make."""
    site = _site(db)
    agent = m.Agent(site_id=site.id, name="a", api_key_hash="x")
    db.add(agent)
    moved = _printer(db, site, ip="10.0.0.5", serial="SN-AAA")
    _printer(db, site, ip="10.0.0.6", serial="SN-BBB")
    db.commit()

    printer, created = services.record_discovered(
        db, agent, s.DiscoveredIn(ip="10.0.0.6", serial="SN-AAA")
    )
    db.commit()

    assert created is False
    assert printer.id == moved.id
    assert printer.ip == "10.0.0.5"  # left alone rather than colliding
    assert db.query(m.Printer).count() == 2
