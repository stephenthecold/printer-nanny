"""Does ``alembic upgrade head`` build the schema the models describe?

Until this file existed the question could not be asked. Most tests build their
schema with ``Base.metadata.create_all()``, the migration chain was exercised
only by a CI step that checked it did not *crash*, and nothing anywhere compared
the two results. Two defects shipped through that gap:

* a ``server_default=text("1")`` on a Boolean that SQLite accepted and Postgres
  rejected, so ``alembic upgrade head`` failed on every fresh Postgres install;
* two revisions creating foreign keys that ``create_all`` had already made under
  SQLAlchemy's own names, leaving a fresh Postgres schema carrying two identical
  constraints per column.

**Read this before trusting a green run.** Revision 0001 is
``Base.metadata.create_all()`` against *current* metadata, so the chain begins by
building today's schema and revisions 0002+ run on top of it. That means a column
added to a model with **no migration at all** is invisible here: 0001 creates it,
the comparison finds it, and the check passes by construction. What this file
does detect is the other direction -- anything the *migrations* do that the
models do not describe -- which is exactly where both shipped defects lived, and
which is the only direction a create_all baseline can be honest about. Closing
the remaining half needs 0001 frozen to explicit DDL so it stops tracking the
models; that is a deliberate decision nobody has taken, not an oversight here.

**Two checks, because one of them provably misses the bug that shipped.**
``compare_metadata`` keys connection-side indexes and unique constraints by
*name* (``conn_indexes_by_name``, ``conn_uniques_by_name``), so a duplicate under
a new name is reported. It keys foreign keys by *signature*
(``conn_fks_by_sig = {c.unnamed: c for c in conn_fks_sig}``) -- a dict, so two
constraints with the same definition collapse into one entry and the redundant
one is discarded before any comparison happens. Verified by reintroducing the
original bug: with three duplicate FK pairs sitting in a real Postgres database,
``compare_metadata`` returned nothing at all. ``test_*_has_no_redundant_foreign_keys``
is what covers that, and it is not optional garnish -- it is the half of this
file that would have caught the defect that actually happened.

Both dialects are checked. SQLite migrates under ``render_as_batch``
(rebuild-the-table) while Postgres uses real ``ALTER``, so a revision can be
correct on one and wrong on the other; and the BRIN index, the partial unique
indexes and every server default only exist on Postgres at all. The SQLite half
runs unconditionally so a bare ``pytest`` still asks the question; the Postgres
half runs when ``PN_TEST_DATABASE_URL`` is set, which is what
``.github/workflows/postgres.yml`` does.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

import central.models  # noqa: F401  (register every table on Base.metadata)
from central.db import Base

from tests.conftest import POSTGRES

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Differences that are correct and must stay. Keyed on (dialect, op, name) so an
#: entry cannot silently widen to cover something it was never meant to, and
#: asserted to still be *produced* by ``test_every_known_difference_is_still_real``
#: -- an allowlist nobody re-checks is how a regression gets adopted as expected.
KNOWN_DIFFERENCES = {
    ("postgresql", "remove_index", "ix_readings_ts_brin"): (
        "Revision 0002 creates a BRIN index on readings.ts with raw DDL, on "
        "Postgres only. It is deliberately absent from the ORM metadata: "
        "declaring it in __table_args__ would make create_all emit a plain "
        "btree of the same name on SQLite, so the two backends would disagree "
        "about what that name means. Postgres-only, so it appears as an extra."
    ),
}


def _run_chain(url: str) -> None:
    """``alembic upgrade head`` against ``url``, in a subprocess.

    A subprocess because ``migrations/env.py`` reads the URL from the *already
    imported* ``central.config.settings`` -- in-process it would target the
    suite's own database and migrate it out from under every other test.
    """
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env.setdefault("SECRET_KEY", "schema-drift-test")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        "alembic upgrade head failed -- a fresh install cannot build its schema:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )


@pytest.fixture(scope="session")
def sqlite_chain_url():
    """A SQLite database built purely by the migration chain.

    Its own temp file, never the suite's database, and independent of whichever
    backend the suite is running on -- so the Postgres CI job checks *both*
    dialects rather than trading one for the other.
    """
    with tempfile.TemporaryDirectory(prefix="pn-drift-") as tmp:
        url = "sqlite:///" + os.path.join(tmp, "chain.sqlite3")
        _run_chain(url)
        yield url


@pytest.fixture(scope="session")
def postgres_chain_url():
    """A scratch Postgres database built purely by the migration chain.

    A separate database rather than a schema inside the suite's: with a schema,
    a search_path that failed to apply would put the chain's tables alongside
    the suite's own and the comparison would pass by comparing the schema to
    itself. A green run that proved nothing is the failure this whole file
    exists to end, so the isolation must not be able to fail open.
    """
    if not POSTGRES:
        pytest.skip("needs Postgres (set PN_TEST_DATABASE_URL)")

    from central.config import settings

    suite_url = sa.engine.make_url(settings.database_url)
    scratch = "pn_schema_drift_check"
    assert suite_url.database != scratch, "scratch name collides with the suite database"
    admin = sa.create_engine(
        suite_url, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool
    )
    try:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{scratch}"'))
            conn.execute(sa.text(f'CREATE DATABASE "{scratch}"'))
        # render_as_string(hide_password=False), never str(): URL.__str__ masks
        # the password as "***", which produces a URL that looks right in a log
        # and fails authentication.
        url = suite_url.set(database=scratch).render_as_string(hide_password=False)
        _run_chain(url)
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{scratch}"'))
        admin.dispose()


def _describe(diff) -> str:
    """One readable line per difference, so a failure names the fix."""
    if isinstance(diff, list):
        return " / ".join(_describe(d) for d in diff)
    op = diff[0]
    if op.endswith("_column") and len(diff) >= 4:
        col = diff[3]
        return f"{op}: {diff[2]}.{getattr(col, 'name', col)}"
    if len(diff) == 2:
        return f"{op}: {getattr(diff[1], 'name', diff[1])!r}"
    return f"{op}: {diff[1:]!r}"


def _key(diff, dialect: str):
    """(dialect, op, name) for allowlist lookup, or None when not allowlistable."""
    if isinstance(diff, list) or len(diff) != 2:
        return None
    return (dialect, diff[0], getattr(diff[1], "name", None))


def _raw_diffs(url: str):
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn,
                opts={
                    # Both off by default in autogenerate. A wrong column type
                    # and a wrong server default are precisely the shapes that
                    # differ between SQLite and Postgres, and one of them is the
                    # defect that broke fresh Postgres installs. Measured clean
                    # on both dialects, so the strictness costs no noise.
                    "compare_type": True,
                    "compare_server_default": True,
                },
            )
            return engine.dialect.name, compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()


def _unexpected(url: str):
    dialect, diffs = _raw_diffs(url)
    return dialect, [d for d in diffs if _key(d, dialect) not in KNOWN_DIFFERENCES]


def _redundant_foreign_keys(url: str):
    """Constraints with identical definitions under different names, per table.

    The failure ``compare_metadata`` cannot see. Signature is
    (columns, referred table, referred columns) and deliberately excludes
    ON DELETE: two constraints over the same columns are redundant whatever
    their options, and if the options *differ* the situation is worse, not
    better.
    """
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        inspector = sa.inspect(engine)
        found = []
        for table in inspector.get_table_names():
            by_signature = {}
            for fk in inspector.get_foreign_keys(table):
                signature = (
                    tuple(fk.get("constrained_columns") or ()),
                    fk.get("referred_table"),
                    tuple(fk.get("referred_columns") or ()),
                )
                by_signature.setdefault(signature, []).append(fk.get("name") or "<unnamed>")
            for signature, names in by_signature.items():
                if len(names) > 1:
                    found.append((table, signature, sorted(names)))
        return found
    finally:
        engine.dispose()


def _assert_no_drift(url: str) -> None:
    dialect, unexpected = _unexpected(url)
    assert not unexpected, (
        f"the schema `alembic upgrade head` builds on {dialect} is not the schema "
        f"the ORM models describe ({len(unexpected)} difference(s)). An existing "
        "deployment that upgrades gets the migration chain's answer, so every one "
        "of these is a real difference between a fresh install and an upgraded "
        "one:\n  " + "\n  ".join(_describe(d) for d in unexpected)
    )


def _assert_no_redundant_fks(url: str) -> None:
    redundant = _redundant_foreign_keys(url)
    assert not redundant, (
        "the migration chain left duplicate foreign keys -- revision 0001 is "
        "create_all(), so SQLAlchemy already made these under its own names and a "
        "later revision added a second copy under its own. compare_metadata "
        "cannot see this (it keys connection-side FKs by signature, so the "
        "duplicate is discarded before comparison). Check FK presence by COLUMN, "
        "not by constraint name:\n  "
        + "\n  ".join(
            f"{table}: {list(sig[0])} -> {sig[1]}{list(sig[2])} declared as {names}"
            for table, sig, names in redundant
        )
    )


def test_sqlite_chain_builds_the_schema_the_models_describe(sqlite_chain_url):
    _assert_no_drift(sqlite_chain_url)


def test_sqlite_chain_has_no_redundant_foreign_keys(sqlite_chain_url):
    _assert_no_redundant_fks(sqlite_chain_url)


@pytest.mark.postgres_only
def test_postgres_chain_builds_the_schema_the_models_describe(postgres_chain_url):
    _assert_no_drift(postgres_chain_url)


@pytest.mark.postgres_only
def test_postgres_chain_has_no_redundant_foreign_keys(postgres_chain_url):
    _assert_no_redundant_fks(postgres_chain_url)


@pytest.mark.postgres_only
def test_every_known_difference_is_still_real(postgres_chain_url):
    """An allowlist entry that no longer fires is covering something else.

    Without this, deleting revision 0002's BRIN index would go unnoticed: the
    entry that excuses it would simply stop matching, and the check would still
    pass while the index that keeps range scans cheap had quietly gone.
    """
    dialect, diffs = _raw_diffs(postgres_chain_url)
    produced = {_key(d, dialect) for d in diffs}
    stale = [
        key for key in KNOWN_DIFFERENCES
        if key[0] == dialect and key not in produced
    ]
    assert not stale, (
        "these differences are allowlisted but no longer produced -- either the "
        f"thing they excuse has gone, or the entry should be deleted: {stale}"
    )


@pytest.mark.postgres_only
def test_the_brin_index_is_actually_brin(postgres_chain_url):
    """Presence is not the property that matters; the access method is.

    The allowlist above excuses ``ix_readings_ts_brin`` from the model
    comparison, so nothing else in this file would notice it being recreated as
    a btree -- which would still be an index, still be named that, and still cost
    what BRIN exists to avoid on an append-only table.
    """
    engine = sa.create_engine(postgres_chain_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            method = conn.execute(
                sa.text(
                    "SELECT am.amname FROM pg_class i "
                    "JOIN pg_am am ON am.oid = i.relam "
                    "WHERE i.relname = 'ix_readings_ts_brin'"
                )
            ).scalar()
    finally:
        engine.dispose()
    assert method == "brin", (
        f"ix_readings_ts_brin exists with access method {method!r}, not 'brin'"
    )
