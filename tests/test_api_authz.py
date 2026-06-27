"""Role gating + tenant isolation on the JSON management & reporting API.

Regression test for a cross-tenant leak: the ``/api/v1`` management router and
the ``/api/v1/reports`` reporting router were mounted with only ``require_user``,
so ANY logged-in user -- including a ``client_readonly`` customer session --
could:

* read every tenant's clients, sites, agents, and printers, and
* create/approve printers and enqueue agent commands (privilege escalation).

Both routers are operator-only (admin/tech). The customer-facing, tenant-scoped
read surface is the CSV exports (``/api/v1/reports/export/*``, already scoped and
covered by test_csv_exports.py) and the HTML ``/portal``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from central import models as m
from central.main import app
from central.security import hash_password


def _login(db, *, role: m.UserRole, client_id: int | None = None) -> TestClient:
    pwd = "pw" + role.value
    db.add(m.User(
        username=f"u_{role.value}",
        password_hash=hash_password(pwd),
        role=role,
        client_id=client_id,
    ))
    db.commit()
    cli = TestClient(app)
    resp = cli.post(
        "/login", data={"username": f"u_{role.value}", "password": pwd},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return cli


def _seed_client_site(db) -> tuple[m.Client, m.Site]:
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.commit()
    return client, site


# ---- management router: read access ----------------------------------------

def test_list_clients_admin_ok(db):
    _seed_client_site(db)
    http = _login(db, role=m.UserRole.admin)
    r = http.get("/api/v1/clients")
    assert r.status_code == 200
    assert any(c["name"] == "Acme" for c in r.json())


def test_list_clients_tech_ok(db):
    _seed_client_site(db)
    http = _login(db, role=m.UserRole.tech)
    assert http.get("/api/v1/clients").status_code == 200


def test_list_clients_client_readonly_forbidden(db):
    client, _ = _seed_client_site(db)
    http = _login(db, role=m.UserRole.client_readonly, client_id=client.id)
    assert http.get("/api/v1/clients").status_code == 403


def test_list_printers_client_readonly_forbidden(db):
    client, _ = _seed_client_site(db)
    http = _login(db, role=m.UserRole.client_readonly, client_id=client.id)
    assert http.get("/api/v1/printers").status_code == 403


def test_list_clients_unauthenticated_401(db):
    _seed_client_site(db)
    assert TestClient(app).get("/api/v1/clients").status_code == 401


# ---- management router: write access (privilege escalation) ----------------

def test_client_readonly_cannot_create_client(db):
    client, _ = _seed_client_site(db)
    http = _login(db, role=m.UserRole.client_readonly, client_id=client.id)
    r = http.post("/api/v1/clients", json={"name": "Evil"})
    assert r.status_code == 403
    assert db.query(m.Client).filter_by(name="Evil").count() == 0


def test_client_readonly_cannot_create_printer(db):
    client, site = _seed_client_site(db)
    http = _login(db, role=m.UserRole.client_readonly, client_id=client.id)
    r = http.post("/api/v1/printers", json={
        "client_id": client.id, "site_id": site.id, "ip": "10.0.0.5",
    })
    assert r.status_code == 403
    assert db.query(m.Printer).filter_by(ip="10.0.0.5").count() == 0


def test_tech_can_create_printer(db):
    """Staff write access is unchanged by the gate."""
    client, site = _seed_client_site(db)
    http = _login(db, role=m.UserRole.tech)
    r = http.post("/api/v1/printers", json={
        "client_id": client.id, "site_id": site.id, "ip": "10.0.0.6",
    })
    assert r.status_code == 201
    assert db.query(m.Printer).filter_by(ip="10.0.0.6").count() == 1


# ---- reporting router -------------------------------------------------------

def test_fleet_report_admin_ok(db):
    _seed_client_site(db)
    http = _login(db, role=m.UserRole.admin)
    r = http.get("/api/v1/reports/fleet")
    assert r.status_code == 200
    assert "total_printers" in r.json()


def test_fleet_report_client_readonly_forbidden(db):
    client, _ = _seed_client_site(db)
    http = _login(db, role=m.UserRole.client_readonly, client_id=client.id)
    assert http.get("/api/v1/reports/fleet").status_code == 403


def test_reporting_errors_client_readonly_forbidden(db):
    client, _ = _seed_client_site(db)
    http = _login(db, role=m.UserRole.client_readonly, client_id=client.id)
    assert http.get("/api/v1/reports/errors").status_code == 403


def test_reporting_unauthenticated_401(db):
    assert TestClient(app).get("/api/v1/reports/fleet").status_code == 401
