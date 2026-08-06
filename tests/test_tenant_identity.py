"""Whose printer is this? -- the question four screens could not answer.

Printer Nanny is multi-tenant, and the customer is the attribute that makes a
screen actionable. It was missing from every screen where a decision gets made:

* the **alerts inbox**, the operator's primary working queue, showed severity /
  title / state / opened and no customer -- and the row was not even a link, so
  it could not reach the device it was about;
* the **approvals queue** omitted it while asking the operator to put a
  discovered device into a specific tenant's fleet;
* the **printer page** identified the device by model and IP, and an RFC1918
  address collides across an MSP's customers *by design*;
* the **scope pickers** listed forty identical model names across six customers.

These tests pin the identity onto those surfaces, and pin the cost: the inbox is
unpaginated, so resolving tenancy must not become an N+1 over every open alert.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from central import models as m
from central.db import engine
from central.main import app
from central.security import hash_password


@pytest.fixture()
def http(db) -> TestClient:
    db.add(m.User(username="admin", password_hash=hash_password("pw12345678"),
                  role=m.UserRole.admin))
    db.commit()
    client = TestClient(app)
    resp = client.post("/login", data={"username": "admin", "password": "pw12345678"},
                       follow_redirects=False)
    assert resp.status_code == 303
    return client


def _fleet(db, client_name="Acme Legal", site_name="Head office", ip="10.0.0.5"):
    client = m.Client(name=client_name)
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name=site_name)
    db.add(site)
    db.flush()
    printer = m.Printer(client_id=client.id, site_id=site.id, ip=ip,
                        model="MFC-L8900CDW",
                        discovery_state=m.DiscoveryState.approved)
    db.add(printer)
    db.flush()
    return client, site, printer


def _alert(db, printer=None, agent=None, title="Toner low"):
    alert = m.Alert(
        printer_id=printer.id if printer else None,
        agent_id=agent.id if agent else None,
        type=m.AlertConditionType.supply_below,
        severity=m.EventSeverity.warning,
        state=m.AlertState.open,
        title=title,
        dedupe_key=f"k{title}{printer.id if printer else 0}",
    )
    db.add(alert)
    db.flush()
    return alert


# --------------------------------------------------------------------------- #
# The alerts inbox
# --------------------------------------------------------------------------- #
def test_the_inbox_names_the_customer(http, db):
    client, site, printer = _fleet(db)
    _alert(db, printer)
    db.commit()
    page = http.get("/alerts").text
    assert "Acme Legal" in page, "the alerts inbox does not name the customer"
    assert "Head office" in page, "the site is not shown"


def test_the_inbox_links_through_to_the_device(http, db):
    client, site, printer = _fleet(db)
    _alert(db, printer)
    db.commit()
    page = http.get("/alerts").text
    assert f'href="/printers/{printer.id}"' in page, (
        "the alert row is not a link to the printer it is about"
    )


def test_an_agent_alert_is_scoped_through_its_site(http, db):
    """Agent-level alerts have no printer, and were the only rows that would
    stay anonymous if tenancy were resolved through the printer alone."""
    client, site, printer = _fleet(db, client_name="Beta Dental")
    agent = m.Agent(site_id=site.id, name="beta-agent", api_key_hash="x")
    db.add(agent)
    db.flush()
    _alert(db, agent=agent, title="Agent offline")
    db.commit()
    page = http.get("/alerts").text
    assert "Beta Dental" in page


def test_an_unscoped_alert_says_so_rather_than_rendering_blank(http, db):
    """A blank cell reads as "we failed to look it up". A fleet-wide alert
    genuinely belongs to nobody and should say that."""
    _alert(db, title="Worker stalled")
    db.commit()
    page = http.get("/alerts").text
    assert "fleet-wide" in page


def test_resolving_tenancy_does_not_scale_queries_with_alerts(http, db):
    """The inbox has no pagination, so a per-row lookup would be an N+1 across
    every open alert in the fleet. Cost must be flat in the number of alerts."""
    client, site, printer = _fleet(db)
    for i in range(3):
        _alert(db, printer, title=f"Alert {i}")
    db.commit()

    counts = []

    def _count(conn, cursor, statement, params, context, executemany):
        counts.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        counts.clear()
        http.get("/alerts")
        few = len(counts)
        for i in range(3, 15):
            _alert(db, printer, title=f"Alert {i}")
        db.commit()
        counts.clear()
        http.get("/alerts")
        many = len(counts)
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert many <= few + 2, (
        f"query count grew from {few} to {many} for 4x the alerts -- that is an N+1"
    )


# --------------------------------------------------------------------------- #
# The approvals queue
# --------------------------------------------------------------------------- #
def test_approvals_names_the_customer_it_would_file_the_device_under(http, db):
    client, site, printer = _fleet(db, client_name="Gamma Freight")
    printer.discovery_state = m.DiscoveryState.pending
    db.commit()
    page = http.get("/approvals").text
    assert "Gamma Freight" in page, (
        "the approvals queue does not say whose fleet the device would join"
    )
    assert "Customer" in page


# --------------------------------------------------------------------------- #
# The printer page
# --------------------------------------------------------------------------- #
def test_the_printer_page_names_its_client_and_site(http, db):
    client, site, printer = _fleet(db, client_name="Delta Clinic", site_name="Ward B")
    db.commit()
    page = http.get(f"/printers/{printer.id}").text
    assert "Delta Clinic" in page
    assert "Ward B" in page
    assert 'aria-label="Breadcrumb"' in page


def test_the_delete_confirm_identifies_the_customer(http, db):
    """An RFC1918 address names a device at every customer at once, so a confirm
    that says only that has not identified what it is about to destroy."""
    client, site, printer = _fleet(db, client_name="Epsilon Ltd")
    db.commit()
    page = http.get(f"/printers/{printer.id}").text
    form = re.search(r'<form[^>]*printers/%d/delete.*?>' % printer.id, page, re.S)
    assert form, "no delete form found"
    assert 'data-who="Epsilon Ltd"' in form.group(0)
    # And the hostile value must still travel by dataset, never interpolated.
    assert "this.dataset.who" in page


# --------------------------------------------------------------------------- #
# Scope pickers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/manage/alert-rules", "/manage/suppression"])
def test_scope_options_are_distinguishable_between_customers(http, db, path):
    """Two customers, the same site name, the same printer model -- which is the
    normal case, not a contrived one."""
    _fleet(db, client_name="One Corp", site_name="Head office", ip="10.0.0.5")
    _fleet(db, client_name="Two Corp", site_name="Head office", ip="10.0.0.5")
    db.commit()
    page = http.get(path).text
    assert "One Corp / Head office" in page, f"{path} site options are ambiguous"
    assert "Two Corp / Head office" in page
    assert "One Corp / MFC-L8900CDW" in page, f"{path} printer options are ambiguous"
