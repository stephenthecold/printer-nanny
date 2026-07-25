"""Track how often an alert's condition has flapped.

Alert evaluation used to open at ``<= threshold`` and resolve the instant the
reading wasn't, with no damping in between. A supply sitting on its threshold
therefore resolved and re-opened every poll cycle, and because each re-open was
a brand-new alert row it was also a brand-new notification: a cartridge
oscillating 9<->11% around a threshold of 10 produced three alerts in six
cycles, which at a 60s poll interval is ~720 notifications a day for one
cartridge.

The evaluator now folds a re-fire inside ``alerts.renotify_cooldown_min`` back
into the alert it already raised, quietly. ``flap_count`` is where that folding
is recorded, so one incident stays one row and an operator can still see that it
has been unstable ("re-opened; flapped 14x within 30m") rather than having the
instability hidden by the very mechanism that stops the noise.

Data safety: additive only. NOT NULL with ``server_default='0'`` so existing
rows backfill to "never flapped" without a rewrite pass, and inserts from a
pre-upgrade process (a worker still running the old code mid-deploy) keep
working instead of failing the constraint.

Revision ID: 0024_alert_flap_count
Revises: 0023_worker_job_runs
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0024_alert_flap_count"
down_revision = "0023_worker_job_runs"
branch_labels = None
depends_on = None

TABLE = "alerts"
COLUMN = "flap_count"


def _columns(bind) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if COLUMN in _columns(bind):
        return
    op.add_column(
        TABLE,
        sa.Column(COLUMN, sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # Guarded so a downgrade run against a database that never had the column
    # (a pre-Alembic install stamped forward, say) doesn't fail on a missing
    # column -- and so it only ever removes what this revision added.
    bind = op.get_bind()
    if COLUMN not in _columns(bind):
        return
    with op.batch_alter_table(TABLE, schema=None) as batch:
        batch.drop_column(COLUMN)
