"""The paths that only exist on Postgres -- i.e. the ones that only run in production.

Everything here was previously covered by nothing at all. The suite ran on
SQLite, where the BRIN index is a no-op, ``pg_try_advisory_lock`` is a no-op,
``pg_dump`` is never invoked, and Alembic runs under ``render_as_batch``. Each of
those is a real production code path whose first execution was on a customer's
server.

That blindness had already shipped two defects, both found by writing this file:

* ``Base.metadata.create_all()`` -- which **is** migration 0001 -- could not
  build a schema on Postgres at all, because two Boolean columns carried an
  integer ``server_default``. SQLite's loose typing accepted it; Postgres
  refuses outright. ``alembic upgrade head`` is what ``docker-compose.yml`` runs
  at api start, so no *fresh* deployment could be created.
* The backup path handed ``pg_dump`` a SQLAlchemy URL (``postgresql+psycopg://``)
  that libpq reads as a bare database name, so it silently tried a local socket
  and left a zero-byte dump. DB backup and restore had never worked on a real
  deployment.

Run with ``PN_TEST_DATABASE_URL=postgresql+psycopg://…`` (see tests/conftest.py).
Tests needing a server carry ``@pytest.mark.postgres_only`` and skip without one;
the ones that need no server are deliberately left unmarked so they guard the
same rules on every contributor's SQLite run.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Boolean, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from central import models as m
from central.config import settings
from central.dashboard import backup_routes as br
from central.db import WORKER_CYCLE_LOCK_KEY, Base, engine, try_leader_lock
from central.worker import run as worker_run
from tests.conftest import POSTGRES

_REPO_ROOT = Path(__file__).resolve().parent.parent

# pg_try_advisory_lock(bigint) splits its key across pg_locks: the high 32 bits
# land in classid, the low 32 in objid.
_LOCK_HIGH = (WORKER_CYCLE_LOCK_KEY >> 32) & 0xFFFFFFFF
_LOCK_LOW = WORKER_CYCLE_LOCK_KEY & 0xFFFFFFFF

_HAS_PG_DUMP = all(
    __import__("shutil").which(tool) for tool in ("pg_dump", "pg_restore")
)


# --------------------------------------------------------------------------- #
# Throwaway databases
# --------------------------------------------------------------------------- #
@contextmanager
def _throwaway_database(tag: str):
    """A brand-new, empty database, dropped on the way out.

    Migrations and restores need a database they own outright: `alembic upgrade
    head` builds the schema itself, and `pg_restore --clean` drops and recreates
    every object. Running either against the suite's own database would fight
    the session-scoped schema the ``db`` fixture depends on.
    """
    base = make_url(settings.database_url)
    name = f"pn_d2_{tag}_{os.getpid()}"  # literal + pid: no interpolation of input
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield base.set(database=name)
    finally:
        with admin.connect() as conn:
            # WITH (FORCE) so a connection this test forgot to dispose cannot
            # strand the database and break the next run.
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _alembic(url, *args: str) -> subprocess.CompletedProcess:
    """Run the real alembic CLI, as an operator would, against ``url``.

    A subprocess rather than an in-process call: ``central.config.settings`` is
    an lru_cached singleton bound at import, and ``central.db.engine`` binds to
    it, so an in-process run would migrate the suite's own database no matter
    what this passed. PYTHONPATH pins ``central`` to *this* checkout -- the
    editable install otherwise resolves it elsewhere from a worktree.
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = url.render_as_string(hide_password=False)
    env["SECRET_KEY"] = "test-secret-for-migrations"
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(_REPO_ROOT), env=env, capture_output=True, text=True,
    )


def _indexes_on(url, table: str) -> dict:
    """``{index_name: access_method}`` for a table -- 'brin' vs 'btree' is the
    whole point, and ``get_indexes()`` does not report the access method."""
    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(
                "SELECT i.relname, am.amname FROM pg_class i "
                "JOIN pg_index ix ON ix.indexrelid = i.oid "
                "JOIN pg_class t ON t.oid = ix.indrelid "
                "JOIN pg_am am ON am.oid = i.relam "
                "WHERE t.relname = :t"
            ), {"t": table}).all()
        return {name: am for name, am in rows}
    finally:
        eng.dispose()


