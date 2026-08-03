"""Tenant scoping belongs in the query, not in a comprehension after the LIMIT.

The customer portal used to read the newest 30 open alerts FLEET-WIDE and only
then keep the ones belonging to the logged-in customer. A LIMIT applied before
a tenant filter does not make the answer slow, it makes it WRONG: a customer
with a live critical fault was shown "No open issues right now" the moment
thirty newer alerts happened to belong to other customers -- the busiest
tenants on the box silently blinding the quietest ones.

These tests pin the reproduction, the boundaries around the cap, and the fact
that the scoped query is a single statement rather than one per alert.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from central import models as m
from central import queries
from central.db import engine
from central.main import app
from central.security import hash_password

# The portal's own cap. If the route changes it, these tests should be read
# again rather than silently re-tuned.
PORTAL_ALERT_LIMIT = 10


def _client_with_printer(db, name: str, cidr_octet: int):
    client = m.Client(name=name)
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip=f"10.{cidr_octet}.0.10",
        brand="Brother",
        model="MFC-L8900CDW",
        display_name=f"{name} Front Desk",
        status=m.PrinterStatus.ok,
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    return client, site, printer


def _alert(db, printer, *, title, minutes_ago, severity=m.EventSeverity.critical):
    row = m.Alert(
        printer_id=printer.id,
        type=m.AlertConditionType.error_severity,
        severity=severity,
        state=m.AlertState.open,
        title=title,
        dedupe_key=f"{printer.id}:{title}:{minutes_ago}",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    db.add(row)
    return row


def _readonly_user(db, client, username):
    db.add(
        m.User(
            username=username,
            password_hash=hash_password("pw"),
            role=m.UserRole.client_readonly,
            client_id=client.id,
        )
    )


def _login(username: str) -> TestClient:
    cli = TestClient(app)
    cli.post(
        "/login",
        data={"username": username, "password": "pw"},
        follow_redirects=False,
    )
    return cli


@pytest.fixture()
def two_tenants(db):
    """Quiet Ltd: one open critical fault, 5 hours old.

    Noisy Corp: thirty open alerts, every one of them NEWER. Under the old
    "newest 30 fleet-wide, then filter" the whole window was Noisy Corp's, so
    Quiet Ltd's genuine fault was invisible.
    """
    quiet, _, quiet_printer = _client_with_printer(db, "Quiet Ltd", 1)
    noisy, _, noisy_printer = _client_with_printer(db, "Noisy Corp", 2)
    _alert(db, quiet_printer, title="Paper jam in tray 2", minutes_ago=300)
    for i in range(30):
        _alert(
            db,
            noisy_printer,
            title=f"Noisy Corp condition {i}",
            minutes_ago=i + 1,
            severity=m.EventSeverity.warning,
        )
    _readonly_user(db, quiet, "quiet-ro")
    db.commit()
    return quiet, noisy, quiet_printer, noisy_printer


# --- The reproduction ------------------------------------------------------- #
def test_portal_shows_quiet_tenants_fault_behind_thirty_noisier_alerts(db, two_tenants):
    """The recipe that shipped broken: rendered 0 open issues while having 1."""
    body = _login("quiet-ro").get("/portal").text
    assert "Paper jam in tray 2" in body
    assert "No open issues right now" not in body
    # And nothing of the other tenant's leaked in while fixing that.
    assert "Noisy Corp condition" not in body


def test_open_alerts_scoped_ignores_newer_foreign_alerts(db, two_tenants):
    quiet, noisy, quiet_printer, _ = two_tenants
    rows = queries.open_alerts(db, PORTAL_ALERT_LIMIT, client_id=quiet.id)
    assert [r.title for r in rows] == ["Paper jam in tray 2"]
    # Unscoped is unchanged: still the newest N across the whole fleet.
    assert len(queries.open_alerts(db, 30)) == 30
    assert all(r.title.startswith("Noisy Corp") for r in queries.open_alerts(db, 30))


def test_open_alerts_never_returns_another_tenants_rows(db, two_tenants):
    quiet, noisy, quiet_printer, noisy_printer = two_tenants
    for scope, own in ((quiet.id, quiet_printer.id), (noisy.id, noisy_printer.id)):
        rows = queries.open_alerts(db, 100, client_id=scope)
        assert rows, "each tenant has open alerts of its own"
        assert {r.printer_id for r in rows} == {own}


# --- Boundaries around the cap ---------------------------------------------- #
def test_scoped_limit_counts_only_the_tenants_own_rows(db):
    """Exactly at the limit: the cap is spent on this tenant, never on others."""
    quiet, _, quiet_printer = _client_with_printer(db, "Quiet Ltd", 1)
    _noisy, _, noisy_printer = _client_with_printer(db, "Noisy Corp", 2)
    for i in range(PORTAL_ALERT_LIMIT):
        _alert(db, quiet_printer, title=f"quiet {i}", minutes_ago=100 + i)
    for i in range(50):
        _alert(db, noisy_printer, title=f"noisy {i}", minutes_ago=i + 1)
    db.commit()

    rows = queries.open_alerts(db, PORTAL_ALERT_LIMIT, client_id=quiet.id)
    assert len(rows) == PORTAL_ALERT_LIMIT
    assert {r.title for r in rows} == {f"quiet {i}" for i in range(PORTAL_ALERT_LIMIT)}


def test_scoped_limit_truncates_to_the_tenants_newest(db):
    """One over the limit: we drop the tenant's OLDEST, not a foreign row."""
    quiet, _, quiet_printer = _client_with_printer(db, "Quiet Ltd", 1)
    for i in range(PORTAL_ALERT_LIMIT + 1):
        # minutes_ago grows with i, so "quiet 0" is newest and the last is oldest.
        _alert(db, quiet_printer, title=f"quiet {i}", minutes_ago=10 * (i + 1))
    db.commit()

    rows = queries.open_alerts(db, PORTAL_ALERT_LIMIT, client_id=quiet.id)
    assert len(rows) == PORTAL_ALERT_LIMIT
    assert [r.title for r in rows] == [f"quiet {i}" for i in range(PORTAL_ALERT_LIMIT)]


