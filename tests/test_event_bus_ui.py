"""The operator surface and the worker wiring for the outbound event bus.

Three separate concerns, kept in one file because they all answer "does the
thing an operator or the worker actually touches behave":

* **The subscriptions page** -- authorisation, the once-shown secret, what the
  audit trail carries (and what it must not), and the destination check firing
  at save time rather than in a dead-lettered delivery an hour later.
* **The worker** -- that real alert and offline transitions emit real events,
  rather than the emit path being exercised only by tests calling it directly.
* **The migration** -- that ``0035_event_bus`` produces the same schema
  ``create_all`` does. Revision 0001 is ``create_all``, so a fresh install and
  an upgraded one are built by two different pieces of code, and nothing else in
  this repo compares them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect as sa_inspect

from central import models as m
from central.main import app
from central.secrets import decrypt_value, is_encrypted
from central.security import hash_password
from central.worker import jobs


def _login(db, role=m.UserRole.admin, username="admin") -> TestClient:
    db.add(m.User(
        username=username, password_hash=hash_password("pw"), role=role
    ))
    db.commit()
    client = TestClient(app)
    resp = client.post(
        "/login", data={"username": username, "password": "pw"}, follow_redirects=False
    )
    assert resp.status_code == 303
    return client


@pytest.fixture()
def public_dns(monkeypatch):
    """Pin name resolution so the save-time SSRF check is deterministic.

    Without it these tests would depend on the box having DNS, and a failure
    would look like a bug in the guard rather than an absent network.
    """
    monkeypatch.setattr(
        "central.events.destinations.resolve", lambda host, port: ["93.184.216.34"]
    )


# --------------------------------------------------------------------------- #
# Authorisation
# --------------------------------------------------------------------------- #
def test_the_page_is_admin_only(db):
    """A subscription mints a signing secret and points this server at a network
    address. That is the same class of decision as adding an operator account."""
    tech = _login(db, role=m.UserRole.tech, username="tech")
    resp = tech.get("/manage/events", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"

    resp = tech.post(
        "/manage/events",
        data={"name": "sneaky", "url": "https://evil.example.com/h"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db.query(m.EventSubscription).count() == 0


def test_an_admin_sees_the_page_and_the_catalogue(db):
    http = _login(db)
    resp = http.get("/manage/events")
    assert resp.status_code == 200
    for name in ("alert.opened", "alert.resolved", "printer.offline",
                 "supply.reorder_recommended"):
        assert name in resp.text


def test_a_quote_in_a_name_cannot_break_the_confirm_handler(db, public_dns):
    """Inside an inline handler the HTML parser decodes entities before JS runs.

    Autoescaping ``'`` to ``&#39;`` therefore still hands JavaScript a bare
    quote, which terminates the string literal early -- so the name goes through
    ``|tojson``, which emits ``\\u0027`` and survives both parsers.
    """
    from central.events.catalogue import EVENT_TYPE_NAMES

    http = _login(db)
    http.post(
        "/manage/events",
        data={
            "name": "Bob's <hooks> \"x\"", "url": "https://hooks.example.com/pn",
            "event_types": list(EVENT_TYPE_NAMES),
        },
        follow_redirects=False,
    )
    html = http.get("/manage/events").text
    assert "\\u0027" in html, "the name was not JSON-encoded for the handler"
    # And no raw quote or angle bracket from the name escaped into the markup.
    assert "confirm('Delete subscription Bob's" not in html
    assert "<hooks>" not in html


def test_the_nav_marks_the_events_page(db):
    http = _login(db)
    html = http.get("/manage/events").text
    assert 'aria-current="page"' in html
    assert 'href="/manage/events"' in html


# --------------------------------------------------------------------------- #
# Creating a subscription
# --------------------------------------------------------------------------- #
def test_creating_one_stores_an_encrypted_secret_and_shows_it_once(db, public_dns):
    http = _login(db)
    resp = http.post(
        "/manage/events",
        data={
            "name": "Acme ERP",
            "url": "https://hooks.example.com/pn",
            "client_id": "",
            "event_types": ["alert.opened", "supply.reorder_recommended"],
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    sub = db.query(m.EventSubscription).one()
    assert sub.name == "Acme ERP"
    assert sub.client_id is None
    assert sub.event_types == ["alert.opened", "supply.reorder_recommended"]

    # Encrypted at rest, exactly like directory_connections.secret.
    assert is_encrypted(sub.secret), "the signing secret was stored in the clear"
    plaintext = decrypt_value(sub.secret)
    assert plaintext.startswith("pnevt_")
    assert plaintext not in sub.secret

    # Shown exactly once, on the redirect that follows the create...
    assert plaintext in resp.text
    # ...and never again.
    assert plaintext not in http.get("/manage/events").text


def test_the_secret_never_reaches_the_audit_trail(db, public_dns):
    """The one thing an audit row must not carry is the thing that authenticates
    the payloads it is describing."""
    from central.events.catalogue import EVENT_TYPE_NAMES

    http = _login(db)
    http.post(
        "/manage/events",
        data={
            "name": "P", "url": "https://hooks.example.com/pn", "client_id": "",
            "event_types": list(EVENT_TYPE_NAMES),
        },
        follow_redirects=False,
    )
    sub = db.query(m.EventSubscription).one()
    secret = decrypt_value(sub.secret)

    rows = db.query(m.AuditLog).all()
    assert any(r.action == "event_subscription.create" for r in rows)
    for row in rows:
        assert secret not in (row.detail or "")
        assert secret not in (row.target or "")
        assert "pnevt_" not in (row.detail or "")


def test_ticking_every_type_stores_all_not_the_expanded_list(db, public_dns):
    """A type added in a later release must reach a subscription that meant 'all'."""
    http = _login(db)
    from central.events.catalogue import EVENT_TYPE_NAMES

    http.post(
        "/manage/events",
        data={
            "name": "P", "url": "https://hooks.example.com/pn", "client_id": "",
            "event_types": list(EVENT_TYPE_NAMES),
        },
        follow_redirects=False,
    )
    assert db.query(m.EventSubscription).one().event_types is None


def test_ticking_no_type_is_refused_rather_than_guessed(db, public_dns):
    """It looks equally like "I want it all" and "I forgot"; both guesses are
    wrong in one of those cases."""
    http = _login(db)
    resp = http.post(
        "/manage/events",
        data={"name": "P", "url": "https://hooks.example.com/pn", "event_types": []},
        follow_redirects=True,
    )
    assert db.query(m.EventSubscription).count() == 0
    assert "at least one event type" in resp.text


def test_a_metadata_address_is_refused_at_save_time(db):
    """Not discovered later in a dead-lettered delivery."""
    http = _login(db)
    resp = http.post(
        "/manage/events",
        data={"name": "oops", "url": "http://169.254.169.254/latest/meta-data/"},
        follow_redirects=True,
    )
    assert db.query(m.EventSubscription).count() == 0
    assert "link-local" in resp.text


def test_a_scope_pointing_at_no_client_is_refused(db, public_dns):
    http = _login(db)
    resp = http.post(
        "/manage/events",
        data={"name": "ghost", "url": "https://hooks.example.com/pn", "client_id": "999"},
        follow_redirects=True,
    )
    assert db.query(m.EventSubscription).count() == 0
    assert "no longer exists" in resp.text


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def _make_sub(db, http, client_id=""):
    from central.events.catalogue import EVENT_TYPE_NAMES

    http.post(
        "/manage/events",
        data={
            "name": "P", "url": "https://hooks.example.com/pn", "client_id": client_id,
            "event_types": list(EVENT_TYPE_NAMES),
        },
        follow_redirects=False,
    )
    return db.query(m.EventSubscription).one()


def test_rotating_replaces_the_secret_and_shows_the_new_one(db, public_dns):
    http = _login(db)
    sub = _make_sub(db, http)
    before = decrypt_value(sub.secret)

    resp = http.post("/manage/events/%d/rotate" % sub.id, follow_redirects=True)
    db.refresh(sub)
    after = decrypt_value(sub.secret)

    assert after != before
    assert after in resp.text
    assert before not in resp.text, "the retired secret was re-displayed"
    assert any(
        r.action == "event_subscription.rotate_secret" for r in db.query(m.AuditLog)
    )


def test_toggle_and_delete_are_audited(db, public_dns):
    from central.events.emit import emit

    http = _login(db)
    sub = _make_sub(db, http)
    sub_id = sub.id

    # Queue something against it: the delete must take its deliveries with it,
    # and SQLite does not enforce the ON DELETE CASCADE that would do so.
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    emit(db, "alert.opened", data={}, client_id=client.id, idempotency_key="k")
    db.commit()
    assert db.query(m.EventDelivery).count() == 1

    http.post("/manage/events/%d/toggle" % sub_id, follow_redirects=False)
    db.refresh(sub)
    assert sub.enabled is False

    http.post("/manage/events/%d/delete" % sub_id, follow_redirects=False)
    assert db.query(m.EventSubscription).count() == 0
    assert db.query(m.EventDelivery).count() == 0, "queued deliveries were orphaned"
    actions = {r.action for r in db.query(m.AuditLog)}
    assert "event_subscription.disable" in actions
    assert "event_subscription.delete" in actions


def test_editing_can_rescope_and_records_both_sides(db, public_dns):
    http = _login(db)
    client = m.Client(name="Acme")
    db.add(client)
    db.commit()
    sub = _make_sub(db, http)

    http.post(
        "/manage/events/%d/update" % sub.id,
        data={
            "name": "Acme only", "url": "https://hooks.example.com/v2",
            "client_id": str(client.id), "event_types": ["alert.opened"],
        },
        follow_redirects=False,
    )
    db.refresh(sub)
    assert sub.client_id == client.id
    assert sub.url == "https://hooks.example.com/v2"
    assert sub.event_types == ["alert.opened"]
    detail = [
        r.detail for r in db.query(m.AuditLog)
        if r.action == "event_subscription.update"
    ][0]
    assert "was" in detail and "now" in detail


def test_a_test_send_signs_a_marked_synthetic_event(db, public_dns, monkeypatch):
    """It must be unmistakable to a subscriber, and must not enter the retry log."""
    import httpx

    from central.events import signing

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        seen["body"] = request.content
        seen["sig"] = request.headers[signing.SIGNATURE_HEADER]
        return httpx.Response(200, content=b"ok")

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handler), **kw),
    )

    http = _login(db)
    sub = _make_sub(db, http)
    secret = decrypt_value(sub.secret)

    resp = http.post("/manage/events/%d/test" % sub.id, follow_redirects=True)
    assert resp.status_code == 200
    assert signing.verify(secret, seen["body"], seen["sig"])
    assert b'"test":true' in seen["body"]
    # A test is not a fact: nothing enters the durable log.
    assert db.query(m.OutboundEvent).count() == 0
    assert db.query(m.EventDelivery).count() == 0
    db.refresh(sub)
    assert sub.last_ok is True
    assert any(r.action == "event_subscription.test" for r in db.query(m.AuditLog))


# --------------------------------------------------------------------------- #
# Worker wiring -- the emit calls are reached by real transitions
# --------------------------------------------------------------------------- #
def _fleet(db):
    now = datetime.now(timezone.utc)
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    db.add(m.Agent(
        site_id=site.id, name="hq", api_key_hash="x",
        status=m.AgentStatus.online, last_heartbeat=now,
    ))
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.5", model="HP M404",
        discovery_state=m.DiscoveryState.approved, status=m.PrinterStatus.ok,
        last_seen=now - timedelta(minutes=120),
    )
    db.add(printer)
    db.commit()
    return client, printer


def test_a_printer_going_offline_emits_an_event(db):
    client, printer = _fleet(db)
    db.add(m.EventSubscription(
        name="msp", client_id=None, url="https://hooks.example.com/pn",
        secret="enc-placeholder", enabled=True,
    ))
    db.commit()

    assert jobs.mark_offline_printers(db)["printers_marked_offline"] == 1

    event = db.query(m.OutboundEvent).one()
    assert event.type == "printer.offline"
    assert event.client_id == client.id
    assert event.data["printer"]["ip"] == "10.0.0.5"
    assert db.query(m.EventDelivery).count() == 1

    # The same stale transition on a later cycle emits nothing further.
    jobs.mark_offline_printers(db)
    assert db.query(m.OutboundEvent).count() == 1


def test_an_alert_opening_and_resolving_emits_both_events(db):
    client, printer = _fleet(db)
    db.add(m.EventSubscription(
        name="msp", client_id=None, url="https://hooks.example.com/pn",
        secret="enc-placeholder", enabled=True,
    ))
    db.add(m.AlertRule(
        name="printer offline",
        condition_type=m.AlertConditionType.printer_offline,
        threshold=30, severity=m.EventSeverity.critical,
    ))
    db.commit()

    jobs.mark_offline_printers(db)
    jobs.evaluate_alerts(db)
    types = [e.type for e in db.query(m.OutboundEvent).order_by(m.OutboundEvent.id)]
    assert "alert.opened" in types

    # Bring it back: the condition clears and the alert resolves.
    printer.status = m.PrinterStatus.ok
    printer.last_seen = datetime.now(timezone.utc)
    db.commit()
    jobs.evaluate_alerts(db)

    types = [e.type for e in db.query(m.OutboundEvent).order_by(m.OutboundEvent.id)]
    assert "alert.resolved" in types
    opened = db.query(m.OutboundEvent).filter(
        m.OutboundEvent.type == "alert.opened"
    ).one()
    assert opened.client_id == client.id


def test_a_real_flap_through_the_evaluator_emits_the_reopen(db):
    """The end-to-end version of the flap-generation rule.

    Driven through ``evaluate_alerts`` rather than by calling ``emit`` directly,
    because the defect this guards against lives in *where* the emit is called
    from: the flap path returns early from ``_open_alert`` and would otherwise
    never reach one.
    """
    from central.runtime import save_settings

    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.5",
        discovery_state=m.DiscoveryState.approved, status=m.PrinterStatus.ok,
    )
    db.add(printer)
    db.flush()
    supply = m.Supply(printer_id=printer.id, type=m.SupplyType.toner,
                      color="black", level_pct=9.0)
    db.add(supply)
    db.add(m.AlertRule(
        name="low toner", condition_type=m.AlertConditionType.supply_below,
        threshold=10, severity=m.EventSeverity.warning,
    ))
    db.add(m.EventSubscription(
        name="msp", client_id=None, url="https://hooks.example.com/pn",
        secret="enc-placeholder", enabled=True,
    ))
    # Deadband off so the resolve really happens; cooldown on to catch the re-fire.
    save_settings(db, {
        "alerts.supply_deadband_pct": "0", "alerts.renotify_cooldown_min": "30",
    }, sections={"Alerts"})
    db.commit()

    assert jobs.evaluate_alerts(db)["alerts_opened"] == 1
    supply.level_pct = 20.0
    db.commit()
    assert jobs.evaluate_alerts(db)["alerts_resolved"] == 1
    supply.level_pct = 9.0
    db.commit()
    res = jobs.evaluate_alerts(db)
    assert res["alerts_opened"] == 0 and res["alerts_flapped"] == 1

    alert = db.query(m.Alert).one()
    keys = [
        e.idempotency_key
        for e in db.query(m.OutboundEvent).order_by(m.OutboundEvent.id)
    ]
    assert keys == [
        "alert.opened:alert:%d:flap:0" % alert.id,
        "alert.resolved:alert:%d:flap:0" % alert.id,
        "alert.opened:alert:%d:flap:1" % alert.id,
    ], "a subscriber was left holding 'resolved' for a live fault"


def test_an_operator_resolving_by_hand_emits_the_resolve(db):
    """A subscriber that only heard the open is otherwise left holding a fault
    that no longer exists."""
    client, printer = _fleet(db)
    db.add(m.EventSubscription(
        name="msp", client_id=None, url="https://hooks.example.com/pn",
        secret="enc-placeholder", enabled=True,
    ))
    alert = m.Alert(
        printer_id=printer.id, type=m.AlertConditionType.printer_offline,
        severity=m.EventSeverity.critical, state=m.AlertState.open,
        title="offline", dedupe_key="k", detail="",
    )
    db.add(alert)
    db.commit()

    http = _login(db)
    resp = http.post("/alerts/%d/resolve" % alert.id)
    assert resp.status_code == 200

    event = db.query(m.OutboundEvent).one()
    assert event.type == "alert.resolved"
    assert event.idempotency_key == "alert.resolved:alert:%d:flap:0" % alert.id


def test_deliver_events_is_in_the_worker_cycle():
    from central.worker import run

    assert jobs.deliver_events in run.JOBS
    # After the notification paths: a person being paged is more urgent than an
    # integration being told.
    assert run.JOBS.index(jobs.deliver_events) > run.JOBS.index(jobs.retry_deliveries)


# --------------------------------------------------------------------------- #
# Migration parity
# --------------------------------------------------------------------------- #
def _schema(inspector, table):
    """Everything about a table that a divergence could hide in."""
    return {
        "columns": sorted(
            (c["name"], type(c["type"]).__name__, bool(c["nullable"]))
            for c in inspector.get_columns(table)
        ),
        "indexes": sorted(
            (i["name"], tuple(i["column_names"]), bool(i["unique"]))
            for i in inspector.get_indexes(table)
        ),
        "unique": sorted(
            (u["name"], tuple(u["column_names"]))
            for u in inspector.get_unique_constraints(table)
        ),
    }


def test_the_migration_builds_what_create_all_builds(tmp_path, db):
    """Revision 0001 is ``create_all``, so a fresh install and an upgraded one
    are built by two different pieces of code. This compares them.

    ``alembic upgrade head`` is deliberately NOT used: this revision's parent
    (``0034_readings_retention``) is being written concurrently, so the chain
    cannot be walked here. The revision's ``upgrade()`` is driven directly
    against an Operations context instead, which is the part that would actually
    diverge.
    """
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    # Loaded by path: a revision filename starts with a digit, so it is not an
    # importable module name and never appears on sys.path.
    path = (
        Path(__file__).resolve().parent.parent
        / "migrations" / "versions" / "0035_event_bus.py"
    )
    spec = importlib.util.spec_from_file_location("rev_0035_event_bus", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # The parent is NOT pinned by name. This revision was authored concurrently
    # with nine siblings, and they landed in a different order than their slots
    # were assigned, so each was re-pointed at the real head when it merged.
    # Asserting a specific parent would pin a coordination artifact and fail on
    # every future reorder; what actually has to hold is that the parent exists,
    # which is what makes the chain walkable.
    parents = {
        p.stem for p in path.parent.glob("*.py") if p.name != "__init__.py"
    }
    assert any(
        stem.startswith(module.down_revision.split("_")[0]) for stem in parents
    ), f"down_revision {module.down_revision!r} names no revision file"

    tables = ("event_subscriptions", "outbound_events", "event_deliveries")

    engine = create_engine("sqlite:///%s" % (tmp_path / "migrated.sqlite3"))
    with engine.begin() as conn:
        # The FK parent only; everything else is the migration's job.
        m.Client.__table__.create(conn)
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
        migrated = {t: _schema(sa_inspect(conn), t) for t in tables}

    from central.db import engine as orm_engine

    fresh = {t: _schema(sa_inspect(orm_engine), t) for t in tables}

    for table in tables:
        assert migrated[table] == fresh[table], (
            "%s differs between the migration and create_all:\n  migration: %r\n"
            "  create_all: %r" % (table, migrated[table], fresh[table])
        )
