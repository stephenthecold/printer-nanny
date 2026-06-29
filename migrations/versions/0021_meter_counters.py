"""Add mono/color impression meters + per-function meter snapshot.

The billing-grade meter split:
- printers.mono_count / printers.color_count: latest cached split of page_count.
- readings.mono_count / readings.color_count: per-reading split (billing diffs
  these across a period).
- readings.meter_snapshot: vendor-shaped per-function breakdown JSON
  (e.g. {"total": N, "mono": N, "color": N, "print": N, "copy": N, "fax": N}).

All nullable -- a device/provider that doesn't report a split leaves them NULL
(we never invent meters).

Revision ID: 0021_meter_counters
Revises: 0020_user_scim
Create Date: 2026-06-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0021_meter_counters"
down_revision = "0020_user_scim"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE printers ADD COLUMN IF NOT EXISTS mono_count INTEGER")
        op.execute("ALTER TABLE printers ADD COLUMN IF NOT EXISTS color_count INTEGER")
        op.execute("ALTER TABLE readings ADD COLUMN IF NOT EXISTS mono_count INTEGER")
        op.execute("ALTER TABLE readings ADD COLUMN IF NOT EXISTS color_count INTEGER")
        op.execute("ALTER TABLE readings ADD COLUMN IF NOT EXISTS meter_snapshot JSONB")
        return
    with op.batch_alter_table("printers") as batch_op:
        if not _column_exists(bind, "printers", "mono_count"):
            batch_op.add_column(sa.Column("mono_count", sa.Integer, nullable=True))
        if not _column_exists(bind, "printers", "color_count"):
            batch_op.add_column(sa.Column("color_count", sa.Integer, nullable=True))
    with op.batch_alter_table("readings") as batch_op:
        if not _column_exists(bind, "readings", "mono_count"):
            batch_op.add_column(sa.Column("mono_count", sa.Integer, nullable=True))
        if not _column_exists(bind, "readings", "color_count"):
            batch_op.add_column(sa.Column("color_count", sa.Integer, nullable=True))
        if not _column_exists(bind, "readings", "meter_snapshot"):
            batch_op.add_column(sa.Column("meter_snapshot", sa.JSON, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("readings") as batch_op:
        for col in ("meter_snapshot", "color_count", "mono_count"):
            if _column_exists(bind, "readings", col):
                batch_op.drop_column(col)
    with op.batch_alter_table("printers") as batch_op:
        for col in ("color_count", "mono_count"):
            if _column_exists(bind, "printers", col):
                batch_op.drop_column(col)
