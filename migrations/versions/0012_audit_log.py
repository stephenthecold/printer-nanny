"""Add the audit_log table (operator-action audit trail).

Revision ID: 0012_audit_log
Revises: 0011_agent_diagnostics
Create Date: 2026-06-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_audit_log"
down_revision = "0011_agent_diagnostics"
branch_labels = None
depends_on = None


def _table_exists(bind, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def upgrade() -> None:
    bind = op.get_bind()
    # Idempotent against a stack whose api container ran create_all before
    # the migration (the SQLite dev path, or a worker race on Postgres).
    if _table_exists(bind, "audit_log"):
        return
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("username", sa.String(120), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("target", sa.String(300), nullable=True),
        sa.Column("detail", sa.Text, nullable=True),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "audit_log"):
        return
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_ts", table_name="audit_log")
    op.drop_table("audit_log")
