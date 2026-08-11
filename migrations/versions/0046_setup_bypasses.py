"""Record explicit exceptions to guided setup requirements.

Revision ID: 0046_setup_bypasses
Revises: 0045_align_drifted_columns
Create Date: 2026-08-11

Fresh databases are built from current ORM metadata by revision 0001, so this
revision is deliberately conditional. Upgraded databases need the table; fresh
ones already have it before Alembic reaches this revision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046_setup_bypasses"
down_revision = "0045_align_drifted_columns"
branch_labels = None
depends_on = None

TABLE = "setup_bypasses"


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("step", sa.String(length=40), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("key", name="uq_setup_bypasses_key"),
    )
    op.create_index("ix_setup_bypasses_site_id", TABLE, ["site_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        op.drop_table(TABLE)