# --------------------------------------------------------------------------- #
# Portable guards -- no server needed, so they run on everyone's SQLite too
# --------------------------------------------------------------------------- #
def test_no_boolean_column_carries_a_non_boolean_server_default():
    """A Boolean column's server_default must render as a boolean on Postgres.

    This is the SQLite-side guard for the defect that motivated this file.
    ``server_default="1"`` on a Boolean is accepted by SQLite and refused by
    Postgres ("column is of type boolean but default expression is of type
    integer"), and because migration 0001 is ``Base.metadata.create_all()`` that
    refusal breaks `alembic upgrade head` on every fresh install. The whole
    suite stayed green through it.

    ``sqlalchemy.true()`` / ``false()`` render per dialect and are the correct
    spelling; a bare ``"1"`` or ``text("1")`` is not.
    """
    from sqlalchemy.dialects import postgresql

    dialect = postgresql.dialect()
    offenders = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if not isinstance(col.type, Boolean) or col.server_default is None:
                continue
            rendered = str(col.server_default.arg.compile(dialect=dialect)).strip()
            if rendered.lower().strip("'\"") not in {"true", "false"}:
                offenders.append(f"{table.name}.{col.name} -> {rendered!r}")
    assert offenders == [], (
        "Boolean columns with a non-boolean server_default; Postgres refuses "
        "these at CREATE TABLE, which is migration 0001. Use sqlalchemy.true()/"
        "false(): " + ", ".join(offenders)
    )


def test_libpq_target_drops_the_driver_suffix_and_keeps_the_password_out_of_argv():
    """The regression test for a backup that never worked.

    libpq only treats a conninfo string as a URI when it starts exactly
    ``postgresql://`` or ``postgres://``. ``postgresql+psycopg://…`` contains no
    ``=``, so libpq reads the entire string as a *database name* and falls back
    to a local socket -- pg_dump exits 1 and leaves a zero-byte file. Separately,
    an argv element is world-readable via ``ps``, so the password must travel in
    the environment.
    """
    real = settings.database_url
    try:
        settings.database_url = (
            "postgresql+psycopg://nanny:s3cr3t@db:5432/printer_nanny?sslmode=require"
        )
        dsn, env = br.libpq_target()
    finally:
        settings.database_url = real

    assert dsn.startswith("postgresql://"), dsn
    assert "+psycopg" not in dsn
    assert "s3cr3t" not in dsn, "password must not reach argv"
    assert "nanny@db:5432/printer_nanny" in dsn
    assert "sslmode=require" in dsn, "connection options must survive"
    assert env["PGPASSWORD"] == "s3cr3t"


def test_libpq_target_omits_pgpassword_when_the_url_has_none():
    """A passwordless URL (peer/trust auth, or a .pgpass file) must not get an
    empty PGPASSWORD -- libpq treats that as an empty password, not as absent,
    and it would override .pgpass."""
    real = settings.database_url
    try:
        settings.database_url = "postgresql+psycopg://nanny@db:5432/printer_nanny"
        dsn, env = br.libpq_target()
    finally:
        settings.database_url = real
    assert "PGPASSWORD" not in env
    assert dsn == "postgresql://nanny@db:5432/printer_nanny"


# --------------------------------------------------------------------------- #
# Real Alembic runs
# --------------------------------------------------------------------------- #
@pytest.mark.postgres_only
def test_alembic_upgrades_a_fresh_database_to_head():
    """`alembic upgrade head` on an empty Postgres -- what docker-compose.yml
    runs at api start, and what no test had ever executed.

    This is the test that fails on the shipped code: revision 0001 is
    ``Base.metadata.create_all()``, and two Boolean columns with an integer
    server_default made that CREATE TABLE illegal on Postgres.
    """
    with _throwaway_database("upgrade") as url:
        proc = _alembic(url, "upgrade", "head")
        assert proc.returncode == 0, f"alembic upgrade head failed:\n{proc.stderr}"

        eng = create_engine(url)
        try:
            with eng.connect() as conn:
                tables = set(conn.execute(text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )).scalars())
                # Not merely "it didn't raise": the schema is actually there,
                # and the boolean default carries the right *semantics*.
                assert {"printers", "readings", "users", "suppression_windows"} <= tables
                for table, column in (("users", "active"),
                                      ("suppression_windows", "allow_breakthrough")):
                    default = conn.execute(text(
                        "SELECT column_default FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = :c"
                    ), {"t": table, "c": column}).scalar()
                    assert default == "true", f"{table}.{column} default={default!r}"
        finally:
            eng.dispose()


