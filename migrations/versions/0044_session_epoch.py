"""Session epoch on users, so logging out actually logs you out.

Revision ID: 0044_session_epoch
Revises: 0043_supply_yield
Create Date: 2026-08-04

Sessions are Starlette signed cookies with no server-side store, so
``session.clear()`` only sends a delete-cookie header -- the signed value the
browser already holds keeps verifying for the whole ``max_age`` (12h). Measured
before this: a captured cookie still reached ``/manage/users`` after the user
clicked logout, after they changed their own password, and after an admin reset
it. Role demotion and deactivation were already safe, because those are re-read
from the row on every request; the three *credential rotation* actions were not,
which is the wrong way round -- they are exactly the ones you take when you
believe a session is compromised.

``session_epoch`` is stamped into the session at login and compared on each
request. Bumping it invalidates every outstanding session for that user at once.

NOT NULL with a server default of 0, so the column is correct on an existing row
without a backfill pass. Declared in ``User.__table_args__``-adjacent model code
as well as here: revision 0001 is ``create_all``, so a column that exists only in
a migration is absent on every fresh install.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044_session_epoch"
down_revision = "0043_supply_yield"
branch_labels = None
depends_on = None


def _columns(insp, table: str) -> set:
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "session_epoch" in _columns(insp, "users"):
        # A fresh database was built by 0001's create_all against current
        # metadata, so the column is already there. Adding it again is an error
        # on both dialects.
        return
    op.add_column(
        "users",
        sa.Column(
            "session_epoch",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "session_epoch" not in _columns(insp, "users"):
        return
    # Batch mode so SQLite can drop it (it rebuilds the table); a plain ALTER on
    # Postgres. The rows survive either way -- this drops a counter, not data --
    # so migrations/guard.py's populated-table refusal is not being worked
    # around here, it simply does not apply to a column that carries no
    # operator-supplied value.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("session_epoch")
