"""Add printers.last_provider_trace JSON for vendor-provider diagnostics.

Revision ID: 0010_printer_provider_trace
Revises: 0009_subnet_snmp_v3
Create Date: 2026-06-09

Operators need to see which vendor providers ran for each printer and what
each one returned (especially valuable for Brother, where PJL/EWS data
comes from outside the standard MIB). Storing the latest trace on Printer
keeps the printer detail page query simple -- no join through Reading.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_printer_provider_trace"
down_revision = "0009_subnet_snmp_v3"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE printers ADD COLUMN IF NOT EXISTS last_provider_trace JSON"
        )
        return
    if _column_exists(bind, "printers", "last_provider_trace"):
        return
    with op.batch_alter_table("printers") as batch_op:
        batch_op.add_column(sa.Column("last_provider_trace", sa.JSON, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "printers", "last_provider_trace"):
        return
    with op.batch_alter_table("printers") as batch_op:
        batch_op.drop_column("last_provider_trace")
