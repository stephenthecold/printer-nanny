"""O365 shipping-mailbox parsing, matching, and operator reconciliation."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import models as m
from central import runtime
from central import shipping_mailbox as shipping
from central import supply_inventory
from central.main import app
from central.secrets import is_encrypted
from central.security import hash_password
from central.worker import jobs, run

NOW = datetime(2026, 8, 11, 16, 0, tzinfo=timezone.utc)


def _fleet(db):
    client = m.Client(name="LCR")
    db.add(client)
    db.flush()
    site = m.Site(
        client_id=client.id,
        name="Downtown",
        address="123 Main Street, Springfield, IL 62701",
    )
    other = m.Site(
        client_id=client.id,
        name="North",
        address="900 North Road, Springfield, IL 62702",
    )
    db.add_all([site, other])
    db.flush()
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip="10.0.0.10",
        model="Brother MFC-L8900CDW",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    return client, site, other, printer


def _order(db, site, printer, *, sku="TN-439BK", quantity=2):
    order = m.SupplyOrder(
        site_id=site.id,
        printer_id=printer.id,
        model=printer.model,
        supply_type="toner",
        color="black",
        description="Black toner",
        manufacturer="Brother",
        sku=sku,
        quantity=quantity,
    )
    db.add(order)
    db.flush()
    return order


def _message(message_id="graph-1", sender="shipping@sundatasupply.com"):
    return {
        "id": message_id,
        "internetMessageId": f"<{message_id}@supplier.test>",
        "receivedDateTime": "2026-08-11T15:30:00Z",
        "subject": "Your toner order has shipped",
        "from": {
            "emailAddress": {
                "name": "Sun Data Supply",
                "address": sender,
            }
        },
        "body": {
            "contentType": "html",
            "content": """
                <p>Shipping confirmation</p>
                <p>Item: Brother black toner</p>
                <p>SKU: TN-439BK</p><p>Quantity: 2</p>
                <p>Ship to: LCR Downtown<br>123 Main Street, Springfield, IL 62701</p>
                <p>Tracking number: 1Z999AA10123456784</p>
                <p>Estimated delivery: August 18, 2026</p>
            """,
        },
    }


def _settings(**overrides):
    values = {
        "shipping.enabled": True,
        "shipping.tenant_id": "tenant-guid",
        "shipping.client_id": "client-guid",
        "shipping.client_secret": "secret-not-real",
        "shipping.mailbox": "shipping@example.test",
        "shipping.allowed_senders": "sundatasupply.com",
        "shipping.poll_interval_min": 30,
        "shipping.initial_lookback_days": 14,
    }
    values.update(overrides)
    return values


def _graph_client(messages, *, next_link=""):
    def handler(request: httpx.Request):
        if request.method == "POST":
            assert request.url.host == "login.microsoftonline.com"
            return httpx.Response(200, json={"access_token": "token-not-real"})
        assert request.url.host == "graph.microsoft.com"
        body = {"value": messages}
        if next_link:
            body["@odata.nextLink"] = next_link
        return httpx.Response(200, json=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


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


def test_html_notice_is_reduced_to_operational_fields_only():
    parsed = shipping.parse_message(_message())

    assert parsed is not None
    assert parsed.sku == "TN-439BK"
    assert parsed.quantity == 2
    assert parsed.tracking_number == "1Z999AA10123456784"
    assert parsed.estimated_delivery_at == date(2026, 8, 18)
    assert "123 Main Street" in parsed.ship_to
    assert "<p>" not in repr(parsed)
    assert shipping.parse_message(
        {"id": "ordinary", "subject": "Hello", "body": {"content": "Lunch?"}}
    ) is None


def test_sync_matches_exact_address_and_sku_then_deduplicates(db):
    _, site, _, printer = _fleet(db)
    order = _order(db, site, printer)
    db.commit()
    client = _graph_client([_message()])

    first = shipping.sync_mailbox(
        db, settings=_settings(), client=client, now=NOW
    )["shipping_mailbox"]

    notice = db.scalar(select(m.ShippingNotice))
    assert first == {
        "messages": 1,
        "imported": 1,
        "linked": 1,
        "needs_review": 0,
        "ignored": 0,
    }
    assert notice.site_id == site.id
    assert notice.supply_order_id == order.id
    assert order.estimated_delivery_at == date(2026, 8, 18)
    assert "Shipping confirmation" not in (db.scalar(select(m.AuditLog)).detail or "")

    second = shipping.sync_mailbox(
        db,
        settings=_settings(),
        client=client,
        now=NOW + timedelta(minutes=31),
    )["shipping_mailbox"]
    assert second["imported"] == 0
    assert second["ignored"] == 1
    assert len(list(db.scalars(select(m.ShippingNotice)))) == 1
    client.close()


def test_sender_allowlist_and_unsafe_graph_paging_fail_closed(db):
    _fleet(db)
    client = _graph_client([_message(sender="shipping@attacker.test")])
    result = shipping.sync_mailbox(
        db, settings=_settings(), client=client, now=NOW
    )["shipping_mailbox"]
    assert result["imported"] == 0
    assert not list(db.scalars(select(m.ShippingNotice)))
    client.close()

    marker = db.get(m.AppSetting, "shipping.last_poll_at")
    db.delete(marker)
    db.commit()
    hostile = _graph_client([], next_link="http://169.254.169.254/latest/meta-data")
    with pytest.raises(shipping.ShippingMailboxError, match="unsafe next page"):
        shipping.sync_mailbox(db, settings=_settings(), client=hostile, now=NOW)
    hostile.close()


def test_unmatched_location_and_item_wait_for_assignment(db):
    _fleet(db)
    message = _message()
    message["body"]["content"] = (
        "Shipping update\nItem: Mystery toner\nShip to: Unknown warehouse\n"
        "Estimated delivery: 2026-08-18"
    )
    client = _graph_client([message])

    shipping.sync_mailbox(db, settings=_settings(), client=client, now=NOW)
    notice = db.scalar(select(m.ShippingNotice))
    assert notice.site_id is None
    assert notice.supply_order_id is None
    assert notice in shipping.pending_notices(db)
    client.close()


def test_due_linked_notice_becomes_delivered_location_stock(db):
    _, site, _, printer = _fleet(db)
    order = _order(db, site, printer, quantity=2)
    notice = m.ShippingNotice(
        source_message_id="due-1",
        mailbox="shipping@example.test",
        subject="Shipped",
        received_at=NOW,
        site_id=site.id,
        supply_order=order,
        estimated_delivery_at=date(2026, 8, 11),
    )
    db.add(notice)
    db.commit()

    assert shipping.assume_due_delivered(db, today=date(2026, 8, 11)) == 1
    db.refresh(order)
    assert order.status == m.SupplyOrderStatus.delivered
    assert supply_inventory.stock_groups(db)[0].on_hand == 2
    assert "supply_order.assumed_delivered" in {
        row.action for row in db.scalars(select(m.AuditLog))
    }


def test_staff_can_assign_or_delete_and_readonly_cannot(db):
    client, site, _, printer = _fleet(db)
    order = _order(db, site, printer)
    notice = m.ShippingNotice(
        source_message_id="ui-1",
        mailbox="shipping@example.test",
        subject="<script>alert(1)</script>",
        item_description="<img src=x onerror=alert(2)>",
        ship_to="Unknown",
        received_at=NOW,
    )
    db.add(notice)
    db.commit()
    notice_id = notice.id
    http = _login(db)

    body = http.get("/supplies/reorder").text
    assert "<script>alert(1)</script>" not in body
    assert "<img src=x onerror=alert(2)>" not in body
    response = http.post(
        f"/supplies/shipping/{notice_id}/assign",
        data={"order_id": order.id},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db.expire_all()
    assert db.get(m.ShippingNotice, notice_id).supply_order_id == order.id

    second = m.ShippingNotice(
        source_message_id="ui-2",
        mailbox="shipping@example.test",
        subject="Delete me",
        received_at=NOW,
    )
    db.add(second)
    db.commit()
    second_id = second.id
    http.post(f"/supplies/shipping/{second_id}/delete", follow_redirects=False)
    db.expire_all()
    assert db.get(m.ShippingNotice, second_id) is None

    third = m.ShippingNotice(
        source_message_id="ui-3",
        mailbox="shipping@example.test",
        subject="Protected",
        received_at=NOW,
    )
    db.add(third)
    db.commit()
    viewer = _login(
        db,
        username="viewer",
        role=m.UserRole.client_readonly,
        client_id=client.id,
    )
    viewer.post(f"/supplies/shipping/{third.id}/delete", follow_redirects=False)
    assert db.get(m.ShippingNotice, third.id) is not None


def test_shipping_settings_encrypt_the_secret_and_worker_is_registered(db):
    assert runtime.SETTINGS_GROUPS["procurement"][0] == "Supply workflow"
    runtime.save_settings(
        db,
        {
            "shipping.enabled": "on",
            "shipping.client_secret": "never-store-this-plaintext",
        },
        sections={"Shipping mailbox (O365)"},
    )
    stored = db.get(m.AppSetting, "shipping.client_secret")
    assert stored is not None
    assert stored.value != "never-store-this-plaintext"
    assert is_encrypted(stored.value)
    assert jobs.sync_shipping_mailbox in run.JOBS
