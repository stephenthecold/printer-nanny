"""Store privacy-minimized shared-mailbox shipping notices.

Revision ID: 0049_shipping_notices
Revises: 0048_supply_inventory
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049_shipping_notices"
down_revision = "0048_supply_inventory"
branch_labels = None
depends_on = None

TABLE = "shipping_notices"


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_message_id", sa.String(length=512), nullable=False),
        sa.Column("internet_message_id", sa.String(length=512), nullable=False),
        sa.Column("mailbox", sa.String(length=320), nullable=False),
        sa.Column("sender", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("vendor", sa.String(length=160), nullable=False),
        sa.Column("item_description", sa.String(length=500), nullable=False),
        sa.Column("sku", sa.String(length=120), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("ship_to", sa.String(length=500), nullable=False),
        sa.Column("tracking_number", sa.String(length=120), nullable=False),
        sa.Column("estimated_delivery_at", sa.Date(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "supply_order_id",
            sa.Integer(),
            sa.ForeignKey("supply_orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "quantity IS NULL OR quantity > 0",
            name="ck_shipping_notices_quantity_positive",
        ),
        sa.UniqueConstraint(
            "source_message_id", name="uq_shipping_notices_source_message_id"
        ),
    )
    op.create_index("ix_shipping_notices_site_id", TABLE, ["site_id"])
    op.create_index("ix_shipping_notices_supply_order_id", TABLE, ["supply_order_id"])
    op.create_index(
        "ix_shipping_notices_order_eta",
        TABLE,
        ["supply_order_id", "estimated_delivery_at"],
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        op.drop_table(TABLE)