@pytest.mark.postgres_only
def test_alembic_round_trips_down_to_base_and_back_up():
    """Every downgrade must run on Postgres too. SQLite migrations execute under
    ``render_as_batch`` (table copy) and Postgres ones do not, so a downgrade can
    pass on SQLite and fail here -- which is the only place it ever runs."""
    with _throwaway_database("roundtrip") as url:
        up = _alembic(url, "upgrade", "head")
        assert up.returncode == 0, up.stderr
        down = _alembic(url, "downgrade", "base")
        assert down.returncode == 0, f"alembic downgrade base failed:\n{down.stderr}"

        eng = create_engine(url)
        try:
            with eng.connect() as conn:
                left = set(conn.execute(text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )).scalars())
        finally:
            eng.dispose()
        # alembic_version is alembic's own bookkeeping and survives by design.
        assert left <= {"alembic_version"}, f"downgrade left tables behind: {left}"

        again = _alembic(url, "upgrade", "head")
        assert again.returncode == 0, f"re-upgrade after downgrade failed:\n{again.stderr}"


@pytest.mark.postgres_only
def test_orm_metadata_builds_a_schema_on_postgres():
    """``create_all`` is the seed path AND migration 0001. It must work here."""
    with _throwaway_database("createall") as url:
        eng = create_engine(url)
        try:
            Base.metadata.create_all(bind=eng)
            with eng.connect() as conn:
                count = conn.execute(text(
                    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'"
                )).scalar()
            assert count == len(Base.metadata.sorted_tables)
        finally:
            eng.dispose()


# --------------------------------------------------------------------------- #
# BRIN index (migration 0002)
# --------------------------------------------------------------------------- #
@pytest.mark.postgres_only
def test_migration_0002_creates_a_real_brin_index_on_readings_ts():
    """Not "an index called brin" -- an index whose access method IS brin.

    The migration body is ``CREATE INDEX … USING brin (ts)`` guarded by a
    dialect check, so on SQLite it is a no-op and the assertion is unavailable.
    Asserting on the *name* would pass against a btree that happened to be
    named ix_readings_ts_brin, which is exactly the "reading a label rather
    than the transport" mistake this codebase has paid for before.
    """
    with _throwaway_database("brin") as url:
        assert _alembic(url, "upgrade", "head").returncode == 0
        indexes = _indexes_on(url, "readings")
        assert indexes.get("ix_readings_ts_brin") == "brin", indexes


@pytest.mark.postgres_only
def test_the_brin_index_actually_serves_a_time_range_scan():
    """Present is not the same as usable. The index exists so a "last N days"
    scan over an append-only table is cheap; prove the planner will use it for
    exactly that shape, and that the rows it returns are right.

    The btree on ``ts`` is dropped first because with both present the planner
    prefers the btree, and a plan naming *some* index would prove nothing about
    the BRIN one.
    """
    with _throwaway_database("brinplan") as url:
        assert _alembic(url, "upgrade", "head").returncode == 0
        eng = create_engine(url)
        try:
            session = sessionmaker(bind=eng, future=True)()
            client = m.Client(name="Acme")
            session.add(client)
            session.flush()
            site = m.Site(client_id=client.id, name="HQ")
            session.add(site)
            session.flush()
            printer = m.Printer(client_id=client.id, site_id=site.id, ip="10.0.0.5")
            session.add(printer)
            session.flush()

            # BRIN summarises 128-page ranges; a handful of rows lives in one
            # range and tells you nothing about a range scan.
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            session.bulk_save_objects([
                m.Reading(printer_id=printer.id, ts=start + timedelta(minutes=i),
                          page_count=i)
                for i in range(20_000)
            ])
            session.commit()
            session.close()

            with eng.connect() as conn:
                conn.execute(text("DROP INDEX ix_readings_ts"))
                conn.execute(text("ANALYZE readings"))
                conn.commit()

                window_start = start + timedelta(minutes=19_000)
                params = {"a": window_start, "b": start + timedelta(minutes=19_500)}
                plan = "\n".join(conn.execute(text(
                    "EXPLAIN SELECT count(*) FROM readings WHERE ts >= :a AND ts < :b"
                ), params).scalars())
                assert "ix_readings_ts_brin" in plan, plan
                assert "Bitmap" in plan, plan  # BRIN is only reachable via bitmap scans

                # And the answer is correct, not merely fast.
                assert conn.execute(text(
                    "SELECT count(*) FROM readings WHERE ts >= :a AND ts < :b"
                ), params).scalar() == 500
        finally:
            eng.dispose()


