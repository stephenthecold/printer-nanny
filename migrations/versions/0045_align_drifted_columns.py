"""Bring already-upgraded databases in line with the model.

Revision ID: 0045_align_drifted_columns
Revises: 0044_session_epoch
Create Date: 2026-08-04

The previous pass corrected the column definitions inside 0034, 0039 and 0042 --
which fixes nothing for anyone who has already run them. Alembic records applied
revisions in ``alembic_version`` and never re-runs one, so editing a
``create_table`` only reaches installs that upgrade *past* that revision in
future. Every deployment already at 0043 -- precisely the population the drift
finding was about -- kept the old shape.

So the edits made the FRESH path right and this makes the UPGRADED path right.
Both are needed, and the first alone reads like a fix while leaving the affected
databases untouched.

What drifted, model vs migration:

* ``remote_requests.kind`` / ``.status`` -- ``String(16)`` where the model's
  ``_enum(...)`` renders ``VARCHAR(32)``. Harmless today (the longest value is
  ``"succeeded"``), and a hard failure the first time an enum member exceeds 16
  characters -- on Postgres, on upgraded installs only, which is the awkward
  half to reproduce.
* ``remote_requests.truncated`` -- nullable where the model says ``Mapped[bool]``.
* ``created_at`` / ``updated_at`` on ``reading_rollups`` and
  ``device_definitions``, and ``created_at`` on ``remote_requests`` -- nullable
  where the models say NOT NULL.

Every change is introspected first and skipped when the column is already
correct, so on a fresh database (built by 0001's ``create_all`` against current
metadata) this whole revision is a no-op. That matters more than tidiness on
SQLite, where ``batch_alter_table`` REBUILDS the table: ``reading_rollups`` is
the retention rollup and can be large, and rebuilding it to change nothing would
be a long outage in exchange for no effect.

NULLs are backfilled before the constraint is applied, because a NOT NULL added
over an existing NULL fails the whole migration -- and the timestamps are only
nullable here by accident, so any row carrying one predates the intent.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_align_drifted_columns"
down_revision = "0044_session_epoch"
branch_labels = None
depends_on = None


#: table -> column -> what the model declares.
_WIDEN = {
    "remote_requests": {"kind": 32, "status": 32},
}
_NOT_NULL = {
    "reading_rollups": ("created_at", "updated_at"),
    "device_definitions": ("created_at", "updated_at"),
    "remote_requests": ("created_at", "truncated"),
}


def _columns(insp, table: str) -> dict:
    if not insp.has_table(table):
        return {}
    return {c["name"]: c for c in insp.get_columns(table)}


def _needs_widening(col: dict, want: int) -> bool:
    length = getattr(col.get("type"), "length", None)
    return length is not None and length < want


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    for table, wants in _WIDEN.items():
        cols = _columns(insp, table)
        todo = {
            name: want for name, want in wants.items()
            if name in cols and _needs_widening(cols[name], want)
        }
        if not todo:
            continue
        with op.batch_alter_table(table) as batch:
            for name, want in todo.items():
                batch.alter_column(
                    name,
                    existing_type=sa.String(length=cols[name]["type"].length),
                    type_=sa.String(length=want),
                    existing_nullable=cols[name]["nullable"],
                )

    for table, names in _NOT_NULL.items():
        cols = _columns(insp, table)
        todo = [n for n in names if n in cols and cols[n]["nullable"]]
        if not todo:
            continue
        # Backfill first: a NOT NULL applied over an existing NULL aborts the
        # whole migration. `truncated` defaults false; the timestamps take the
        # current time, which is wrong-but-bounded for rows that should never
        # have been able to hold NULL in the first place.
        for name in todo:
            filler = sa.false() if name == "truncated" else sa.func.now()
            # Built with the SQLAlchemy expression language rather than an
            # f-string: the names here are our own constants, but a migration
            # that interpolates identifiers is a pattern worth not leaving
            # around for the next one that takes them from somewhere else.
            op.execute(
                sa.update(sa.table(table, sa.column(name)))
                .where(sa.column(name).is_(None))
                .values({name: filler})
            )
        with op.batch_alter_table(table) as batch:
            for name in todo:
                batch.alter_column(
                    name,
                    existing_type=cols[name]["type"],
                    nullable=False,
                )


def downgrade() -> None:
    """Deliberately a no-op.

    Every change here makes a column MATCH the model. Reversing them would
    reintroduce the drift and, for the NOT NULLs, would be reversing a
    constraint rather than restoring data -- there is nothing to give back. A
    downgrade past this point still works; it simply leaves the columns correct.
    """
