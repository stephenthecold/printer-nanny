"""Add supplies.status_note (coarse supply state when no numeric level reported).

Revision ID: 0004_supply_status_note
Revises: 0003_settings_and_auth
Create Date: 2026-06-06
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_supply_status_note"
down_revision = "0003_settings_and_auth"
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "status_note" not in _columns(insp, "supplies"):
        op.add_column("supplies", sa.Column("status_note", sa.String(60), nullable=True))


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "status_note" in _columns(insp, "supplies"):
        op.drop_column("supplies", "status_note")
