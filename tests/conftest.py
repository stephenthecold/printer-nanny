"""Test fixtures. Forces an isolated temp SQLite DB before importing the app."""

from __future__ import annotations

import atexit
import os
import tempfile

# Must be set before importing anything under `central` (engine binds at import).
# The filename carries the PID so concurrent pytest processes -- a second run in
# another checkout, or pytest-xdist workers -- each get their own database. A
# single fixed name here is shared machine-wide (tempdir is not per-worktree),
# so overlapping runs used to drop each other's tables mid-test and fail in
# ways that look like real bugs.
_TMP_DB = os.path.join(tempfile.gettempdir(), f"printer_nanny_test.{os.getpid()}.sqlite3")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["SECRET_KEY"] = "test-secret"

import pytest  # noqa: E402

from central.db import Base, SessionLocal, engine  # noqa: E402


@atexit.register
def _cleanup_tmp_db() -> None:
    """Remove this process's scratch DB. Per-PID names would otherwise pile up."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(_TMP_DB + suffix)
        except OSError:
            pass


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
