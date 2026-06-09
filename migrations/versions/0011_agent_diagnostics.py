"""Surface agent self-update diagnostics: install_path + last_update_result.

Revision ID: 0011_agent_diagnostics
Revises: 0010_printer_provider_trace
Create Date: 2026-06-09

Operators need to see whether 'Update' on /manage/agents actually pip-installed
the new code -- previously the only way was to ssh into the agent host and
read its log. This migration adds install_path + last_update_result (JSON)
that the agent reports on every heartbeat.

Also widens version VARCHAR(40) -> VARCHAR(80) since the install-marker
suffix (``+YYYYMMDD-HHMMSS``) wouldn't always fit.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_agent_diagnostics"
down_revision = "0010_printer_provider_trace"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS install_path VARCHAR(400)"
        )
        op.execute(
            "ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_update_result JSON"
        )
        # version VARCHAR(40) -> VARCHAR(80) to fit install-marker suffix.
        op.execute("ALTER TABLE agents ALTER COLUMN version TYPE VARCHAR(80)")
        return

    # SQLite path: idempotent add. Skip the version widening -- SQLite
    # doesn't enforce VARCHAR length, so the existing column accepts the
    # longer strings fine (no batch_alter needed for a no-op).
    if not _column_exists(bind, "agents", "install_path"):
        with op.batch_alter_table("agents") as batch_op:
            batch_op.add_column(sa.Column("install_path", sa.String(400), nullable=True))
    if not _column_exists(bind, "agents", "last_update_result"):
        with op.batch_alter_table("agents") as batch_op:
            batch_op.add_column(sa.Column("last_update_result", sa.JSON, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("agents") as batch_op:
        if _column_exists(bind, "agents", "install_path"):
            batch_op.drop_column("install_path")
        if _column_exists(bind, "agents", "last_update_result"):
            batch_op.drop_column("last_update_result")
