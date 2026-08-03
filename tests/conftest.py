"""Test fixtures. Picks the database backend BEFORE importing the app.

Two backends, one suite:

* **SQLite** (default) -- an isolated temp file, no setup, what a contributor
  gets from a bare ``pytest``.
* **Postgres** (opt-in) -- set ``PN_TEST_DATABASE_URL`` and the whole suite runs
  against a real server. This is the only way the Postgres-only paths get
  exercised at all: the BRIN index on ``readings.ts``, ``pg_try_advisory_lock``
  leader election, ``pg_dump``/``pg_restore``, and Alembic without
  ``render_as_batch``. See ``.github/workflows/postgres.yml``.

**The opt-in variable is deliberately NOT ``DATABASE_URL``.** This fixture drops
or truncates every table it can see on the target, so honouring an ambient
``DATABASE_URL`` would mean a shell that happens to carry a production URL loses
its database to a stray ``pytest``. ``PN_TEST_DATABASE_URL`` exists nowhere else
in this project and has to be set on purpose.
"""

from __future__ import annotations

import atexit
import os
import pathlib
import subprocess
import tempfile

# Must be set before importing anything under `central` (engine binds at import).
_PG_URL = os.environ.get("PN_TEST_DATABASE_URL", "").strip()
POSTGRES = bool(_PG_URL) and not _PG_URL.startswith("sqlite")
_TMP_DB = ""

if _PG_URL:
    # Honoured verbatim, including a sqlite:/// value -- silently ignoring a URL
    # somebody deliberately set is how a run gets misread as proving something.
    os.environ["DATABASE_URL"] = _PG_URL
else:
    # The filename carries the PID so concurrent pytest processes -- a second run
    # in another checkout, or pytest-xdist workers -- each get their own
    # database. A single fixed name here is shared machine-wide (tempdir is not
    # per-worktree), so overlapping runs used to drop each other's tables
    # mid-test and fail in ways that look like real bugs.
    _TMP_DB = os.path.join(tempfile.gettempdir(), f"printer_nanny_test.{os.getpid()}.sqlite3")
    os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["SECRET_KEY"] = "test-secret"

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

import central.models  # noqa: E402,F401  (register every table on Base.metadata)
from central.config import settings  # noqa: E402
from central.db import Base, SessionLocal, engine  # noqa: E402

# The dashboard marks its session cookie ``Secure`` whenever the database is not
# SQLite (``settings.secure_cookies`` -> ``is_production``), because a real
# deployment sits behind TLS at Caddy. A Secure cookie is *stored* by the client
# but never *sent* back over http -- verified both ways: on Postgres the login
# POST sets the cookie either way, and the following GET /manage/users is 303
# (bounced to /login) on http and 200 on https. So a harness talking to
# ``http://testserver`` would log every dashboard test out again silently. It
# therefore speaks https exactly when the app is in that mode, which is also
# what production actually looks like.
# Tests that assert on the scheme should read this rather than hardcoding one.
TEST_BASE_URL = "https://testserver" if settings.secure_cookies else "http://testserver"


def _default_testclient_base_url() -> None:
    """Fill in TestClient's ``base_url`` default without disturbing callers.

    189 call sites say ``TestClient(app)``; parameterising each one would be a
    worse trade than filling the default in a single place. Only the default is
    substituted -- an explicit ``base_url`` still wins.
    """
    from starlette.testclient import TestClient

    original = TestClient.__init__

    def __init__(self, app, *args, **kwargs):  # noqa: N807
        if not args and "base_url" not in kwargs:
            kwargs["base_url"] = TEST_BASE_URL
        original(self, app, *args, **kwargs)

    TestClient.__init__ = __init__


if TEST_BASE_URL != "http://testserver":
    _default_testclient_base_url()


@atexit.register
def _cleanup_tmp_db() -> None:
    """Remove this process's scratch DB. Per-PID names would otherwise pile up."""
    if not _TMP_DB:
        return
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(_TMP_DB + suffix)
        except OSError:
            pass


_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "postgres_only: exercises a code path that only exists on Postgres; "
        "skipped unless PN_TEST_DATABASE_URL is set.",
    )
    config.addinivalue_line(
        "markers",
        "sqlite_only: asserts SQLite-specific behaviour (file layout, "
        "single-process no-ops); skipped on Postgres.",
    )


def pytest_collection_modifyitems(config, items) -> None:
    skip_pg = pytest.mark.skip(reason="needs Postgres (set PN_TEST_DATABASE_URL)")
    skip_lite = pytest.mark.skip(reason="SQLite-specific; suite is running on Postgres")
    for item in items:
        if not POSTGRES and "postgres_only" in item.keywords:
            item.add_marker(skip_pg)
        if POSTGRES and "sqlite_only" in item.keywords:
            item.add_marker(skip_lite)


@pytest.fixture(scope="session")
def git_file_mode():
    """A callable giving a path's mode as *git* records it, or None.

    The executable bit on a shipped script is a property of the repository, not
    of whichever filesystem a checkout happened to land on. Git on Windows does
    not materialise the bit in the working tree, so ``stat()`` there says
    nothing about what ships -- it reports 0o666 for a file git has as 100755,
    and a test asserting on it fails for a reason that has nothing to do with
    the packaging it is trying to guard.

    Asking git is the same question the packagers and CI ask. Returns None when
    there is no git checkout to ask (a source tarball, an export), so callers
    can skip rather than fail on a question that cannot be posed.
    """

    def mode(path) -> "str | None":
        try:
            proc = subprocess.run(
                ["git", "ls-files", "-s", "--", str(path)],
                cwd=str(_REPO_ROOT), capture_output=True, text=True,
            )
        except OSError:  # no git on PATH
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        return proc.stdout.split()[0]

    return mode


@pytest.fixture(scope="session")
def _schema():
    """Build the schema once per session (Postgres) or hand back the marker.

    On SQLite the per-test drop/create stays: it is ~0.4s per rebuild on a file
    that lives in the page cache, and it is what 1700 tests have always run
    against. On Postgres the same loop pays a network round trip per table per
    test, so the schema is built once and each test gets a TRUNCATE instead --
    measured below in the fixture.
    """
    if POSTGRES:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
    yield
    if POSTGRES:
        Base.metadata.drop_all(bind=engine)


_truncate_sql = ""


def _truncate_all() -> str:
    """One statement, every table, identities reset.

    The Postgres equivalent of what drop_all/create_all gives SQLite -- fresh
    rowids included, which matters because tests do assert on ids. Built on
    first use rather than at import so a table registered by a module imported
    during collection is still covered.
    """
    global _truncate_sql
    if not _truncate_sql:
        names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        _truncate_sql = f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"
    return _truncate_sql


def _rebuild_schema() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


@pytest.fixture()
def db(_schema):
    if POSTGRES:
        try:
            with engine.begin() as conn:
                conn.execute(text(_truncate_all()))
        except SQLAlchemyError:
            # A handful of tests DROP a table on purpose, to prove the app
            # survives a missing one (test_branding, test_worker_health). On
            # SQLite the next rebuild silently put it back; TRUNCATE cannot,
            # and a table missing for the rest of the session poisons every
            # test after it -- 626 errors, all attributed to the wrong tests.
            # Falling back costs nothing on the 99% of tests that truncate fine.
            _rebuild_schema()
    else:
        _rebuild_schema()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
