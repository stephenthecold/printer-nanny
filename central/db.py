"""Database engine, session factory, and the declarative Base."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from central.config import settings

log = logging.getLogger("central.db")

# Stable 64-bit key for the worker's single-leader advisory lock. Postgres
# advisory locks are keyed by an arbitrary bigint; this constant identifies the
# "worker cycle" lock so two worker containers can't both run a cycle at once.
# Value is arbitrary but fixed (derived once from "printer-nanny:worker-cycle").
WORKER_CYCLE_LOCK_KEY = 0x504E574B  # 'PNWK'


class Base(DeclarativeBase):
    pass


def _engine_kwargs() -> dict:
    if settings.is_sqlite:
        # check_same_thread=False lets the worker thread + request threads share
        # the SQLite file during local dev.
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}


engine = create_engine(settings.database_url, future=True, **_engine_kwargs())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all() -> None:
    """Create all tables. Used for SQLite dev/tests and the seed script.

    Production (Postgres) should use Alembic migrations instead, which additionally
    set up monthly range partitioning on the ``readings`` table.
    """
    import central.models  # noqa: F401  (ensure models are registered on Base)

    Base.metadata.create_all(bind=engine)


@contextmanager
def try_leader_lock(
    key: int = WORKER_CYCLE_LOCK_KEY,
) -> Iterator[bool]:
    """Try to acquire the single-leader advisory lock for a worker cycle.

    Yields ``True`` if this process holds the lock and should run the cycle, or
    ``False`` if another worker already holds it (the caller must SKIP the cycle).
    The lock is always released on exit when it was acquired.

    Postgres: a session-level ``pg_try_advisory_lock(key)`` -- non-blocking, so a
    second worker container that loses the race gets ``False`` immediately and
    skips instead of double-processing. The lock is held on a dedicated short-
    lived connection for the duration of the ``with`` block and released with
    ``pg_advisory_unlock`` on exit (and implicitly if the connection drops).

    SQLite / dev (``settings.is_sqlite``): the deployment is single-process by
    construction, so there's nothing to coordinate. The lock is a logged no-op
    that always "acquires" (yields ``True``), preserving the acquire/skip/release
    contract without touching the file (SQLite has no advisory-lock primitive).

    Usage::

        with try_leader_lock() as acquired:
            if not acquired:
                return  # another worker holds the lock; skip this cycle
            run_cycle()
    """
    if settings.is_sqlite:
        # Single-process dev/test: always the leader, nothing to release.
        log.debug("try_leader_lock: sqlite no-op, acquiring (key=%s)", key)
        yield True
        return

    # Postgres: hold the lock on its own connection for the block's lifetime.
    conn = engine.connect()
    acquired = False
    try:
        acquired = bool(
            conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": key}
            ).scalar()
        )
        if not acquired:
            log.info("try_leader_lock: lock %s held by another worker; skipping", key)
        yield acquired
    finally:
        try:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
        finally:
            conn.close()
