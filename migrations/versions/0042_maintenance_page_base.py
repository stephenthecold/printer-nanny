"""anchor a page-driven maintenance schedule to the meter at its last service

Revision ID: 0042_maintenance_page_base
Revises: 0042_supply_class
Create Date: 2026-08-03

``maintenance_schedules.page_threshold`` was an absolute odometer target with
nothing to move it, so "Mark serviced" on a schedule with no ``interval_days``
re-armed nothing: ``next_due`` stayed in the past, the schedule stayed due, and
its maintenance-due alert could never resolve -- while the UI reported success.

``last_serviced_page_count`` is the meter reading at the last logged service.
The effective target becomes ``last_serviced_page_count + page_threshold``
(``MaintenanceSchedule.page_target``), so servicing a kit pushes the next one
out by one kit-life.

NULL means "never serviced", which resolves to base 0 and therefore to the
configured threshold -- byte-for-byte the behaviour every existing row already
has. That is why this is a plain nullable column with no backfill: there is no
state to migrate, only a place to record the next one.

Conditional, like 0027-0041, because revision 0001 is
``Base.metadata.create_all()`` and a fresh database already has the column.
"""

from alembic import op
import sqlalchemy as sa

revision = "0042_maintenance_page_base"
down_revision = "0042_supply_class"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "maintenance_schedules", "last_serviced_page_count"):
        op.add_column(
            "maintenance_schedules",
            sa.Column("last_serviced_page_count", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "maintenance_schedules", "last_serviced_page_count"):
        op.drop_column("maintenance_schedules", "last_serviced_page_count")
