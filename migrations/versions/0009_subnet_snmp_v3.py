"""Add subnets.snmp_v3 JSON column for SNMPv3 credentials.

Revision ID: 0009_subnet_snmp_v3
Revises: 0008_subnet_bind_interface
Create Date: 2026-06-09

SNMPv3 per-subnet creds (USM user, security level, auth/priv protos + secrets)
live in a JSON blob to mirror the existing ``Printer.snmp_v3`` shape. JSON is a
small payload so we don't gain anything by exploding it into columns, and the
agent already passes JSON through end-to-end via the existing config endpoint.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_subnet_snmp_v3"
down_revision = "0008_subnet_bind_interface"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # IF NOT EXISTS: idempotent against a stack that started up before the
        # migration shipped (matches the 0007 / 0008 pattern after the worker
        # race fix).
        op.execute(
            "ALTER TABLE subnets ADD COLUMN IF NOT EXISTS snmp_v3 JSON"
        )
        return
    if _column_exists(bind, "subnets", "snmp_v3"):
        return
    with op.batch_alter_table("subnets") as batch_op:
        batch_op.add_column(sa.Column("snmp_v3", sa.JSON, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "subnets", "snmp_v3"):
        return
    with op.batch_alter_table("subnets") as batch_op:
        batch_op.drop_column("snmp_v3")
