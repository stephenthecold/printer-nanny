"""The schema check, and the race it exists to close.

The incident, 2026-08-05: `docker compose up -d` starts api and worker in
parallel, only the api runs `alembic upgrade head`, and on a database with 2.4M
readings fifteen revisions took long enough that the worker's first cycle ran
against a half-built schema -- seven jobs dead with UndefinedColumn /
UndefinedTable, every later cycle clean.

The test that matters most here is
``test_a_stamp_at_head_does_not_mean_the_schema_is_there``, because the first
diagnosis of that incident was that ``alembic_version`` was lying, and a check
built on the version would have agreed with it -- and would equally have passed
during the real race, since the version is already moving while the schema is
being built. Only the columns can be asked.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from central import schema_check
from central.db import Base, engine


@pytest.fixture(autouse=True)
def _rebuild_schema_after_ddl():
    """Put the schema back, because these tests damage it on purpose.

    The shared ``db`` fixture truncates on Postgres and rebuilds only when the
    truncate RAISES -- which a dropped *table* causes and a dropped *column*
    does not. So without this a column dropped here stays dropped for the rest
    of the session, and the resulting failures are attributed to whichever
    tests happen to run next. That is the same "626 errors, all attributed to
    the wrong tests" trap conftest already documents for tables; columns fall
    through it.
    """
    yield
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _drop_column(db, table: str, column: str) -> None:
    """Remove a column on whichever backend the suite is running against.

    Pick columns that carry no foreign key: SQLite refuses to drop one that
    appears in an FK definition ("unknown column ... in foreign key definition"),
    so ``subnets.collector_agent_id`` -- the obvious choice, being one of the
    columns the real incident was missing -- cannot be used here.
    """
    db.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
    db.commit()


def _drop_table(db, table: str) -> None:
    db.execute(text(f"DROP TABLE {table}"))
    db.commit()


# --------------------------------------------------------------------------- #
# What it sees
# --------------------------------------------------------------------------- #
def test_a_complete_schema_is_reported_clean(db):
    drift = schema_check.inspect_schema(db.get_bind())
    assert drift.ok, drift.describe()
    assert "schema OK" in drift.describe()


def test_a_missing_column_is_named(db):
    _drop_column(db, "printers", "remote_capability")
    drift = schema_check.inspect_schema(db.get_bind())

    assert not drift.ok
    assert ("printers", "remote_capability") in drift.missing_columns
    assert "printers.remote_capability" in drift.describe()


def test_a_missing_table_is_named(db):
    _drop_table(db, "login_attempts")
    drift = schema_check.inspect_schema(db.get_bind())

    assert not drift.ok
    assert "login_attempts" in drift.missing_tables
    assert "login_attempts" in drift.describe()


def test_an_extra_column_is_not_drift(db):
    """Older code against a newer database. It breaks nothing, so it must not
    read as a broken install -- a check that cries wolf during a rollback is one
    an operator learns to ignore."""
    db.execute(text("ALTER TABLE subnets ADD COLUMN a_future_column INTEGER"))
    db.commit()

    assert schema_check.inspect_schema(db.get_bind()).ok


# --------------------------------------------------------------------------- #
# The distinction the first diagnosis got wrong
# --------------------------------------------------------------------------- #
def test_a_stamp_at_head_does_not_mean_the_schema_is_there(db):
    """`alembic_version` records which revisions RAN. That is a different
    question from what the schema HAS, and the difference is the whole reason
    this module introspects columns.

    Both readings of the incident hinge on it: mid-migration the version is
    already moving while the columns are still arriving, and after the fact the
    version cannot say what was true two minutes ago. A version check would pass
    in both cases -- and the schema check fails, correctly, in the first.
    """
    bind = db.get_bind()
    db.execute(text("CREATE TABLE IF NOT EXISTS alembic_version "
                    "(version_num VARCHAR(32) NOT NULL)"))
    db.execute(text("DELETE FROM alembic_version"))
    db.execute(text("INSERT INTO alembic_version (version_num) "
                    "VALUES ('0045_align_drifted_columns')"))
    _drop_column(db, "alert_rules", "window_minutes")

    drift = schema_check.inspect_schema(bind)
    assert not drift.ok, "the stamp said head; the column was gone. Ask the columns."
    assert ("alert_rules", "window_minutes") in drift.missing_columns


# --------------------------------------------------------------------------- #
# How it behaves for the callers
# --------------------------------------------------------------------------- #
def test_check_never_raises_on_an_unusable_bind():
    """`check` returning None means "could not check", which is deliberately
    NOT the same answer as "clean" -- an unreachable database is not a migrated
    one, and install.sh keys on the difference."""
    class Exploding:
        def __getattr__(self, name):
            raise RuntimeError("no database here")

    assert schema_check.check(Exploding()) is None


def test_waiting_returns_immediately_when_the_schema_is_complete(db):
    drift = schema_check.wait_for_schema(timeout=30, bind=db.get_bind())
    assert drift is not None and drift.ok


def test_waiting_gives_up_rather_than_blocking_forever(db, caplog):
    """An install whose operator genuinely never migrated must still come up:
    the dashboard is how they fix it. So the wait is bounded and the worker
    proceeds, loudly."""
    _drop_column(db, "printers", "remote_capability")

    with caplog.at_level(logging.ERROR):
        drift = schema_check.wait_for_schema(
            timeout=0.3, interval=0.05, bind=db.get_bind(), where="worker"
        )

    assert drift is not None and not drift.ok
    assert ("printers", "remote_capability") in drift.missing_columns
    assert any("still incomplete" in r.getMessage() for r in caplog.records)


def test_the_wait_ends_as_soon_as_the_schema_completes(db, monkeypatch):
    """The point of waiting is to stop racing, not to sleep a fixed time: a
    worker that waited the full timeout on a healthy stack would delay every
    restart by minutes."""
    calls = {"n": 0}
    real = schema_check.inspect_schema
    incomplete = schema_check.SchemaDrift([], [("subnets", "collector_agent_id")])

    def flaky(bind=None):
        calls["n"] += 1
        return incomplete if calls["n"] < 3 else real(db.get_bind())

    monkeypatch.setattr(schema_check, "inspect_schema", flaky)
    drift = schema_check.wait_for_schema(timeout=30, interval=0.01, where="worker")

    assert drift is not None and drift.ok
    assert calls["n"] == 3, "should have stopped on the first complete result"


# --------------------------------------------------------------------------- #
# The CLI install.sh keys on
# --------------------------------------------------------------------------- #
def test_cli_exit_codes_distinguish_clean_drifted_and_unknown(db, monkeypatch):
    """install.sh reads these. 2 must never be treated as success -- a database
    we cannot reach is not a migrated one."""
    monkeypatch.setattr(
        schema_check, "inspect_schema", lambda bind=None: schema_check.SchemaDrift()
    )
    assert schema_check.main([]) == 0

    monkeypatch.setattr(
        schema_check,
        "inspect_schema",
        lambda bind=None: schema_check.SchemaDrift(["event_deliveries"], []),
    )
    assert schema_check.main([]) == 1

    def boom(bind=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(schema_check, "inspect_schema", boom)
    assert schema_check.main([]) == 2


def test_every_model_table_is_covered(db):
    """The check is only worth anything if it looks at all of them -- a
    hand-listed subset would silently stop covering whatever is added next."""
    drift = schema_check.inspect_schema(db.get_bind())
    assert drift.ok
    assert len(Base.metadata.tables) > 30, "metadata did not load"


@pytest.mark.parametrize("where", ["worker", "api"])
def test_the_reporting_names_which_process_saw_it(db, caplog, where):
    """Two processes check, and they fail for different reasons -- the worker
    races the migrations, the api ran them. A log line that does not say which
    one is speaking sends the reader to the wrong container."""
    _drop_column(db, "alert_rules", "window_minutes")
    with caplog.at_level(logging.WARNING):
        schema_check.wait_for_schema(
            timeout=0.2, interval=0.05, bind=db.get_bind(), where=where
        )
    assert any(where in r.getMessage() for r in caplog.records)
