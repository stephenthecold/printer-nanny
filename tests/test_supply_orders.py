"""Location-specific supply ordering, compatibility, and authorization."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import supply_orders
from central.main import app
from central.security import hash_password


def _fleet(db):
    client = m.Client(name="LCR")
    db.add(client)
    db.flush()
    hq = m.Site(client_id=client.id, name="HQ")
    branch = m.Site(client_id=client.id, name="Branch")
    db.add_all([hq, branch])
    db.flush()
    printers = []
    for site, ip, model, name in (
        (hq, "10.0.0.10", "Brother MFC-L8900CDW", "Front"),
        (hq, "10.0.0.11", "Brother MFC-L8900CDW", "Back"),
        (hq, "10.0.0.12", "HP M404", "Wrong model"),
        (branch, "10.1.0.10", "Brother MFC-L8900CDW", "Wrong location"),
    ):
        printer = m.Printer(
            client_id=client.id,
            site_id=site.id,
            ip=ip,
            model=model,
            brand=model.split()[0],
            display_name=name,
            discovery_state=m.DiscoveryState.approved,
        )
        db.add(printer)
        db.flush()
        db.add(m.Supply(
            printer_id=printer.id,
            type=m.SupplyType.toner,
            color="black",
            description="Black toner",
            level_pct=5,
        ))
        printers.append(printer)
    db.commit()
    return client, hq, printers


def _login(db, username="admin", role=m.UserRole.admin, client_id=None):
    db.add(m.User(
        username=username,
        password_hash=hash_password("pw12345678"),
        role=role,
        client_id=client_id,
    ))
    db.commit()
    http = TestClient(app)
    http.post(
        "/login",
        data={"username": username, "password": "pw12345678"},
        follow_redirects=False,
    )
    return http


def test_record_order_uses_the_supplies_location_and_device_identity(db):
    client, hq, printers = _fleet(db)
    http = _login(db)
    supply = printers[0].supplies[0]
    response = http.post(
        "/supplies/orders",
        data={
            "supply_id": supply.id,
            "quantity": 2,
            "manufacturer": "Brother",
            "sku": "TN-439BK",
            "vendor": "Sun Data Supply",
            "estimated_delivery_at": "2026-08-18",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/supplies/reorder?client_id={client.id}"
    order = db.scalar(select(m.SupplyOrder))
    assert order.site_id == hq.id
    assert order.printer_id == printers[0].id
    assert order.model == "Brother MFC-L8900CDW"
    assert order.quantity == 2
    assert order.sku == "TN-439BK"
    assert order.status == m.SupplyOrderStatus.ordered


def test_compatibility_is_exact_model_slot_and_location(db):
    _, hq, printers = _fleet(db)
    order = m.SupplyOrder(
        site_id=hq.id,
        model="Brother MFC-L8900CDW",
        supply_type="toner",
        color="black",
        description="Black toner",
        quantity=1,
    )
    db.add(order)
    db.commit()
    matches = supply_orders.compatible_printers(db, [order])[order.id]
    assert {p.display_name for p in matches} == {"Front", "Back"}
    assert "Wrong model" not in {p.display_name for p in matches}
    assert "Wrong location" not in {p.display_name for p in matches}


def test_an_active_compatible_order_prevents_a_duplicate(db):
    _, hq, printers = _fleet(db)
    db.add(m.SupplyOrder(
        site_id=hq.id,
        model="Brother MFC-L8900CDW",
        supply_type="toner",
        color="black",
        description="Black toner",
        quantity=1,
    ))
    db.commit()
    http = _login(db)
    http.post(
        "/supplies/orders",
        data={"supply_id": printers[1].supplies[0].id, "quantity": 1},
        follow_redirects=False,
    )
    assert len(list(db.scalars(select(m.SupplyOrder)))) == 1


def test_deliver_and_delete_are_audited(db):
    _, hq, _ = _fleet(db)
    order = m.SupplyOrder(
        site_id=hq.id,
        model="Brother MFC-L8900CDW",
        supply_type="toner",
        color="black",
        description="Black toner",
        quantity=1,
    )
    db.add(order)
    db.commit()
    order_id = order.id
    http = _login(db)
    http.post(f"/supplies/orders/{order_id}/delivered", follow_redirects=False)
    db.expire_all()
    assert db.get(m.SupplyOrder, order_id).status == m.SupplyOrderStatus.delivered
    http.post(f"/supplies/orders/{order_id}/delete", follow_redirects=False)
    db.expire_all()
    assert db.get(m.SupplyOrder, order_id) is None
    actions = {row.action for row in db.scalars(select(m.AuditLog))}
    assert {"supply_order.delivered", "supply_order.delete"} <= actions


def test_client_readonly_cannot_record_an_order(db):
    client, _, printers = _fleet(db)
    http = _login(db, "viewer", m.UserRole.client_readonly, client.id)
    response = http.post(
        "/supplies/orders",
        data={"supply_id": printers[0].supplies[0].id, "quantity": 1},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert not list(db.scalars(select(m.SupplyOrder)))


def test_order_metadata_is_escaped_on_the_supply_page(db):
    _, hq, _ = _fleet(db)
    db.add(m.SupplyOrder(
        site_id=hq.id,
        model="<script>alert(1)</script>",
        supply_type="toner",
        color="black",
        description="<img src=x onerror=alert(2)>",
        manufacturer="<b>OEM</b>",
        quantity=1,
    ))
    db.commit()
    body = _login(db).get("/supplies/reorder").text
    assert "<script>alert(1)</script>" not in body
    assert "<img src=x onerror=alert(2)>" not in body
    assert "&lt;script&gt;" in body
