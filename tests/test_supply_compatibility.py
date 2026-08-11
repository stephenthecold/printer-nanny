"""Manual compatibility catalogue, matching, UI authorization, and auditing."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import supply_compatibility as compatibility
from central import supply_inventory as inventory
from central import supply_orders
from central.main import app
from central.security import hash_password


def _login(db, username="admin", role=m.UserRole.admin, client_id=None):
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


def _fleet(db):
    client = m.Client(name="LCR")
    db.add(client)
    db.flush()
    hq = m.Site(client_id=client.id, name="HQ")
    branch = m.Site(client_id=client.id, name="Branch")
    db.add_all([hq, branch])
    db.flush()
    printers = []
    for site, model, ip in (
        (hq, "HP LaserJet Enterprise M611dn", "10.0.0.10"),
        (hq, "HP LaserJet Enterprise M612dn", "10.0.0.11"),
        (hq, "HP LaserJet Pro M404dn", "10.0.0.12"),
        (branch, "HP LaserJet Enterprise M611dn", "10.1.0.10"),
    ):
        printer = m.Printer(
            client_id=client.id,
            site_id=site.id,
            ip=ip,
            model=model,
            display_name=model,
            discovery_state=m.DiscoveryState.approved,
        )
        db.add(printer)
        db.flush()
        db.add(
            m.Supply(
                printer_id=printer.id,
                type=m.SupplyType.toner,
                color="black",
                description="Black toner",
                level_pct=5,
            )
        )
        printers.append(printer)
    db.commit()
    return client, hq, printers


def _product(db, sku="W1470A", *, oem=True):
    product = m.SupplyProduct()
    compatibility.set_product_fields(
        product,
        manufacturer="HP",
        sku=sku,
        description="147A black toner",
        supply_type="toner",
        color="black",
        is_oem=oem,
        notes="Manufacturer table",
        model_tags="LaserJet Enterprise M611\nLaserJet Enterprise M612",
    )
    db.add(product)
    db.flush()
    return product


def test_catalogue_expands_one_sku_across_models_but_not_locations(db):
    _, hq, printers = _fleet(db)
    _product(db)
    order = m.SupplyOrder(
        site_id=hq.id,
        model=printers[0].model,
        supply_type="toner",
        color="black",
        manufacturer="HP",
        sku="w1470a",
        quantity=1,
    )
    db.add(order)
    db.commit()

    matches = supply_orders.compatible_printers(db, [order])[order.id]
    assert {printer.id for printer in matches} == {printers[0].id, printers[1].id}
    assert printers[2].id not in {printer.id for printer in matches}
    assert printers[3].id not in {printer.id for printer in matches}


def test_nonmatching_recorded_manufacturer_does_not_borrow_a_unique_sku(db):
    _, hq, printers = _fleet(db)
    _product(db)
    order = m.SupplyOrder(
        site_id=hq.id,
        model=printers[2].model,
        supply_type="toner",
        color="black",
        manufacturer="Not HP",
        sku="W1470A",
        quantity=1,
    )
    db.add(order)
    db.commit()
    assert compatibility.product_for_order(db, order) is None
    matches = supply_orders.compatible_printers(db, [order])[order.id]
    assert {printer.id for printer in matches} == {printers[2].id}


def test_catalogued_stock_can_be_deducted_by_another_supported_model(db):
    _, hq, printers = _fleet(db)
    _product(db)
    order = m.SupplyOrder(
        site_id=hq.id,
        status=m.SupplyOrderStatus.delivered,
        model=printers[0].model,
        supply_type="toner",
        color="black",
        manufacturer="HP",
        sku="W1470A",
        quantity=1,
        delivered_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    cycle = m.SupplyCycle(
        printer_id=printers[1].id,
        supply_type="toner",
        color="black",
        started_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        last_ts=datetime(2026, 8, 11, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        start_level_pct=100,
        end_level_pct=5,
        min_level_pct=5,
        complete=True,
    )
    db.add_all([order, cycle])
    db.flush()

    result = inventory.reconcile_replacements(db, printers[1], [cycle])
    db.commit()
    usage = db.scalar(select(m.SupplyUsage))
    assert result["auto_assigned"] == 1
    assert usage.supply_order_id == order.id


def test_distinct_compatible_products_are_ranked_but_never_auto_guessed(db):
    _, hq, printers = _fleet(db)
    oem = _product(db, "W1470A", oem=True)
    alternate = _product(db, "ALT-147A", oem=False)
    for sku in (oem.sku, alternate.sku):
        db.add(
            m.SupplyOrder(
                site_id=hq.id,
                status=m.SupplyOrderStatus.delivered,
                model=printers[0].model,
                supply_type="toner",
                color="black",
                manufacturer="HP",
                sku=sku,
                quantity=1,
                delivered_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
            )
        )
    cycle = m.SupplyCycle(
        printer_id=printers[1].id,
        supply_type="toner",
        color="black",
        started_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        last_ts=datetime(2026, 8, 11, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        start_level_pct=100,
        end_level_pct=5,
        min_level_pct=5,
        complete=True,
    )
    db.add(cycle)
    db.flush()

    ranked = compatibility.products_for_printer(
        [alternate, oem],
        model=printers[1].model,
        supply_type="toner",
        color="black",
    )
    assert [product.sku for product in ranked] == ["W1470A", "ALT-147A"]
    result = inventory.reconcile_replacements(db, printers[1], [cycle])
    db.commit()
    usage = db.scalar(select(m.SupplyUsage))
    assert result["ambiguous"] == 1
    assert usage.supply_order_id is None


def test_longest_model_tag_is_reported_and_short_or_duplicate_tags_are_removed(db):
    product = m.SupplyProduct(
        manufacturer="HP",
        sku="X",
        product_key="hp|x",
        supply_type="toner",
        color="black",
        model_mappings=[
            m.SupplyProductModel(model_tag="LaserJet", model_key="laserjet"),
            m.SupplyProductModel(
                model_tag="LaserJet Enterprise M611",
                model_key="laserjet enterprise m611",
            ),
        ],
    )
    assert compatibility.matching_model_tag(
        product, "HP LaserJet Enterprise M611dn"
    ) == "LaserJet Enterprise M611"
    assert compatibility.clean_model_tags("HP, hp, x, M611") == ["M611"]


def test_staff_crud_is_audited_deduplicated_and_escaped(db):
    _fleet(db)
    http = _login(db)
    response = http.post(
        "/manage/supply-compatibility",
        data={
            "manufacturer": "<script>HP</script>",
            "sku": "W1470A",
            "description": "<img src=x onerror=alert(1)>",
            "supply_type": "toner",
            "color": "black",
            "is_oem": "true",
            "model_tags": "LaserJet Enterprise M611\nLaserJet Enterprise M612",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    product = db.scalar(select(m.SupplyProduct))
    assert product is not None
    assert len(product.model_mappings) == 2
    body = http.get("/manage/supply-compatibility").text
    assert "<script>HP</script>" not in body
    assert "&lt;script&gt;HP&lt;/script&gt;" in body
    assert "<img src=x onerror=alert(1)>" not in body
    assert 'aria-current="page"' in body
    assert 'href="/supplies/reorder"' in body

    duplicate = http.post(
        "/manage/supply-compatibility",
        data={
            "manufacturer": " <SCRIPT>hp</SCRIPT> ",
            "sku": "w1470a",
            "supply_type": "toner",
            "color": "black",
            "model_tags": "LaserJet Enterprise M611",
        },
        follow_redirects=False,
    )
    assert duplicate.status_code == 303
    assert len(list(db.scalars(select(m.SupplyProduct)))) == 1
    assert "supply_compatibility.create" in {
        row.action for row in db.scalars(select(m.AuditLog))
    }


def test_order_form_offers_and_applies_a_known_product(db):
    _, _, printers = _fleet(db)
    product = _product(db)
    db.commit()
    http = _login(db)
    body = http.get("/supplies/reorder").text
    assert "Known compatible product" in body
    assert "W1470A" in body

    response = http.post(
        "/supplies/orders",
        data={
            "supply_id": printers[0].supplies[0].id,
            "quantity": 1,
            "product_id": product.id,
            "manufacturer": "Wrong typed value",
            "sku": "WRONG",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    order = db.scalar(select(m.SupplyOrder))
    assert (order.manufacturer, order.sku) == ("HP", "W1470A")


def test_client_readonly_cannot_view_or_change_the_catalogue(db):
    client, _, _ = _fleet(db)
    http = _login(db, "viewer", m.UserRole.client_readonly, client.id)
    assert http.get(
        "/manage/supply-compatibility", follow_redirects=False
    ).headers["location"] == "/login"
    http.post(
        "/manage/supply-compatibility",
        data={
            "manufacturer": "HP",
            "sku": "W1470A",
            "supply_type": "toner",
            "model_tags": "LaserJet Enterprise M611",
        },
        follow_redirects=False,
    )
    assert db.scalar(select(m.SupplyProduct)) is None
