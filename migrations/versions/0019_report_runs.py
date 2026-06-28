"""Add the report_runs table (transactional scheduled-report idempotency).

One row claims "(report_type, period_key) was sent"; the UNIQUE constraint makes
the weekly/monthly send race-safe even with multiple worker processes.

Revision ID: 0019_report_runs
Revises: 0018_printer_firmware
Create Date: 2026-06-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_report_runs"
down_revision = "0018_printer_firmware"
branch_labels = None
depends_on = None


def _table_exists(bind, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "report_runs"):
        return
    op.create_table(
        "report_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("report_type", sa.String(32), nullable=False),
        sa.Column("period_key", sa.String(40), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "report_type", "period_key", name="uq_report_run_type_period"
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "report_runs"):
        return
    op.drop_table("report_runs")