@pytest.mark.postgres_only
def test_downgrading_past_0002_removes_the_brin_index():
    """The migration's downgrade half runs only on Postgres, so it has never
    executed anywhere."""
    with _throwaway_database("brindown") as url:
        assert _alembic(url, "upgrade", "head").returncode == 0
        assert _indexes_on(url, "readings").get("ix_readings_ts_brin") == "brin"
        down = _alembic(url, "downgrade", "0001_baseline")
        assert down.returncode == 0, down.stderr
        assert "ix_readings_ts_brin" not in _indexes_on(url, "readings")


# --------------------------------------------------------------------------- #
# Advisory-lock leader election
# --------------------------------------------------------------------------- #
def _advisory_lock_count(conn) -> int:
    return conn.execute(text(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
        "AND classid = :hi AND objid = :lo"
    ), {"hi": _LOCK_HIGH, "lo": _LOCK_LOW}).scalar()


@pytest.mark.postgres_only
def test_a_second_session_is_refused_the_leader_lock(db):
    """The whole point of the lock: two worker containers, one cycle.

    Each ``try_leader_lock`` takes its own connection from the pool, so these
    are two genuinely separate Postgres sessions -- which is what
    ``pg_try_advisory_lock`` discriminates on. On SQLite this helper is a no-op
    that always yields True, so this contract has never once been asserted.
    """
    with try_leader_lock() as first:
        assert first is True
        with try_leader_lock() as second:
            assert second is False, "a second worker acquired the leader lock"


@pytest.mark.postgres_only
def test_the_lock_is_visible_while_held_and_gone_after(db):
    """Held means held in Postgres, not merely 'the helper returned True'."""
    with engine.connect() as probe:
        assert _advisory_lock_count(probe) == 0, "a previous test leaked the lock"
        with try_leader_lock() as acquired:
            assert acquired is True
            assert _advisory_lock_count(probe) == 1
        assert _advisory_lock_count(probe) == 0, "lock not released on exit"


@pytest.mark.postgres_only
def test_an_exception_inside_the_block_still_releases_the_lock(db):
    """A cycle that raises must not wedge every other worker forever. The
    SQLite version of this test proves nothing -- there is no lock to leak."""
    with pytest.raises(RuntimeError):
        with try_leader_lock() as acquired:
            assert acquired is True
            raise RuntimeError("boom")
    with engine.connect() as probe:
        assert _advisory_lock_count(probe) == 0
    with try_leader_lock() as acquired:
        assert acquired is True


@pytest.mark.postgres_only
def test_run_cycle_skips_when_another_process_holds_the_lock(db):
    """End-to-end leader election with nothing stubbed.

    The existing SQLite test monkeypatches ``try_leader_lock`` to return False,
    so it asserts run_cycle's branch and not the lock. Here a separate session
    really holds it and run_cycle really loses the race.
    """
    holder = engine.connect()
    try:
        assert holder.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": WORKER_CYCLE_LOCK_KEY}
        ).scalar() is True

        assert worker_run.run_cycle() == {"skipped": "not_leader"}
    finally:
        # Explicit release: the connection goes back to the pool, and an
        # advisory lock is session-scoped -- a leak here would silently make
        # every later worker test skip its cycle.
        assert holder.execute(
            text("SELECT pg_advisory_unlock(:k)"), {"k": WORKER_CYCLE_LOCK_KEY}
        ).scalar() is True
        holder.close()

    # And with the lock free, the cycle runs for real.
    out = worker_run.run_cycle()
    assert "alerts_opened" in out
    assert out.get("skipped") is None


