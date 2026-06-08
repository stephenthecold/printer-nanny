"""Add subnets.bind_interface for one-agent-many-clients deployments.

Revision ID: 0008_subnet_bind_interface
Revises: 0007_app_assets
Create Date: 2026-06-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_subnet_bind_interface"
down_revision = "0007_app_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # IF NOT EXISTS: idempotent on a Postgres stack that started up before
        # the migration shipped (mirrors the 0007 pattern after the worker race).
        op.execute(
            "ALTER TABLE subnets ADD COLUMN IF NOT EXISTS "
            "bind_interface VARCHAR(64)"
        )
        return
    with op.batch_alter_table("subnets") as batch_op:
        batch_op.add_column(sa.Column("bind_interface", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subnets") as batch_op:
        batch_op.drop_column("bind_interface")
