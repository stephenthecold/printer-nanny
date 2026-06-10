"""Add printers.display_name (operator-chosen friendly name).

Revision ID: 0013_printer_display_name
Revises: 0012_audit_log
Create Date: 2026-06-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_printer_display_name"
down_revision = "0012_audit_log"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE printers ADD COLUMN IF NOT EXISTS display_name VARCHAR(200)"
        )
        return
    if _column_exists(bind, "printers", "display_name"):
        return
    with op.batch_alter_table("printers") as batch_op:
        batch_op.add_column(sa.Column("display_name", sa.String(200), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "printers", "display_name"):
        return
    with op.batch_alter_table("printers") as batch_op:
        batch_op.drop_column("display_name")
