"""Add the worker_job_runs table (per-job worker liveness stamps).

A dead worker was undetectable: no job recorded a last-run timestamp anywhere and
/healthz answered 200 with the database unreachable. Because mark_offline_agents
is itself a worker job, a dead worker also stopped marking agents offline, so the
dashboard showed a green fleet over frozen data (docs/audit-2026-07.md).

One row per job name -- not one global cycle timestamp -- because the worker
swallows a single job's exception to keep the rest of the cycle alive, so a global
stamp would stay fresh while one job failed on every pass. Read by
central.health for /readyz, the worker container healthcheck, and the dashboard
banner.

Data safety on a populated database: purely additive. A new empty table, no
column added to an existing table, nothing rewritten, no constraint applied to
existing rows. An empty table reads as "the worker has never reported" (state
never_ran), which by design does NOT fail /readyz -- so applying this to a live
deployment cannot flip it into a false outage before the worker's next cycle.

Revision ID: 0023_worker_job_runs
Revises: 0022_printer_serial_index
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0023_worker_job_runs"
down_revision = "0022_printer_serial_index"
branch_labels = None
depends_on = None

_TABLE = "worker_job_runs"


def _table_exists(bind, table: str) -> bool:
    return sa.inspect(bind).has_table(table)


def upgrade() -> None:
    bind = op.get_bind()
    # Idempotent: SQLite dev boxes get this table from create_all() at startup,
    # so an operator who ran the app before migrating must not hit a duplicate.
    if _table_exists(bind, _TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("job", sa.String(80), primary_key=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(80), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "expected_interval_seconds",
            sa.Integer,
            nullable=False,
            server_default="60",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, _TABLE):
        return
    # Dropping this loses only liveness stamps -- no fleet, tenant or billing
    # data lives here, and the next worker cycle after a re-upgrade repopulates
    # every row. central.health tolerates the table being absent (state
    # "unknown"), so a downgraded stack keeps serving instead of 503-ing.
    op.drop_table(_TABLE)
