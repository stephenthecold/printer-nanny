"""Test fixtures. Forces an isolated temp SQLite DB before importing the app."""

from __future__ import annotations

import os
import tempfile

# Must be set before importing anything under `central` (engine binds at import).
_TMP_DB = os.path.join(tempfile.gettempdir(), "printer_nanny_test.sqlite3")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP_DB}"
os.environ["SECRET_KEY"] = "test-secret"

import pytest  # noqa: E402

from central.db import Base, SessionLocal, engine  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
