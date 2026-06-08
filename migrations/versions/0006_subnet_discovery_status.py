"""Add per-subnet discovery status (last_discovery_at + found/new counts).

Revision ID: 0006_subnet_discovery_status
Revises: 0005_printer_metadata
Create Date: 2026-06-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_subnet_discovery_status"
down_revision = "0005_printer_metadata"
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = _columns(insp, "subnets")
    if "last_discovery_at" not in cols:
        op.add_column(
            "subnets", sa.Column("last_discovery_at", sa.DateTime(timezone=True), nullable=True)
        )
    if "last_discovery_found_count" not in cols:
        op.add_column("subnets", sa.Column("last_discovery_found_count", sa.Integer(), nullable=True))
    if "last_discovery_new_count" not in cols:
        op.add_column("subnets", sa.Column("last_discovery_new_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    drop = [
        c for c in
        ("last_discovery_new_count", "last_discovery_found_count", "last_discovery_at")
        if c in _columns(insp, "subnets")
    ]
    if drop:
        # Batch mode so SQLite can drop columns (it recreates the table).
        with op.batch_alter_table("subnets") as batch:
            for col in drop:
                batch.drop_column(col)
