"""Alembic environment. Pulls the DB URL from central.config and the metadata
from central.db so migrations stay in sync with the ORM models."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from central.config import settings
from central.db import Base
import central.models  # noqa: F401  (register models on Base.metadata)

config = context.config
# Deliberately NOT `config.set_main_option("sqlalchemy.url", settings.database_url)`.
# That writes through ConfigParser, whose BasicInterpolation treats `%` as a
# directive -- so any percent-encoded character in a password (`p%40ss`, entirely
# normal in a Postgres URL) makes `alembic upgrade head` die at import time with
# `ValueError: invalid interpolation syntax`, before a single migration runs. The
# option is also never read: alembic core never consults `sqlalchemy.url` itself,
# `fileConfig` below is handed a *filename* and re-parses alembic.ini from disk so
# it cannot see an in-memory option, and both paths here carry the URL themselves
# -- offline passes it to `context.configure(url=...)`, online assigns it onto the
# plain dict `get_section()` returns. Keep the URL out of ConfigParser.
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=settings.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = settings.database_url
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=settings.is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
