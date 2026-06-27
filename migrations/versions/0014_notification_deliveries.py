"""Add the notification_deliveries table (durable channel retry / dead-letter).

A failed channel send used to be recorded on alerts.notified_channels and then
dropped, so a transient SMTP/Slack/webhook outage silently lost the alert. Each
(alert, channel) send now persists a row here; the retry_deliveries worker job
re-sends due rows with exponential backoff and dead-letters after a cap.

Revision ID: 0014_notification_deliveries
Revises: 0013_printer_display_name
Create Date: 2026-06-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_notification_deliveries"
down_revision = "0013_printer_display_name"
branch_labels = None
depends_on = None


def _table_exists(bind, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def upgrade() -> None:
    bind = op.get_bind()
    # Idempotent against a stack whose api/worker container ran create_all before
    # the migration (the SQLite dev path, or a worker race on Postgres).
    if _table_exists(bind, "notification_deliveries"):
        return
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "alert_id",
            sa.Integer,
            sa.ForeignKey("alerts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("channel_key", sa.String(120), nullable=False),
        # DeliveryStatus is stored as a string (native_enum=False): pending |
        # delivered | failed | dead.
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_notification_deliveries_alert_id", "notification_deliveries", ["alert_id"]
    )
    op.create_index(
        "ix_notification_deliveries_channel_key",
        "notification_deliveries",
        ["channel_key"],
    )
    op.create_index(
        "ix_notification_deliveries_status", "notification_deliveries", ["status"]
    )
    op.create_index(
        "ix_notification_deliveries_next_attempt_at",
        "notification_deliveries",
        ["next_attempt_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "notification_deliveries"):
        return
    op.drop_index(
        "ix_notification_deliveries_next_attempt_at",
        table_name="notification_deliveries",
    )
    op.drop_index(
        "ix_notification_deliveries_status", table_name="notification_deliveries"
    )
    op.drop_index(
        "ix_notification_deliveries_channel_key", table_name="notification_deliveries"
    )
    op.drop_index(
        "ix_notification_deliveries_alert_id", table_name="notification_deliveries"
    )
    op.drop_table("notification_deliveries")
