"""Component-life predictive maintenance: a MaintenanceSchedule with a
component_type + life_threshold opens a maintenance_due alert when the matching
component-life Supply row (as written by the Brother provider) drops to/below
the threshold, dedupes, and auto-resolves once the part is serviced.
"""

from __future__ import annotations

from central import models as m
from central.worker import jobs


def _approved_printer(db, ip="10.0.0.9", model="Brother HL-L8900CDW"):
    client = m.Client(name=f"Acme {ip}")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip=ip,
        model=model,
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    return printer


def _fuser_supply(db, printer, level_pct):
    # Mirrors how the Brother maintenance blob writes the fuser life row
    # (brother_maintenance._EXTRA_PART_ROWS["fuser"] -> type=fuser, color=None).
    supply = m.Supply(
        printer_id=printer.id,
        type=m.SupplyType.fuser,
        color=None,
        description="Fuser Unit",
        level_pct=level_pct,
    )
    db.add(supply)
    db.flush()
    return supply


def _open_component_alerts(db):
    return db.query(m.Alert).filter_by(
        type=m.AlertConditionType.maintenance_due, state=m.AlertState.open
    ).all()


def test_component_opens_at_or_under_threshold(db):
    printer = _approved_printer(db)
    _fuser_supply(db, printer, level_pct=8)
    db.add(
        m.MaintenanceSchedule(
            printer_id=printer.id, name="Fuser kit",
            component_type="fuser", life_threshold=10,
        )
    )
    db.commit()

    res = jobs.check_maintenance_due(db)
    assert res["maintenance_opened"] == 1
    opened = _open_component_alerts(db)
    assert len(opened) == 1
    assert opened[0].printer_id == printer.id
    assert "8%" in opened[0].detail


def test_component_at_exact_threshold_opens(db):
    printer = _approved_printer(db)
    _fuser_supply(db, printer, level_pct=10)  # exactly at threshold
    db.add(
        m.MaintenanceSchedule(
            printer_id=printer.id, name="Fuser kit",
            component_type="fuser", life_threshold=10,
        )
    )
    db.commit()

    assert jobs.check_maintenance_due(db)["maintenance_opened"] == 1


def test_component_above_threshold_does_not_fire(db):
    printer = _approved_printer(db)
    _fuser_supply(db, printer, level_pct=42)
    db.add(
        m.MaintenanceSchedule(
            printer_id=printer.id, name="Fuser kit",
            component_type="fuser", life_threshold=10,
        )
    )
    db.commit()

    res = jobs.check_maintenance_due(db)
    assert res["maintenance_opened"] == 0
    assert _open_component_alerts(db) == []


def test_component_dedupes(db):
    printer = _approved_printer(db)
    _fuser_supply(db, printer, level_pct=5)
    db.add(
        m.MaintenanceSchedule(
            printer_id=printer.id, name="Fuser kit",
            component_type="fuser", life_threshold=10,
        )
    )
    db.commit()

    assert jobs.check_maintenance_due(db)["maintenance_opened"] == 1
    # Second pass: still low, but no second alert opened.
    res2 = jobs.check_maintenance_due(db)
    assert res2["maintenance_opened"] == 0
    assert len(_open_component_alerts(db)) == 1


def test_component_auto_resolves_when_serviced(db):
    printer = _approved_printer(db)
    supply = _fuser_supply(db, printer, level_pct=5)
    db.add(
        m.MaintenanceSchedule(
            printer_id=printer.id, name="Fuser kit",
            component_type="fuser", life_threshold=10,
        )
    )
    db.commit()

    assert jobs.check_maintenance_due(db)["maintenance_opened"] == 1
    assert len(_open_component_alerts(db)) == 1

    # New fuser installed -> life climbs back above threshold -> alert resolves.
    supply.level_pct = 100
    db.commit()
    res = jobs.check_maintenance_due(db)
    assert res["maintenance_resolved"] == 1
    assert _open_component_alerts(db) == []


def test_component_model_wide_matches_by_model(db):
    p1 = _approved_printer(db, ip="10.0.0.10", model="Brother HL-L8900CDW")
    p2 = _approved_printer(db, ip="10.0.0.11", model="HP LaserJet M404")
    _fuser_supply(db, p1, level_pct=4)
    _fuser_supply(db, p2, level_pct=4)
    db.add(
        m.MaintenanceSchedule(
            name="Brother fuser", model="Brother",
            component_type="fuser", life_threshold=10,
        )
    )
    db.commit()

    res = jobs.check_maintenance_due(db)
    # Only the Brother printer matches the model substring.
    assert res["maintenance_opened"] == 1
    opened = _open_component_alerts(db)
    assert {a.printer_id for a in opened} == {p1.id}