def test_tenant_whose_alerts_are_all_older_than_another_tenants(db):
    """Every one of this tenant's alerts sorts below every one of the other's."""
    quiet, _, quiet_printer = _client_with_printer(db, "Quiet Ltd", 1)
    _noisy, _, noisy_printer = _client_with_printer(db, "Noisy Corp", 2)
    for i in range(5):
        _alert(db, quiet_printer, title=f"quiet {i}", minutes_ago=1000 + i)
    for i in range(200):
        _alert(db, noisy_printer, title=f"noisy {i}", minutes_ago=i + 1)
    _readonly_user(db, quiet, "quiet-ro")
    db.commit()

    # Render first: this must fail as a WRONG PAGE against the old code, not as
    # a TypeError on a keyword the old signature lacked.
    body = _login("quiet-ro").get("/portal").text
    for i in range(5):
        assert f"quiet {i}" in body
    assert "noisy" not in body
    assert len(queries.open_alerts(db, PORTAL_ALERT_LIMIT, client_id=quiet.id)) == 5


def test_agent_scope_alerts_are_excluded_when_scoping_to_a_client(db):
    """``printer_id IS NULL`` is a fleet signal about an agent, not a tenant's.

    The portal already dropped these; the inner join must keep dropping them
    rather than 500 on a null printer_id, and they must still reach the
    unscoped fleet view.
    """
    quiet, _, quiet_printer = _client_with_printer(db, "Quiet Ltd", 1)
    _alert(db, quiet_printer, title="Paper jam in tray 2", minutes_ago=300)
    db.add(
        m.Alert(
            printer_id=None,
            type=m.AlertConditionType.error_severity,
            severity=m.EventSeverity.critical,
            state=m.AlertState.open,
            title="Agent offline",
            dedupe_key="agent:1:offline",
            created_at=datetime.now(timezone.utc),
        )
    )
    db.commit()

    scoped = queries.open_alerts(db, 100, client_id=quiet.id)
    assert [r.title for r in scoped] == ["Paper jam in tray 2"]
    assert "Agent offline" in {r.title for r in queries.open_alerts(db, 100)}


def test_resolved_and_acknowledged_alerts_stay_out_of_the_scoped_query(db):
    quiet, _, quiet_printer = _client_with_printer(db, "Quiet Ltd", 1)
    _alert(db, quiet_printer, title="open one", minutes_ago=5)
    for state, title in (
        (m.AlertState.resolved, "resolved one"),
        (m.AlertState.acknowledged, "acked one"),
    ):
        row = _alert(db, quiet_printer, title=title, minutes_ago=1)
        row.state = state
    db.commit()

    assert [r.title for r in queries.open_alerts(db, 100, client_id=quiet.id)] == [
        "open one"
    ]


