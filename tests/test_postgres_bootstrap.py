"""The schema must be creatable on Postgres, not merely on SQLite.

Migration 0001 is ``Base.metadata.create_all()``, so **the ORM metadata is what
builds a fresh database** -- a declaration Postgres rejects is not a cosmetic
issue, it is an install that cannot create its schema at all.

That happened. ``suppression_windows.allow_breakthrough`` was declared
``Boolean, server_default=text("1")``, which renders as an unquoted integer:

    psycopg.errors.DatatypeMismatch: column "allow_breakthrough" is of type
    boolean but default expression is of type integer

so ``alembic upgrade head`` failed outright against a fresh Postgres, breaking
the documented compose bootstrap. **SQLite accepts 1 for a boolean**, which is
precisely why 1777 tests passed over it -- the same "CI has zero Postgres"
blindness this repo has already paid for on the Windows client.

These tests deliberately need **no Postgres server**. They compile the real
metadata against the Postgres *dialect*, so the check runs in ordinary CI on
SQLite and still catches a dialect-specific defect. A test that required a live
Postgres would be skipped exactly where it is needed most.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import central.models  # noqa: F401  (register models on Base.metadata)
from central.db import Base

PG = postgresql.dialect()

# A boolean column's DEFAULT must be a real boolean literal. Both an unquoted
# integer (DEFAULT 1 -- hard error) and a quoted one (DEFAULT '1' -- accepted
# only because Postgres coerces it) are the same latent mistake.
_BOOL_DEFAULT = re.compile(
    r"\bBOOLEAN\b[^,]*?\bDEFAULT\s+(?P<value>'?[^\s,]+'?)",
    re.IGNORECASE,
)


def _pg_ddl(table) -> str:
    return str(CreateTable(table).compile(dialect=PG))


def test_every_table_compiles_for_postgres():
    """No table may fail to render DDL for Postgres."""
    for table in Base.metadata.sorted_tables:
        _pg_ddl(table)  # raises if the declaration cannot be compiled


def test_no_boolean_column_defaults_to_a_numeric_literal():
    """The regression that broke the fresh-Postgres bootstrap.

    Asserts against the *rendered Postgres DDL*, not the Python declaration, so
    it catches every spelling that produces a numeric default -- ``text("1")``,
    ``"1"``, ``1`` -- rather than one blacklisted idiom.
    """
    offenders = []
    for table in Base.metadata.sorted_tables:
        for line in _pg_ddl(table).splitlines():
            match = _BOOL_DEFAULT.search(line)
            if not match:
                continue
            value = match.group("value").strip().strip("'").upper()
            if value not in {"TRUE", "FALSE"}:
                offenders.append(f"{table.name}: {line.strip()}")

    assert not offenders, (
        "boolean columns must render a boolean DEFAULT for Postgres "
        "(use sqlalchemy.true()/false(), not 1/0):\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "table_name,column_name",
    [("suppression_windows", "allow_breakthrough"), ("users", "active")],
)
def test_known_boolean_defaults_render_as_booleans(table_name, column_name):
    """Pin the two columns that carried the defect, by name.

    The sweep above is the real guard; these two make the failure message name
    the actual column if it ever comes back.
    """
    table = Base.metadata.tables[table_name]
    line = next(
        ln for ln in _pg_ddl(table).splitlines() if ln.strip().startswith(column_name)
    )
    assert "DEFAULT true" in line, f"expected a boolean DEFAULT, got: {line.strip()}"
