"""readings retention: daily rollup table + the (printer_id, ts) index

Revision ID: 0034_readings_retention
Revises: 0038_client_branding
Create Date: 2026-08-03

``readings`` had no retention at all -- ~52M rows/year at 500 printers on the
default 300s poll. This adds the table that makes retention possible without
losing history: one ``reading_rollups`` row per printer per UTC day, kept
forever, holding the page/mono/colour meters at both ends of the day plus the
end-of-day supply snapshot. ``central.retention`` writes them and owns the
rules; deletion of raw rows is opt-in and off by default.

It also adds ``ix_readings_printer_ts``. That index is not new work for this
feature so much as a long-standing omission it surfaced: every hot query on the
table is "one printer, a time range" -- the forecast pass, ``supply_runway``,
page-count trends, and now the retention pass -- and only two single-column
indexes existed, so Postgres read every reading a printer had ever produced and
filtered by ``ts``. Confirmed by EXPLAIN on Postgres 16 before and after.

Conditional, like 0027-0033, because revision 0001 is
``Base.metadata.create_all()`` -- on a fresh database the ORM metadata has
already produced this table and both of its indexes, so every step has to ask
before acting or a fresh install dies on "table already exists".
"""

from alembic import op
import sqlalchemy as sa

revision = "0034_readings_retention"
down_revision = "0038_client_branding"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _has_index(bind, table: str, name: str) -> bool:
    return any(ix["name"] == name for ix in sa.inspect(bind).get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "reading_rollups"):
        op.create_table(
            "reading_rollups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "printer_id",
                sa.Integer(),
                sa.ForeignKey("printers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column(
                "readings_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("first_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_ts", sa.DateTime(timezone=True), nullable=False),
            # Both ends of each meter: a cumulative counter answers "ever", and
            # billing asks "during this period", which is a difference.
            sa.Column("page_count_start", sa.Integer(), nullable=True),
            sa.Column("page_count", sa.Integer(), nullable=True),
            sa.Column("mono_count_start", sa.Integer(), nullable=True),
            sa.Column("mono_count", sa.Integer(), nullable=True),
            sa.Column("color_count_start", sa.Integer(), nullable=True),
            sa.Column("color_count", sa.Integer(), nullable=True),
            sa.Column("supply_snapshot", sa.JSON(), nullable=True),
            sa.Column(
                "raw_pruned", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            # NOT NULL to match the model. Inside create_table, so there are no
            # existing rows to violate it. The ORM always supplies the value, so
            # nothing changes at runtime -- but an UPGRADED install was missing a
            # constraint the model documents, and the drift test cannot see that
            # because it compares two FRESH databases, where 0001's create_all
            # builds both.
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            # The uniqueness IS the idempotency: rolling up (printer, day) has to
            # be safe to re-run after a crash, and a duplicate row would
            # double-count every period that summed it.
            sa.UniqueConstraint(
                "printer_id", "day", name="uq_reading_rollup_printer_day"
            ),
        )
        op.create_index("ix_reading_rollups_day", "reading_rollups", ["day"])

    # Mirrored from Reading.__table_args__. Declared in both places on purpose:
    # 0001 is create_all(), so a fresh install gets it from the model and an
    # existing install gets it from here.
    if not _has_index(bind, "readings", "ix_readings_printer_ts"):
        op.create_index(
            "ix_readings_printer_ts", "readings", ["printer_id", "ts"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_index(bind, "readings", "ix_readings_printer_ts"):
        op.drop_index("ix_readings_printer_ts", table_name="readings")
    if _has_table(bind, "reading_rollups"):
        # Dropping this destroys the ONLY remaining copy of every day whose raw
        # readings were pruned. There is no recovery path, so a downgrade past
        # this revision on an installation that has ever enabled
        # `retention.delete_enabled` is a data-loss event, not a rollback.
        op.drop_table("reading_rollups")
