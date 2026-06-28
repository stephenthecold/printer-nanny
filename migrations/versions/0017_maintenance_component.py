"""Add maintenance_schedules.component_type + life_threshold (component-life triggers).

Lets a maintenance schedule fire when a tracked component's life percentage
(fuser/drum/belt/laser/PF-kit, from the Brother maintenance blob) drops to or
below a threshold. Both nullable: a schedule without them keeps its existing
interval/page-threshold behavior.

Revision ID: 0017_maintenance_component
Revises: 0016_alert_routing_escalation
Create Date: 2026-06-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_maintenance_component"
down_revision = "0016_alert_routing_escalation"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE maintenance_schedules ADD COLUMN IF NOT EXISTS "
            "component_type VARCHAR(32)"
        )
        op.execute(
            "ALTER TABLE maintenance_schedules ADD COLUMN IF NOT EXISTS "
            "life_threshold DOUBLE PRECISION"
        )
        return
    with op.batch_alter_table("maintenance_schedules") as batch_op:
        if not _column_exists(bind, "maintenance_schedules", "component_type"):
            batch_op.add_column(sa.Column("component_type", sa.String(32), nullable=True))
        if not _column_exists(bind, "maintenance_schedules", "life_threshold"):
            batch_op.add_column(sa.Column("life_threshold", sa.Float, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("maintenance_schedules") as batch_op:
        if _column_exists(bind, "maintenance_schedules", "life_threshold"):
            batch_op.drop_column("life_threshold")
        if _column_exists(bind, "maintenance_schedules", "component_type"):
            batch_op.drop_column("component_type")
