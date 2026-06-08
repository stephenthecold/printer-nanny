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
    # has_table is racy on Postgres when another container's create_all runs
    # in parallel (worker did this before the fix that goes out with this
    # migration). Belt-and-suspenders: use CREATE TABLE IF NOT EXISTS on
    # Postgres, fall back to the inspector check on SQLite.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE TABLE IF NOT EXISTS app_assets ("
            " name VARCHAR(40) NOT NULL,"
            " content_type VARCHAR(80) NOT NULL,"
            " data BYTEA NOT NULL,"
            " updated_at TIMESTAMP WITH TIME ZONE,"
            " PRIMARY KEY (name))"
        )
        return
    insp = sa.inspect(bind)
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
