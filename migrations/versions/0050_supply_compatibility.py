"""Add the technician-maintained supply compatibility catalogue.

Revision ID: 0050_supply_compatibility
Revises: 0049_shipping_notices
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050_supply_compatibility"
down_revision = "0049_shipping_notices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("supply_products"):
        op.create_table(
            "supply_products",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("manufacturer", sa.String(length=100), nullable=False),
            sa.Column("sku", sa.String(length=120), nullable=False),
            sa.Column("product_key", sa.String(length=240), nullable=False),
            sa.Column("description", sa.String(length=200), nullable=False),
            sa.Column("supply_type", sa.String(length=40), nullable=False),
            sa.Column("color", sa.String(length=40), nullable=False),
            sa.Column("is_oem", sa.Boolean(), nullable=False),
            sa.Column("notes", sa.String(length=500), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("product_key", name="uq_supply_products_product_key"),
        )
        op.create_index(
            "ix_supply_products_slot", "supply_products", ["supply_type", "color"]
        )
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("supply_product_models"):
        op.create_table(
            "supply_product_models",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "product_id",
                sa.Integer(),
                sa.ForeignKey("supply_products.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("model_tag", sa.String(length=200), nullable=False),
            sa.Column("model_key", sa.String(length=200), nullable=False),
            sa.UniqueConstraint(
                "product_id", "model_key", name="uq_supply_product_model_key"
            ),
        )
        op.create_index(
            "ix_supply_product_models_product_id",
            "supply_product_models",
            ["product_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("supply_product_models"):
        op.drop_table("supply_product_models")
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("supply_products"):
        op.drop_table("supply_products")