# --- The N+1 the Python filter required ------------------------------------- #
def test_scoped_alert_read_is_one_statement_not_one_per_alert(db):
    """``db.get(m.Printer, a.printer_id)`` per alert is gone with the join."""
    quiet, _, quiet_printer = _client_with_printer(db, "Quiet Ltd", 1)
    for i in range(PORTAL_ALERT_LIMIT):
        _alert(db, quiet_printer, title=f"quiet {i}", minutes_ago=i + 1)
    db.commit()
    db.expunge_all()

    statements: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        rows = queries.open_alerts(db, PORTAL_ALERT_LIMIT, client_id=quiet.id)
        assert len(rows) == PORTAL_ALERT_LIMIT
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    assert len(statements) == 1, statements


# --- Low supplies: the same scoping, pushed into SQL ------------------------- #
def _supply(db, printer, level, color="black"):
    db.add(
        m.Supply(
            printer_id=printer.id,
            type=m.SupplyType.toner,
            color=color,
            level_pct=level,
        )
    )


def test_low_supplies_scoped_to_one_tenant(db):
    quiet, _, quiet_printer = _client_with_printer(db, "Quiet Ltd", 1)
    _noisy, _, noisy_printer = _client_with_printer(db, "Noisy Corp", 2)
    _supply(db, quiet_printer, 7.0)
    for i, color in enumerate(("black", "cyan", "magenta", "yellow")):
        _supply(db, noisy_printer, 1.0 + i, color=color)
    db.commit()

    rows = queries.low_supplies(db, client_id=quiet.id)
    assert [r.printer_id for r in rows] == [quiet_printer.id]
    # Unscoped still sees the whole fleet.
    assert len(queries.low_supplies(db)) == 5


def test_low_supplies_limit_applies_after_the_tenant_filter(db):
    """A cap must never be spent on rows this tenant cannot see.

    The other tenant owns the four lowest supplies in the fleet. Asking for the
    tenant's two lowest must return the tenant's own two, not zero.
    """
    quiet, _, quiet_printer = _client_with_printer(db, "Quiet Ltd", 1)
    _noisy, _, noisy_printer = _client_with_printer(db, "Noisy Corp", 2)
    for i, color in enumerate(("black", "cyan", "magenta")):
        _supply(db, quiet_printer, 10.0 + i, color=color)
    for i, color in enumerate(("black", "cyan", "magenta", "yellow")):
        _supply(db, noisy_printer, 1.0 + i, color=color)
    db.commit()

    rows = queries.low_supplies(db, client_id=quiet.id, limit=2)
    assert [r.level_pct for r in rows] == [10.0, 11.0]
    assert {r.printer_id for r in rows} == {quiet_printer.id}


def test_low_supplies_still_excludes_unapproved_printers(db):
    quiet, site, quiet_printer = _client_with_printer(db, "Quiet Ltd", 1)
    pending = m.Printer(
        client_id=quiet.id,
        site_id=site.id,
        ip="10.1.0.11",
        discovery_state=m.DiscoveryState.pending,
    )
    db.add(pending)
    db.flush()
    _supply(db, quiet_printer, 9.0)
    _supply(db, pending, 1.0)
    db.commit()

    rows = queries.low_supplies(db, client_id=quiet.id)
    assert [r.printer_id for r in rows] == [quiet_printer.id]


def test_low_supplies_eager_loads_its_printer(db):
    """Both templates render ``supply.printer``; a lazy load there is an N+1."""
    quiet, _, quiet_printer = _client_with_printer(db, "Quiet Ltd", 1)
    for i, color in enumerate(("black", "cyan", "magenta", "yellow")):
        _supply(db, quiet_printer, 1.0 + i, color=color)
    db.commit()
    db.expunge_all()

    statements: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        # Explicit threshold: ``threshold=None`` reads the operator's setting,
        # which is a legitimate extra SELECT and would blunt this count.
        rows = queries.low_supplies(db, threshold=20.0, client_id=quiet.id)
        # Touching the relationship must not issue anything further.
        assert [r.printer.ip for r in rows] == ["10.1.0.10"] * 4
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    assert len(statements) == 1, statements


def test_portal_low_supplies_survive_a_noisier_tenant(db):
    """Guard against a future "optimisation" that caps the fleet-wide fetch."""
    quiet, _, quiet_printer = _client_with_printer(db, "Quiet Ltd", 1)
    _noisy, _, noisy_printer = _client_with_printer(db, "Noisy Corp", 2)
    _supply(db, quiet_printer, 11.0)
    for i in range(4):
        _supply(db, noisy_printer, 1.0 + i, color=f"c{i}")
    _readonly_user(db, quiet, "quiet-ro")
    db.commit()

    body = _login("quiet-ro").get("/portal").text
    assert "All supplies healthy." not in body
    assert "Quiet Ltd Front Desk" in body
    assert "Noisy Corp Front Desk" not in body
