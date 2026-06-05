"""Database engine, session factory, and the declarative Base."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from central.config import settings


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
