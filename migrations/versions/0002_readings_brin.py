"""Postgres-only: BRIN index on readings.ts for cheap time-range scans.

BRIN suits an append-only, time-ordered table: tiny on disk, fast for
"last N days" queries. No-op on SQLite. True monthly range-partitioning can be
layered on later if retention volume demands it.

Revision ID: 0002_readings_brin
Revises: 0001_baseline
Create Date: 2026-06-05
"""

from alembic import op

revision = "0002_readings_brin"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_readings_ts_brin "
            "ON readings USING brin (ts)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_readings_ts_brin")
