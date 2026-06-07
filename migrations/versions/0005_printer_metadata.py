"""Add notes, asset_tag, and tags to printers (operator-managed metadata).

Revision ID: 0005_printer_metadata
Revises: 0004_supply_status_note
Create Date: 2026-06-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_printer_metadata"
down_revision = "0004_supply_status_note"
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    cols = _columns(insp, "printers")
    if "notes" not in cols:
        op.add_column("printers", sa.Column("notes", sa.Text(), nullable=True))
    if "asset_tag" not in cols:
        op.add_column("printers", sa.Column("asset_tag", sa.String(120), nullable=True))
    if "tags" not in cols:
        op.add_column("printers", sa.Column("tags", sa.JSON(), nullable=True))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    drop = [c for c in ("tags", "asset_tag", "notes") if c in _columns(insp, "printers")]
    if drop:
        # Batch mode so SQLite can drop columns (it recreates the table).
        with op.batch_alter_table("printers") as batch:
            for col in drop:
                batch.drop_column(col)
