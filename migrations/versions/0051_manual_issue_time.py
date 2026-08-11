"""Record when a staff-reported issue occurred.

Revision ID: 0051_manual_issue_time
Revises: 0050_supply_compatibility
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0051_manual_issue_time"
down_revision = "0050_supply_compatibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("alerts")}
    if "occurred_at" not in columns:
        op.add_column(
            "alerts", sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("alerts")}
    if "occurred_at" in columns:
        with op.batch_alter_table("alerts") as batch:
            batch.drop_column("occurred_at")
