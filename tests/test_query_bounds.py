"""The three unbounded queries, and the counts that keep them bounded.

Every assertion here is a **statement count** or a row-reachability claim, never
a timing. A timing assertion on a shared runner is a coin flip that eventually
gets marked flaky and deleted, taking the guarantee with it; a statement count
is exact, is the same on SQLite and Postgres, and fails loudly the moment
somebody reintroduces a query inside a loop.

What is being locked in:

O4  ``/manage/audit`` was ``LIMIT 200`` with no offset, so on an install with
    100,000 audit rows 99,800 of them could not be reached from any URL. The
    trail that exists to be complete looked complete and was not.
O5  ``per_client_rollup`` was 5N+1 -- four counts plus a lazy ``client.sites``
    per client -- so the Overview page issued 1,002 statements at 200 clients.
O6  ``retry_due`` fetched and processed the ENTIRE due set in one worker cycle,
    inside the leader lock, one network round trip at a time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select

from central import models as m
from central import queries
from central.channels.base import ChannelResult, Notification, NotificationChannel
from central.channels.delivery import BATCH_LIMIT, retry_due
from central.db import engine


# --------------------------------------------------------------------------- #
# Counting statements. `before_cursor_execute` fires once per statement the
# engine actually sends, which is what we mean by "a query" -- it counts a lazy
# relationship load (invisible to a grep for `select(`) exactly like an explicit
# one, and that is the whole point: the `len(client.sites)` in per_client_rollup
# was one fifth of the N+1 and looked like an attribute access.
# --------------------------------------------------------------------------- #
class _Counter:
    def __init__(self):
        self.statements = []

    def __len__(self):
        return len(self.statements)

    def _hook(self, conn, cursor, statement, params, context, executemany):
        self.statements.append(statement)

    def __enter__(self):
        event.listen(engine, "before_cursor_execute", self._hook)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._hook)
        return False


@pytest.fixture()
def count_queries():
    return _Counter


# --------------------------------------------------------------------------- #
# O5 -- per_client_rollup is constant in the number of clients.
# --------------------------------------------------------------------------- #
def _fleet(db, clients: int, printers_per_client: int = 3, prefix="Client"):
    for c in range(clients):
        client = m.Client(name=f"{prefix} {c:03d}")
        db.add(client)
        db.flush()
        site = m.Site(client_id=client.id, name=f"Site {c}")
        db.add(site)
        db.flush()
        for p in range(printers_per_client):
            printer = m.Printer(
                client_id=client.id, site_id=site.id, ip=f"10.{c}.0.{p}",
                discovery_state=m.DiscoveryState.approved,
                status=m.PrinterStatus.offline if p == 0 else m.PrinterStatus.ok,
            )
            db.add(printer)
            db.flush()
            db.add(m.Supply(printer_id=printer.id, type=m.SupplyType.toner,
                            color="black", level_pct=5.0 if p == 0 else 90.0))
            if p == 0:
                db.add(m.Alert(
                    printer_id=printer.id, type=m.AlertConditionType.supply_below,
                    severity=m.EventSeverity.warning, state=m.AlertState.open,
                    title="Low toner", dedupe_key=f"k:{printer.id}",
                ))
    db.commit()


def test_per_client_rollup_query_count_does_not_grow_with_the_fleet(db, count_queries):
    """The N+1 test that matters: the same page, ten times the clients, the
    same number of statements. Measured before the fix on 200 clients / 4,000
    printers: 1,002 statements, 776ms."""
    _fleet(db, clients=2, prefix="Small")
    with count_queries() as small:
        queries.per_client_rollup(db)
    db.expire_all()

    _fleet(db, clients=20, prefix="Large")
    with count_queries() as large:
        rows = queries.per_client_rollup(db)
    assert len(rows) == 22

    assert len(small) == len(large), (
        f"{len(small)} statements for 2 clients but {len(large)} for 22 -- "
        "something is querying per client again"
    )
    # Five aggregates + the client list + the low-supply threshold setting.
    assert len(large) <= 8, [s.split("\n")[0] for s in large.statements]


def test_per_client_rollup_numbers_are_unchanged_by_the_grouped_rewrite(db):
    """Counting in SQL is only a win if it counts the same things. Each number
    is asserted against a client whose composition is known exactly."""
    _fleet(db, clients=3, printers_per_client=4)
    # A client with nothing at all must still appear, with zeros -- it is absent
    # from every GROUP BY, so this is the row a dict-lookup rewrite drops.
    db.add(m.Client(name="Zzz Empty"))
    db.commit()

    rows = {r["client"].name: r for r in queries.per_client_rollup(db)}
    assert list(rows) == sorted(rows), "still ordered by name"

    row = rows["Client 000"]
    assert row["printer_count"] == 4          # all approved
    assert row["offline_count"] == 1          # printer 0 only
    assert row["open_alerts"] == 1
    assert row["low_supplies"] == 1           # printer 0's black at 5%
    assert row["sites_count"] == 1

    empty = rows["Zzz Empty"]
    assert (empty["printer_count"], empty["offline_count"], empty["open_alerts"],
            empty["low_supplies"], empty["sites_count"]) == (0, 0, 0, 0, 0)


def test_per_client_rollup_counts_are_not_multiplied_by_the_joins(db):
    """The trap in collapsing this to one statement: a printer with 4 supplies
    and 2 open alerts joins to 8 rows, and every count comes back wrong. Four
    supplies and two alerts on one printer, so a fused query would say 8."""
    client = m.Client(name="Multi")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="S")
    db.add(site)
    db.flush()
    printer = m.Printer(client_id=client.id, site_id=site.id, ip="10.0.0.1",
                        discovery_state=m.DiscoveryState.approved,
                        status=m.PrinterStatus.ok)
    db.add(printer)
    db.flush()
    for colour in ("black", "cyan", "magenta", "yellow"):
        db.add(m.Supply(printer_id=printer.id, type=m.SupplyType.toner,
                        color=colour, level_pct=1.0))
    for k in range(2):
        db.add(m.Alert(printer_id=printer.id, type=m.AlertConditionType.supply_below,
                       severity=m.EventSeverity.warning, state=m.AlertState.open,
                       title="t", dedupe_key=f"k{k}"))
    db.commit()

    row = queries.per_client_rollup(db)[0]
    assert row["printer_count"] == 1
    assert row["low_supplies"] == 4
    assert row["open_alerts"] == 2


# --------------------------------------------------------------------------- #
# O4 -- the audit trail is reachable in full, and filtering happens in SQL.
# --------------------------------------------------------------------------- #
def _audit(db, n: int, action="login", username="tech1", target="printer:1"):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.add_all([
        m.AuditLog(ts=base + timedelta(seconds=i), action=action,
                   username=username, target=target, ip="10.0.0.1", detail=f"d{i}")
        for i in range(n)
    ])
    db.commit()


def test_every_audit_row_is_reachable_by_paging(db):
    """The defect, stated as a test: with ``LIMIT 200`` and no offset the 201st
    newest row could not be reached from anywhere in the product."""
    _audit(db, 450)
    seen = set()
    page = 1
    while True:
        result = queries.audit_page(db, page=page, per_page=100)
        seen.update(r.detail for r in result["rows"])
        if page >= result["pages"]:
            break
        page += 1
    assert len(seen) == 450, "a row exists that no page returns"
    assert result["pages"] == 5


def test_audit_pages_do_not_repeat_or_skip_a_row(db):
    """Rows written inside one clock tick share a ``ts``. Ordering by ``ts``
    alone leaves them free to swap between requests, and OFFSET paging over an
    unstable order then shows one row twice and another never -- silent gaps in
    the record of record."""
    same_ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    db.add_all([
        m.AuditLog(ts=same_ts, action="settings.update", username="admin",
                   target=f"key:{i}", detail=f"row{i}")
        for i in range(60)
    ])
    db.commit()

    first = [r.detail for r in queries.audit_page(db, page=1, per_page=20)["rows"]]
    second = [r.detail for r in queries.audit_page(db, page=2, per_page=20)["rows"]]
    third = [r.detail for r in queries.audit_page(db, page=3, per_page=20)["rows"]]
    everything = first + second + third
    assert len(set(everything)) == 60
    # And it is stable across repeated requests, not merely disjoint once.
    assert [r.detail for r in queries.audit_page(db, page=2, per_page=20)["rows"]] == second


def test_audit_filter_is_applied_before_the_limit_not_after(db):
    """The bug this codebase already shipped once, in the customer portal: take
    a page, then narrow it. Here 500 newer ``login`` rows bury the one
    ``backup.download`` an auditor is looking for."""
    _audit(db, 500, action="login")
    db.add(m.AuditLog(ts=datetime(2020, 1, 1, tzinfo=timezone.utc),
                      action="backup.download", username="admin",
                      target="backup", detail="the one that matters"))
    db.commit()

    result = queries.audit_page(db, q="backup.download")
    assert result["total"] == 1
    assert [r.detail for r in result["rows"]] == ["the one that matters"]


def test_audit_filter_paginates_over_the_filtered_set(db):
    """Filter and pagination compose: page 2 of a filter is page 2 of the
    matches, not the matches within page 2 of everything."""
    _audit(db, 300, action="login", username="tech1")
    _audit(db, 250, action="login.failed", username="tech2")

    result = queries.audit_page(db, q="login.failed", page=2, per_page=100)
    assert result["total"] == 250
    assert result["pages"] == 3
    assert len(result["rows"]) == 100
    assert all(r.action == "login.failed" for r in result["rows"])


def test_audit_filter_escapes_like_metacharacters(db):
    """``%`` in an operator's filter used to mean "match everything". A security
    surface whose filter silently widens fails open: it reads as "nothing
    matched that narrowing", which is how somebody concludes an action was never
    logged."""
    _audit(db, 5, action="login", target="printer:1")
    db.add(m.AuditLog(ts=datetime(2026, 2, 1, tzinfo=timezone.utc),
                      action="settings.update", username="admin",
                      target="100%_done", detail="literal"))
    db.commit()

    assert queries.audit_page(db, q="%")["total"] == 1     # not 6
    assert queries.audit_page(db, q="100%_d")["total"] == 1
    assert queries.audit_page(db, q="_")["total"] == 1     # `_` is not "any char"


def test_audit_page_is_clamped_not_errored(db):
    _audit(db, 30)
    assert queries.audit_page(db, page=9999, per_page=10)["page"] == 3
    assert queries.audit_page(db, page=0)["page"] == 1
    assert queries.audit_page(db, page="banana")["page"] == 1
    # A per_page an operator cannot set from the UI still cannot be huge.
    assert queries.audit_page(db, per_page=10_000)["per_page"] <= 200


def test_audit_page_costs_two_statements_whatever_the_page(db, count_queries):
    _audit(db, 900)
    for page in (1, 5, 9):
        with count_queries() as c:
            queries.audit_page(db, page=page, per_page=100)
        assert len(c) == 2, [s.split("\n")[0] for s in c.statements]


# --------------------------------------------------------------------------- #
# O6 -- the retry sweep is bounded, ordered, and reaches the tail.
# --------------------------------------------------------------------------- #
class _Recorder(NotificationChannel):
    type = "rec"

    def __init__(self, name="Email", ok=False):
        super().__init__(name, config={}, runtime={})
        self.sent = 0
        self._ok = ok

    def send(self, note: Notification) -> ChannelResult:
        self.sent += 1
        return ChannelResult(ok=self._ok, detail="ok" if self._ok else "down")


def _deliveries(db, n: int, channel_key: str, *, due_at, alert_id=None):
    db.add_all([
        m.NotificationDelivery(
            alert_id=alert_id, channel_key=channel_key,
            status=m.DeliveryStatus.failed, attempts=1,
            next_attempt_at=due_at - timedelta(seconds=i),
            payload={"title": f"{channel_key} {i}", "body": "",
                     "severity": "warning"},
        )
        for i in range(n)
    ])
    db.commit()


def test_retry_due_processes_at_most_one_batch_per_cycle(db, monkeypatch):
    """It ran the whole due set in one cycle, inside the leader lock. Measured
    before the fix on a 5,000-row backlog: 8,501 statements and 8.3 seconds --
    with a 2ms fake channel, so a real SMTP round trip makes it far worse. Alert
    evaluation, forecasting and offline marking simply did not run that cycle."""
    now = datetime.now(timezone.utc)
    _deliveries(db, BATCH_LIMIT * 3, "Email", due_at=now - timedelta(minutes=5))
    ch = _Recorder()
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])

    res = retry_due(db, {"notifications.max_attempts": 99}, now=now)
    assert ch.sent == BATCH_LIMIT
    assert res["deliveries_retried"] == BATCH_LIMIT


def test_a_disabled_channels_backlog_cannot_block_a_live_one(db, monkeypatch):
    """The starvation a bare LIMIT would have introduced. Every Slack row is
    older than every Email row, so they sort first; Slack is disabled, so
    nothing can be done with them and processing them modifies nothing. Fetch
    them and the batch is 200 no-ops, forever, while Email never sends."""
    now = datetime.now(timezone.utc)
    _deliveries(db, 1000, "Slack", due_at=now - timedelta(days=10))
    _deliveries(db, 1000, "Email", due_at=now - timedelta(minutes=1))
    ch = _Recorder()   # Slack absent from active_channels == disabled
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])

    res = retry_due(db, {"notifications.max_attempts": 99}, now=now)
    assert ch.sent == BATCH_LIMIT, "the live channel's backlog was starved"
    # The operator is still told about the whole stuck set, not the batch-sized
    # slice of it -- a number that shrank because the sweep was bounded would
    # read as a backlog draining when nothing has drained.
    assert res["deliveries_skipped"] == 1000
    # And a disabled channel's rows are genuinely untouched.
    assert db.scalar(
        select(func.count()).select_from(m.NotificationDelivery)
        .where(m.NotificationDelivery.channel_key == "Slack",
               m.NotificationDelivery.attempts != 1)
    ) == 0


def test_the_sweep_reaches_the_tail_of_the_backlog(db, monkeypatch):
    """Bounded is not enough; it has to rotate. Ten cycles over a 1,000-row
    backlog must spread evenly rather than re-running the same 200 -- measured
    on Postgres, every row lands on exactly the same attempt count."""
    now = datetime.now(timezone.utc)
    _deliveries(db, 1000, "Email", due_at=now - timedelta(minutes=1))
    ch = _Recorder()
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])

    for k in range(10):
        retry_due(db, {"notifications.max_attempts": 999},
                  now=now + timedelta(hours=2 * k))

    attempts = [a for (a,) in db.execute(
        select(m.NotificationDelivery.attempts)).all()]
    assert ch.sent == 10 * BATCH_LIMIT
    assert min(attempts) == 3 and max(attempts) == 3, (
        f"work is concentrated on the head: min={min(attempts)} "
        f"max={max(attempts)}"
    )


def test_owed_rows_rotate_instead_of_re_examining_the_same_head(db, monkeypatch):
    """An owed row with no channel to go to is deliberately not charged an
    attempt, so nothing about it changes -- which with a LIMIT means the same
    head is reconsidered every cycle and rows behind it never get their
    give-up check. It is stamped due-now instead, which keeps it recoverable
    the instant a channel appears and puts it behind everything still waiting.
    """
    from central.channels.delivery import UNROUTED_CHANNEL_KEY

    now = datetime.now(timezone.utc)
    db.add_all([
        m.NotificationDelivery(
            channel_key=UNROUTED_CHANNEL_KEY, status=m.DeliveryStatus.pending,
            attempts=0, next_attempt_at=None,
            payload={"title": f"owed {i}", "body": "", "severity": "warning"},
        )
        for i in range(BATCH_LIMIT + 50)
    ])
    db.commit()
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [])

    res = retry_due(db, {}, now=now)
    assert res["deliveries_owed"] == BATCH_LIMIT
    rotated = db.scalar(
        select(func.count()).select_from(m.NotificationDelivery)
        .where(m.NotificationDelivery.next_attempt_at.is_not(None)))
    assert rotated == BATCH_LIMIT
    # No attempt burned -- an unconfigured system must not eat the retry budget.
    assert db.scalar(
        select(func.count()).select_from(m.NotificationDelivery)
        .where(m.NotificationDelivery.attempts != 0)) == 0

    # Next cycle reaches the 50 the first one could not.
    ch = _Recorder(ok=True)
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])
    retry_due(db, {}, now=now + timedelta(seconds=1))
    assert ch.sent == BATCH_LIMIT
    remaining = db.scalar(
        select(func.count()).select_from(m.NotificationDelivery)
        .where(m.NotificationDelivery.channel_key == UNROUTED_CHANNEL_KEY))
    assert remaining == 50


def test_resolved_rows_close_out_in_one_statement_not_one_per_row(db, monkeypatch):
    """The resolved-alert check was ``db.get(Alert)`` per delivery. It also has
    to keep working for rows whose channel is disabled -- otherwise they
    accumulate forever on a system that never gets a channel back -- which is
    why it is its own pass over every due row rather than a filter on the
    sendable batch."""
    alert = m.Alert(type=m.AlertConditionType.supply_below,
                    severity=m.EventSeverity.warning, state=m.AlertState.resolved,
                    title="cleared", dedupe_key="r:1")
    db.add(alert)
    db.flush()
    now = datetime.now(timezone.utc)
    _deliveries(db, 50, "Slack", due_at=now - timedelta(minutes=1),
                alert_id=alert.id)
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [])

    with _Counter() as c:
        res = retry_due(db, {}, now=now)
    assert res["deliveries_dead"] == 50
    assert res["deliveries_skipped"] == 0
    assert db.scalar(
        select(func.count()).select_from(m.NotificationDelivery)
        .where(m.NotificationDelivery.status == m.DeliveryStatus.dead)) == 50
    # Five statements-ish, not fifty: the closure is a select + an update.
    assert len(c) <= 8, [s.split("\n")[0] for s in c.statements]
