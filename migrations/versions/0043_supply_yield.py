"""Yield-gap detection: cartridge cycles, and operator-entered expected yields.

Revision ID: 0043_supply_yield
Revises: 0042_remote_hands

Two tables.

``supply_cycles`` -- one row per cartridge life per supply slot: when it was
fitted, when it was replaced, the levels at each end, and the pages printed in
between. This is a MEASUREMENT table, not a verdict table. Nothing here records
"this printer is using non-OEM cartridges"; that judgement is computed on read
from these rows against the operator's threshold and is stored nowhere, exactly
as the reorder recommendation is (``central/reorder.py`` documents why).

It has to be persisted because it cannot be recomputed: a drum lasts a year and
``retention.raw_days`` is 90, so by the time a cycle closes the readings that
opened it may be gone. ``central.supply_yield`` reads the daily rollup for
anything older than the raw window, which is precisely the use
``ReadingRollup.supply_snapshot`` was carried for.

``supply_yield_expectations`` -- what a cartridge for a model is rated to yield,
typed in by an operator off the datasheet. Global rather than per-client: rated
yield is a property of the hardware. Matched as a case-insensitive substring of
``printers.model``, longest tag wins, exact tie refused -- the driver-package
rule, for the same reason (SNMP model strings vary by firmware).

Conditional, like 0027-0042, because revision 0001 is
``Base.metadata.create_all()``: on a fresh database the ORM metadata has already
built both tables and both indexes by the time this runs, so each step asks
before acting or a fresh install dies on "table already exists".

Both indexes are declared in ``__table_args__`` as well as here. An index that
exists only in a migration is silently absent on every fresh install, which is
the trap this project has documented since 0034.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043_supply_yield"
down_revision = "0042_remote_hands"
branch_labels = None
depends_on = None

CYCLES = "supply_cycles"
EXPECTATIONS = "supply_yield_expectations"

#: Mirrors ``SupplyCycle.__table_args__``.
CYCLE_INDEXES = (
    ("ix_supply_cycles_printer_slot", ["printer_id", "supply_type", "color"]),
    ("ix_supply_cycles_ended", ["ended_at"]),
    # SQLAlchemy emits this one from ``index=True`` on the column rather than
    # from __table_args__; it still has to exist on an upgraded database.
    ("ix_supply_cycles_printer_id", ["printer_id"]),
)


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indexes(table: str) -> set:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    tables = _tables()

    if CYCLES not in tables:
        op.create_table(
            CYCLES,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "printer_id",
                sa.Integer(),
                sa.ForeignKey("printers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("supply_type", sa.String(length=40), nullable=False),
            # "" and never NULL: NULLs compare distinct in SQL, so a NULL colour
            # would make "the open cycle for this slot" unfindable and a second
            # one would be opened on every pass.
            sa.Column(
                "color", sa.String(length=40), nullable=False, server_default=""
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("start_level_pct", sa.Float(), nullable=False),
            sa.Column("end_level_pct", sa.Float(), nullable=False),
            sa.Column("min_level_pct", sa.Float(), nullable=False),
            sa.Column("start_page_count", sa.Integer(), nullable=True),
            sa.Column("end_page_count", sa.Integer(), nullable=True),
            sa.Column("pages", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "readings_count", sa.Integer(), nullable=False, server_default="1"
            ),
            # sa.false(), not text("0"): a numeric default on a Boolean renders
            # unquoted and Postgres refuses it outright (DatatypeMismatch),
            # while SQLite accepts it -- so a whole-suite SQLite run would pass
            # over a schema that cannot be created in production.
            sa.Column(
                "complete", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    have = _indexes(CYCLES)
    for name, cols in CYCLE_INDEXES:
        if name not in have:
            op.create_index(name, CYCLES, cols)

    if EXPECTATIONS not in tables:
        op.create_table(
            EXPECTATIONS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("model_tag", sa.String(length=200), nullable=False),
            sa.Column("supply_type", sa.String(length=40), nullable=False),
            sa.Column(
                "color", sa.String(length=40), nullable=False, server_default=""
            ),
            sa.Column("expected_pages", sa.Integer(), nullable=False),
            sa.Column("note", sa.String(length=300), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "model_tag",
                "supply_type",
                "color",
                name="uq_supply_yield_expectation",
            ),
        )


def downgrade() -> None:
    tables = _tables()
    if EXPECTATIONS in tables:
        op.drop_table(EXPECTATIONS)
    if CYCLES in tables:
        for name, _cols in CYCLE_INDEXES:
            if name in _indexes(CYCLES):
                op.drop_index(name, table_name=CYCLES)
        op.drop_table(CYCLES)
