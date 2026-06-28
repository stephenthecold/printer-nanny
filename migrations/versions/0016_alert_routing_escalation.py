"""Add alerts.external_ref + escalation bookkeeping (last_notified_at, escalation_level).

- external_ref: FreeScout ticket/conversation id captured at open time, so the
  closed-loop resolver can auto-close that exact ticket.
- last_notified_at / escalation_level: re-notify/escalation bookkeeping for
  alerts.escalate_after_minutes.

Revision ID: 0016_alert_routing_escalation
Revises: 0015_supply_forecast
Create Date: 2026-06-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_alert_routing_escalation"
down_revision = "0015_supply_forecast"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS external_ref VARCHAR(120)")
        op.execute(
            "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS last_notified_at "
            "TIMESTAMP WITH TIME ZONE"
        )
        # NOT NULL with a server default backfills existing rows to 0.
        op.execute(
            "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS escalation_level "
            "INTEGER NOT NULL DEFAULT 0"
        )
        return
    with op.batch_alter_table("alerts") as batch_op:
        if not _column_exists(bind, "alerts", "external_ref"):
            batch_op.add_column(sa.Column("external_ref", sa.String(120), nullable=True))
        if not _column_exists(bind, "alerts", "last_notified_at"):
            batch_op.add_column(
                sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True)
            )
        if not _column_exists(bind, "alerts", "escalation_level"):
            batch_op.add_column(
                sa.Column(
                    "escalation_level",
                    sa.Integer,
                    nullable=False,
                    server_default="0",
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("alerts") as batch_op:
        if _column_exists(bind, "alerts", "escalation_level"):
            batch_op.drop_column("escalation_level")
        if _column_exists(bind, "alerts", "last_notified_at"):
            batch_op.drop_column("last_notified_at")
        if _column_exists(bind, "alerts", "external_ref"):
            batch_op.drop_column("external_ref")
