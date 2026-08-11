"""Derive location stock from delivered orders and detected cartridge use.

Revision ID: 0048_supply_inventory
Revises: 0047_supply_orders
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0048_supply_inventory"
down_revision = "0047_supply_orders"
branch_labels = None
depends_on = None

TABLE = "supply_usages"


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "supply_cycle_id",
            sa.Integer(),
            sa.ForeignKey("supply_cycles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "printer_id",
            sa.Integer(),
            sa.ForeignKey("printers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "supply_order_id",
            sa.Integer(),
            sa.ForeignKey("supply_orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("supply_type", sa.String(length=40), nullable=False),
        sa.Column("color", sa.String(length=40), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "assigned_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("supply_cycle_id", name="uq_supply_usages_supply_cycle_id"),
    )
    op.create_index("ix_supply_usages_site_id", TABLE, ["site_id"])
    op.create_index("ix_supply_usages_printer_id", TABLE, ["printer_id"])
    op.create_index("ix_supply_usages_supply_order_id", TABLE, ["supply_order_id"])
    op.create_index("ix_supply_usages_status", TABLE, ["status"])
    op.create_index("ix_supply_usages_site_status", TABLE, ["site_id", "status"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
