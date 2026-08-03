"""A downgrade must not silently discard an operator's credentials.

Revisions 0003, 0007, 0012, 0014 and 0019 guard their ``drop_table`` with
``has_table``/``_table_exists``, and those guards are real. They check
*existence*, though, and revision 0001 is ``Base.metadata.create_all()`` -- so on
every install the table exists because the baseline made it, the guard passes,
and the drop goes ahead. Measured on Postgres 16 before ``migrations/guard.py``
existed: ``alembic downgrade 0002_readings_brin`` dropped ``app_settings``,
``audit_log`` and ``app_assets``, and the next ``alembic upgrade head`` brought
them back empty. ``app_settings`` is where the SMTP password, OAuth tokens,
FreeScout key, Slack/Teams/webhook URLs and OIDC client secret live; Fernet
encryption at rest does nothing about ``DROP TABLE``.

These tests run the real ``alembic`` CLI in a subprocess against throwaway SQLite
databases, because the behaviour under test *is* the command-line behaviour --
and because ``migrations/env.py`` reads its URL from the already-imported
settings, so an in-process run would migrate the suite's own database.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import shutil
import subprocess
import sys

import pytest
import sqlalchemy as sa

from migrations.guard import (
    OVERRIDE_ENV,
    DestructiveDowngrade,
    install_drop_table_guard,
    override_enabled,
    refuse_if_populated,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The revision the destructive experiment downgrades to. Chosen because 0003 is
#: the revision that drops app_settings, which is the table that matters.
BEFORE_SETTINGS = "0002_readings_brin"


def _alembic(db: pathlib.Path, *args: str, override: bool = False):
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db}"
    env["SECRET_KEY"] = "downgrade-guard-test"
    env.pop(OVERRIDE_ENV, None)
    if override:
        env[OVERRIDE_ENV] = "1"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )


@pytest.fixture(scope="session")
def chain_template(tmp_path_factory):
    """One migrated SQLite database, built once and copied per test.

    ``alembic upgrade head`` is ~0.5s; copying the resulting file is free. Each
    test gets its own copy because several of them deliberately destroy it.
    """
    db = tmp_path_factory.mktemp("dg-guard") / "template.sqlite3"
    proc = _alembic(db, "upgrade", "head")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return db


@pytest.fixture()
def empty_db(chain_template, tmp_path):
    db = tmp_path / "chain.sqlite3"
    shutil.copy(chain_template, db)
    return db


@pytest.fixture()
def populated_db(empty_db):
    """The same database with one operator secret in ``app_settings``."""
    engine = sa.create_engine(f"sqlite:///{empty_db}", poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO app_settings (key, value, updated_at) "
                    "VALUES (:k, :v, :t)"
                ),
                {
                    "k": "notify.smtp_password",
                    "v": '"enc:v1:pretend-this-is-a-real-credential"',
                    "t": dt.datetime(2026, 8, 3, 0, 0, 0),
                },
            )
    finally:
        engine.dispose()
    return empty_db


def _revision(db: pathlib.Path):
    engine = sa.create_engine(f"sqlite:///{db}", poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


def _tables(db: pathlib.Path):
    engine = sa.create_engine(f"sqlite:///{db}", poolclass=sa.pool.NullPool)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_an_empty_database_downgrades_freely(empty_db):
    """The CI contract: ``upgrade head`` then ``downgrade base`` must still pass.

    Both ci.yml and postgres.yml run exactly that on a freshly migrated scratch
    database. Dropping an empty table destroys nothing, so the guard has nothing
    to say and neither workflow needs an opt-in it would then carry forever.
    """
    proc = _alembic(empty_db, "downgrade", "base")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "app_settings" not in _tables(empty_db)


def test_a_populated_database_refuses_to_downgrade(populated_db):
    proc = _alembic(populated_db, "downgrade", BEFORE_SETTINGS)
    assert proc.returncode != 0, "the downgrade was allowed to destroy app_settings"
    assert "Refusing to downgrade" in proc.stderr
    assert "app_settings" in proc.stderr
    assert OVERRIDE_ENV in proc.stderr, "the refusal must name the way forward"


def test_a_refused_downgrade_leaves_the_database_exactly_as_it_was(populated_db):
    """The pre-flight's whole reason for existing, asserted rather than assumed.

    A guard that only fires at the ``drop_table`` refuses *partway through*, and
    SQLite does not roll that back -- pysqlite commits implicitly around DDL.
    Measured with the per-drop guard alone: a refused downgrade came to rest at
    0003 with ``audit_log`` already gone and revision 0040's columns already
    dropped. Checking the whole database once, before the first step runs, is
    what makes "nothing has been changed" true here as well as on Postgres.

    So this test is not a restatement of the one above: it is the only thing
    that distinguishes the pre-flight from the backstop, and it fails if the
    pre-flight ever stops installing.
    """
    before_revision = _revision(populated_db)
    before_tables = _tables(populated_db)

    proc = _alembic(populated_db, "downgrade", BEFORE_SETTINGS)
    assert proc.returncode != 0

    assert _revision(populated_db) == before_revision, (
        "the database moved -- the refusal happened partway through a downgrade "
        "instead of before it started"
    )
    assert _tables(populated_db) == before_tables
    engine = sa.create_engine(f"sqlite:///{populated_db}", poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT count(*) FROM app_settings")).scalar()
    finally:
        engine.dispose()
    assert rows == 1, "the secret this guard exists for did not survive"


def test_the_override_permits_the_downgrade(populated_db):
    """An operator who means it must not be blocked.

    The intent is to turn silent destruction into a decision, not to forbid the
    operation -- a guard with no way past it gets worked around, and the
    workaround is never as careful.
    """
    proc = _alembic(populated_db, "downgrade", BEFORE_SETTINGS, override=True)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "app_settings" not in _tables(populated_db)


def test_upgrading_is_never_blocked(populated_db):
    """Only downgrades are guarded. An upgrade on a populated database is normal."""
    assert _alembic(populated_db, "downgrade", BEFORE_SETTINGS, override=True).returncode == 0
    proc = _alembic(populated_db, "upgrade", "head")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "app_settings" in _tables(populated_db)


def test_the_drop_table_backstop_refuses_a_populated_table(populated_db):
    """The layer that survives an alembic that changes shape.

    The pre-flight reads the migration plan out of the migration context, which
    is not public API. If a future alembic moves it the pre-flight degrades to a
    no-op -- so ``op.drop_table`` itself is wrapped as well, and every revision's
    drop goes through it whether or not its author knew.
    """
    install_drop_table_guard()
    from alembic.operations import Operations

    assert getattr(Operations.drop_table, "_pn_guarded", False), (
        "op.drop_table is not wrapped -- the backstop is not installed"
    )

    engine = sa.create_engine(f"sqlite:///{populated_db}", poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            with pytest.raises(DestructiveDowngrade) as caught:
                refuse_if_populated(conn, "app_settings")
    finally:
        engine.dispose()
    assert "app_settings" in str(caught.value)


def test_installing_the_backstop_twice_wraps_once():
    """``env.py`` is re-executed on every alembic command."""
    from alembic.operations import Operations

    install_drop_table_guard()
    once = Operations.drop_table
    install_drop_table_guard()
    assert Operations.drop_table is once


def test_empty_and_missing_tables_are_not_evidence_of_data(empty_db):
    """A guard that fires on nothing blocks every legitimate downgrade."""
    engine = sa.create_engine(f"sqlite:///{empty_db}", poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            refuse_if_populated(conn, "app_settings", "audit_log")
            refuse_if_populated(conn, "no_such_table_anywhere")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("YES", True), ("on", True),
     ("", False), ("0", False), ("false", False), ("maybe", False)],
)
def test_the_override_is_explicit(monkeypatch, value, expected):
    """A typo must fail closed -- an unrecognised value is not consent."""
    monkeypatch.setenv(OVERRIDE_ENV, value)
    assert override_enabled() is expected
