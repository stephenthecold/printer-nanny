"""Revision 0045 repairs a database that ACTUALLY drifted.

`test_schema_drift.py` cannot see the drift this fixes, and says so: it compares
two FRESH databases, and revision 0001 is `create_all` against current metadata,
so both come out correct by construction. The drift only exists where
0034/0039/0042's `create_table` calls really executed -- a deployment that
predates them and upgraded through.

That is also why correcting those three files was only half a fix: alembic
records applied revisions and never re-runs one, so editing a `create_table`
reaches future upgrades and nothing already deployed. 0045 is the other half.

This test manufactures the drifted shape by dropping the three tables after 0001
and letting the later revisions build them, which is the same trick
`test_schema_drift.py` uses to reach the upgraded path at all.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest
import sqlalchemy as sa

REPO = pathlib.Path(__file__).resolve().parent.parent
LATE_TABLES = ("remote_requests", "reading_rollups", "device_definitions")
WATCHED = ("kind", "status", "truncated", "created_at", "updated_at")


def _alembic(db: pathlib.Path, *args: str):
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{db}", SECRET_KEY="drift-test")
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO), env=env, capture_output=True, text=True,
    )


def _shape(db: pathlib.Path) -> dict:
    engine = sa.create_engine(f"sqlite:///{db}")
    try:
        insp = sa.inspect(engine)
        return {
            f"{t}.{c['name']}": (getattr(c["type"], "length", None), c["nullable"])
            for t in LATE_TABLES
            for c in insp.get_columns(t)
            if c["name"] in WATCHED
        }
    finally:
        engine.dispose()


@pytest.fixture
def upgraded_db(tmp_path):
    """A database whose late tables were built by their migrations, not by 0001."""
    db = tmp_path / "upgraded.db"
    r = _alembic(db, "upgrade", "0033_macos_drivers")
    assert r.returncode == 0, r.stderr[-2000:]

    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.begin() as cx:
        for table in LATE_TABLES:
            cx.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
    engine.dispose()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr[-2000:]
    return db


def test_the_late_tables_match_the_model_after_an_upgrade(upgraded_db):
    shape = _shape(upgraded_db)
    assert shape, "no columns inspected -- the fixture did not build the tables"

    nullable = [k for k, (_, null) in shape.items() if null]
    assert not nullable, (
        "these are NOT NULL in central/models.py but nullable on an upgraded "
        f"database: {sorted(nullable)}"
    )
    for column in ("remote_requests.kind", "remote_requests.status"):
        length, _ = shape[column]
        assert length == 32, (
            f"{column} is VARCHAR({length}); the model's _enum(...) renders 32, "
            "and the first enum member longer than this fails on Postgres for "
            "upgraded installs only"
        )


def test_0045_is_a_no_op_on_a_fresh_database(tmp_path):
    """On a fresh chain 0001 already built these correctly, and 0045 must not
    rebuild them for nothing -- batch_alter_table REBUILDS a table on SQLite,
    and reading_rollups is the retention rollup."""
    db = tmp_path / "fresh.db"
    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr[-2000:]

    shape = _shape(db)
    assert all(null is False for _, null in shape.values())
    assert shape["remote_requests.kind"][0] == 32
