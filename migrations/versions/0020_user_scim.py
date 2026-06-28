"""Add users.active + scim_external_id + updated_at (SCIM lifecycle).

- active: account-active / SCIM-deprovision flag (NOT NULL DEFAULT true backfills
  existing users to active).
- scim_external_id: the IdP's stable externalId.
- updated_at: last-modified for SCIM meta.lastModified.

Revision ID: 0020_user_scim
Revises: 0019_report_runs
Create Date: 2026-06-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_user_scim"
down_revision = "0019_report_runs"
branch_labels = None
depends_on = None


def _column_exists(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT true"
        )
        op.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS scim_external_id VARCHAR(200)"
        )
        op.execute(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at "
            "TIMESTAMP WITH TIME ZONE"
        )
        return
    with op.batch_alter_table("users") as batch_op:
        if not _column_exists(bind, "users", "active"):
            batch_op.add_column(
                sa.Column("active", sa.Boolean, nullable=False, server_default="1")
            )
        if not _column_exists(bind, "users", "scim_external_id"):
            batch_op.add_column(
                sa.Column("scim_external_id", sa.String(200), nullable=True)
            )
        if not _column_exists(bind, "users", "updated_at"):
            batch_op.add_column(
                sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True)
            )


def downgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("users") as batch_op:
        if _column_exists(bind, "users", "updated_at"):
            batch_op.drop_column("updated_at")
        if _column_exists(bind, "users", "scim_external_id"):
            batch_op.drop_column("scim_external_id")
        if _column_exists(bind, "users", "active"):
            batch_op.drop_column("active")
