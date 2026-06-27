"""Add supplies.days_to_empty + supplies.forecast_at (persisted supply forecast).

The forecast pass now persists each supply's days-to-empty estimate (and when it
was computed) onto the Supply row so dashboards/portal/reports read it instead of
re-fitting on every render. Both are nullable -- the worker's next forecast pass
populates them, so no backfill is needed.

Revision ID: 0015_supply_forecast
Revises: 0014_notification_deliveries
Create Date: 2026-06-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_supply_forecast"
down_revision = "0014_notification_deliveries"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE supplies ADD COLUMN IF NOT EXISTS days_to_empty "
            "DOUBLE PRECISION"
        )
        op.execute(
            "ALTER TABLE supplies ADD COLUMN IF NOT EXISTS forecast_at "
            "TIMESTAMP WITH TIME ZONE"
        )
        return
    with op.batch_alter_table("supplies") as batch_op:
        if not _column_exists(bind, "supplies", "days_to_empty"):
            batch_op.add_column(sa.Column("days_to_empty", sa.Float, nullable=True))
        if not _column_exists(bind, "supplies", "forecast_at"):
            batch_op.add_column(
                sa.Column("forecast_at", sa.DateTime(timezone=True), nullable=True)
            )


def downgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("supplies") as batch_op:
        if _column_exists(bind, "supplies", "forecast_at"):
            batch_op.drop_column("forecast_at")
        if _column_exists(bind, "supplies", "days_to_empty"):
            batch_op.drop_column("days_to_empty")
