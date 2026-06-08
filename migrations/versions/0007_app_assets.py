"""Add app_assets table (operator-uploaded logo and similar small blobs).

Revision ID: 0007_app_assets
Revises: 0006_subnet_discovery_status
Create Date: 2026-06-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_app_assets"
down_revision = "0006_subnet_discovery_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table("app_assets"):
        op.create_table(
            "app_assets",
            sa.Column("name", sa.String(40), primary_key=True),
            sa.Column("content_type", sa.String(80), nullable=False),
            sa.Column("data", sa.LargeBinary(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table("app_assets"):
        op.drop_table("app_assets")
