"""Add printers.firmware (best-effort firmware/version captured over SNMP).

Powers the device security-posture report. Nullable; surfaced as "unknown"
when the device's sysDescr doesn't expose a version.

Revision ID: 0018_printer_firmware
Revises: 0017_maintenance_component
Create Date: 2026-06-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_printer_firmware"
down_revision = "0017_maintenance_component"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE printers ADD COLUMN IF NOT EXISTS firmware VARCHAR(200)")
        return
    if _column_exists(bind, "printers", "firmware"):
        return
    with op.batch_alter_table("printers") as batch_op:
        batch_op.add_column(sa.Column("firmware", sa.String(200), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "printers", "firmware"):
        return
    with op.batch_alter_table("printers") as batch_op:
        batch_op.drop_column("firmware")
