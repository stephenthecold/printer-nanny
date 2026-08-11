"""Location stock and replacement-to-inventory reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import supply_inventory as inventory
from central.main import app
from central.security import hash_password
from central.worker import jobs

NOW = datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc)


def _fleet(db):
    client = m.Client(name="LCR")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="Downtown")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip="10.0.0.20",
        display_name="Reception",
        brand="Brother",
        model="Brother MFC-L8900CDW",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    return client, site, printer


def _order(db, site, *, sku="TN-439BK", quantity=1, delivered_offset=0):
    order = m.SupplyOrder(
        site_id=site.id,
        status=m.SupplyOrderStatus.delivered,
        model="Brother MFC-L8900CDW",
        supply_type="toner",
        color="black",
        description="Black toner",
        manufacturer="Brother",
        sku=sku,
        quantity=quantity,
        delivered_at=NOW + timedelta(minutes=delivered_offset),
    )
    db.add(order)
    db.flush()
    return order


def _cycle(db, printer, *, offset=0):
    ended = NOW + timedelta(minutes=offset)
    cycle = m.SupplyCycle(
        printer_id=printer.id,
        supply_type="toner",
        color="black",
        started_at=ended - timedelta(days=30),
        last_ts=ended,
        ended_at=ended,
        start_level_pct=100,
        end_level_pct=5,
        min_level_pct=5,
        pages=5000,
        complete=True,
    )
    db.add(cycle)
    db.flush()
    return cycle


def _login(db, *, role=m.UserRole.admin, username="admin", client_id=None):
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


def test_one_exact_product_is_deducted_fifo_and_only_once(db):
    _, site, printer = _fleet(db)
    first = _order(db, site, quantity=1)
    second = _order(db, site, quantity=2, delivered_offset=1)
    cycles = [_cycle(db, printer), _cycle(db, printer, offset=2)]

    result = inventory.reconcile_replacements(db, printer, cycles, now=NOW)
    db.commit()

    assert result == {"auto_assigned": 2, "ambiguous": 0, "no_stock": 0}
    usages = list(db.scalars(select(m.SupplyUsage).order_by(m.SupplyUsage.id)))
    assert [usage.supply_order_id for usage in usages] == [first.id, second.id]
    stock = inventory.stock_groups(db)
    assert len(stock) == 1
    assert (stock[0].received, stock[0].used, stock[0].on_hand) == (3, 2, 1)

    assert inventory.reconcile_replacements(db, printer, cycles, now=NOW) == {
        "auto_assigned": 0,
        "ambiguous": 0,
        "no_stock": 0,
    }
    db.commit()
    assert len(list(db.scalars(select(m.SupplyUsage)))) == 2


def test_multiple_distinct_products_wait_for_a_technician(db):
    _, site, printer = _fleet(db)
    _order(db, site, sku="TN-439BK")
    _order(db, site, sku="ALT-439BK", delivered_offset=1)
    cycle = _cycle(db, printer)

    result = inventory.reconcile_replacements(db, printer, [cycle], now=NOW)
    db.commit()

    usage = db.scalar(select(m.SupplyUsage))
    assert result["ambiguous"] == 1
    assert usage.status == m.SupplyUsageStatus.ambiguous
    assert usage.supply_order_id is None
    assert len(inventory.assignment_options(db, [usage])[usage.id]) == 2


def test_no_compatible_location_stock_is_visible_as_unmatched(db):
    _, _, printer = _fleet(db)
    cycle = _cycle(db, printer)

    inventory.reconcile_replacements(db, printer, [cycle], now=NOW)
    db.commit()

    usage = db.scalar(select(m.SupplyUsage))
    assert usage.status == m.SupplyUsageStatus.no_stock
    assert usage in inventory.unresolved_usages(db)


def test_worker_detects_refill_and_deducts_delivered_stock(db):
    _, site, printer = _fleet(db)
    order = _order(db, site)
    db.add_all(
        [
            m.Reading(
                printer_id=printer.id,
                ts=NOW - timedelta(days=1),
                page_count=5000,
                supply_snapshot=[
                    {"type": "toner", "color": "black", "level_pct": 4}
                ],
            ),
            m.Reading(
                printer_id=printer.id,
                ts=NOW - timedelta(minutes=1),
                page_count=5010,
                supply_snapshot=[
                    {"type": "toner", "color": "black", "level_pct": 100}
                ],
            ),
        ]
    )
    db.commit()

    result = jobs.scan_supply_cycles(db, now=NOW)["supply_cycles"]

    usage = db.scalar(select(m.SupplyUsage))
    assert result["cartridges_replaced"] == 1
    assert result["inventory"]["auto_assigned"] == 1
    assert usage.supply_order_id == order.id
    assert inventory.stock_groups(db)[0].on_hand == 0


def test_staff_can_assign_or_delete_an_unresolved_detection(db):
    client, site, printer = _fleet(db)
    order = _order(db, site)
    cycle = _cycle(db, printer)
    usage = m.SupplyUsage(
        supply_cycle_id=cycle.id,
        site_id=site.id,
        printer_id=printer.id,
        status=m.SupplyUsageStatus.no_stock,
        model=printer.model,
        supply_type="toner",
        color="black",
        detected_at=NOW,
    )
    db.add(usage)
    db.commit()
    usage_id = usage.id
    http = _login(db)

    response = http.post(
        f"/supplies/usages/{usage_id}/assign",
        data={"order_id": order.id},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db.expire_all()
    assigned = db.get(m.SupplyUsage, usage_id)
    assert assigned.status == m.SupplyUsageStatus.manually_assigned
    assert assigned.supply_order_id == order.id
    assert "supply_usage.assign" in {
        row.action for row in db.scalars(select(m.AuditLog))
    }
    order_id = order.id
    http.post(f"/supplies/orders/{order_id}/delete", follow_redirects=False)
    db.expire_all()
    assert db.get(m.SupplyOrder, order_id) is not None

    another = _cycle(db, printer, offset=2)
    discard = m.SupplyUsage(
        supply_cycle_id=another.id,
        site_id=site.id,
        printer_id=printer.id,
        status=m.SupplyUsageStatus.no_stock,
        model=printer.model,
        supply_type="toner",
        color="black",
        detected_at=NOW,
    )
    db.add(discard)
    db.commit()
    discard_id = discard.id
    http.post(f"/supplies/usages/{discard_id}/delete", follow_redirects=False)
    db.expire_all()
    assert db.get(m.SupplyUsage, discard_id) is None
    assert client.id is not None


def test_readonly_cannot_change_inventory_and_hostile_values_are_escaped(db):
    client, site, printer = _fleet(db)
    order = _order(db, site)
    cycle = _cycle(db, printer)
    usage = m.SupplyUsage(
        supply_cycle_id=cycle.id,
        site_id=site.id,
        printer_id=printer.id,
        status=m.SupplyUsageStatus.no_stock,
        model="<script>alert(1)</script>",
        supply_type="toner",
        color="black",
        detected_at=NOW,
    )
    db.add(usage)
    db.commit()
    usage_id = usage.id

    viewer = _login(
        db,
        role=m.UserRole.client_readonly,
        username="viewer",
        client_id=client.id,
    )
    viewer.post(
        f"/supplies/usages/{usage_id}/assign",
        data={"order_id": order.id},
        follow_redirects=False,
    )
    viewer.post(f"/supplies/usages/{usage_id}/delete", follow_redirects=False)
    db.expire_all()
    assert db.get(m.SupplyUsage, usage_id).supply_order_id is None

    body = _login(db, username="second-admin").get("/supplies/reorder").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
