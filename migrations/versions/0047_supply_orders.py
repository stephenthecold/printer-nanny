"""Track location-specific consumable orders.

Revision ID: 0047_supply_orders
Revises: 0046_setup_bypasses
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047_supply_orders"
down_revision = "0046_setup_bypasses"
branch_labels = None
depends_on = None

TABLE = "supply_orders"


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("printer_id", sa.Integer(), sa.ForeignKey("printers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("supply_id", sa.Integer(), sa.ForeignKey("supplies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("supply_type", sa.String(length=40), nullable=False),
        sa.Column("color", sa.String(length=40), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("manufacturer", sa.String(length=100), nullable=False),
        sa.Column("sku", sa.String(length=120), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("vendor", sa.String(length=160), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("external_ref", sa.String(length=240), nullable=True),
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("estimated_delivery_at", sa.Date(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint("quantity > 0", name="ck_supply_orders_quantity_positive"),
        sa.UniqueConstraint("external_ref", name="uq_supply_orders_external_ref"),
    )
    op.create_index("ix_supply_orders_site_id", TABLE, ["site_id"])
    op.create_index("ix_supply_orders_printer_id", TABLE, ["printer_id"])
    op.create_index("ix_supply_orders_status", TABLE, ["status"])
    op.create_index("ix_supply_orders_site_status", TABLE, ["site_id", "status"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