# --------------------------------------------------------------------------- #
# pg_dump / pg_restore
# --------------------------------------------------------------------------- #
@pytest.mark.postgres_only
@pytest.mark.skipif(not _HAS_PG_DUMP, reason="pg_dump/pg_restore not on PATH")
def test_pg_dump_and_pg_restore_round_trip_real_data():
    """The backup feature, executed rather than stubbed.

    ``tests/test_backup_routes.py`` monkeypatches both subprocess seams, which
    is why a dump that produced zero bytes on every real deployment stayed
    green. This runs the actual binaries against an actual server and checks
    that data destroyed after the dump comes back.
    """
    with _throwaway_database("dump") as url:
        eng = create_engine(url)
        Base.metadata.create_all(bind=eng)
        session = sessionmaker(bind=eng, future=True)()
        session.add(m.Client(name="Backup Canary", notes="must survive the round trip"))
        session.commit()
        session.close()
        eng.dispose()  # pg_restore --clean drops objects; no lock-holders allowed

        real = settings.database_url
        fd, tmp = tempfile.mkstemp(prefix="pn-d2-", suffix=".dump")
        os.close(fd)
        dump = Path(tmp)
        try:
            settings.database_url = url.render_as_string(hide_password=False)

            br._pg_dump_to_file(dump)
            assert dump.stat().st_size > 0, "pg_dump produced a zero-byte file"
            # Custom format, i.e. the thing pg_restore can read.
            assert dump.read_bytes()[:5] == b"PGDMP"

            eng = create_engine(url)
            with eng.connect() as conn:
                conn.execute(text("DELETE FROM clients"))
                conn.commit()
                assert conn.execute(text("SELECT count(*) FROM clients")).scalar() == 0
            eng.dispose()

            br._pg_restore_from_file(dump)

            eng = create_engine(url)
            try:
                with eng.connect() as conn:
                    names = list(conn.execute(text("SELECT name FROM clients")).scalars())
                assert names == ["Backup Canary"]
            finally:
                eng.dispose()
        finally:
            settings.database_url = real
            dump.unlink(missing_ok=True)


@pytest.mark.postgres_only
@pytest.mark.skipif(not _HAS_PG_DUMP, reason="pg_dump/pg_restore not on PATH")
def test_pg_dump_fails_loudly_on_an_unreachable_database():
    """A backup that cannot connect must raise, not hand back an empty file the
    operator downloads and files away as their disaster-recovery copy."""
    real = settings.database_url
    fd, tmp = tempfile.mkstemp(prefix="pn-d2-bad-", suffix=".dump")
    os.close(fd)
    dump = Path(tmp)
    try:
        bad = make_url(settings.database_url).set(port=1, database="nope")
        settings.database_url = bad.render_as_string(hide_password=False)
        with pytest.raises(RuntimeError, match="pg_dump failed"):
            br._pg_dump_to_file(dump)
    finally:
        settings.database_url = real
        dump.unlink(missing_ok=True)


def test_the_postgres_only_marker_is_not_silently_skipping_everything():
    """A guard against this whole file quietly becoming a no-op.

    Every test above that matters is skipped without a server. If CI ever stops
    setting PN_TEST_DATABASE_URL, the Postgres job would go green while
    exercising nothing -- the failure mode this item exists to end. On a
    Postgres run this asserts the marker really did select in.
    """
    if POSTGRES:
        assert settings.is_production, "PN_TEST_DATABASE_URL set but not in PG mode"
        assert not settings.is_sqlite
    else:
        pytest.skip("SQLite run: the postgres_only tests above are skipped by design")
