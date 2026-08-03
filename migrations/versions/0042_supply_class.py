"""Record what a supply's level MEANS: prtMarkerSuppliesClass.

Revision ID: 0042_supply_class
Revises: 0040_collector_redundancy

Adds ``supplies.supply_class`` -- ``"consumed"`` / ``"receptacle"`` / ``"other"``
/ NULL -- taken verbatim from RFC 3805's ``prtMarkerSuppliesClass``.

WHY A COLUMN RATHER THAN A DERIVATION. The class decides whether ``level_pct``
is read as "how much is left" or "how full the container is". A waste box
reporting 5 is a nearly EMPTY box -- the healthiest it ever is -- and was being
counted as a low supply and recommended for reorder. Only the device can settle
which reading applies for the cases our supply *type* cannot see (a hole-punch
chip box, a staple waste bin), so the answer has to travel with the row.

NO BACKFILL, AND NONE IS POSSIBLE OR NEEDED. Every existing row lands on NULL,
which means "the device did not report it" -- honestly, because we never asked
for the column before this shipped. ``central.supplies.is_receptacle`` falls back
to the supply type (our ``waste`` type is only ever produced from wasteToner(4)
or from "waste"/"toner collection" in the description, all of which ARE
receptacles), so history is reinterpreted correctly without touching a byte.

Nothing stored changes meaning: a device reporting a receptacle has always been
reporting fullness and ``parse_supply_level`` has always stored what it said.
Only our reading of it was wrong. That is what makes this a read rule rather than
a data migration -- there is no half-migrated state to end up in, and a rollback
loses only the class, never a level.

Conditional, like 0027-0040, because revision 0001 is
``Base.metadata.create_all()``: on a fresh database the ORM metadata has already
produced this column, so the step must ask before acting or a fresh install dies
on "duplicate column name".

Plain ``ADD COLUMN`` / ``DROP COLUMN`` rather than ``batch_alter_table``: batch
mode rebuilds the table on SQLite by reflecting it, and reflection does not carry
``ON DELETE`` back -- that is how 0040 silently downgraded ``subnets.site_id``.
``supplies.printer_id`` is ON DELETE CASCADE and must survive both directions.
SQLite has supported ``DROP COLUMN`` since 3.35 (2021-03); older builds raise
rather than silently rebuild, which is the outcome to want here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_supply_class"
down_revision = "0040_collector_redundancy"
branch_labels = None
depends_on = None

_TABLE = "supplies"
_COLUMN = "supply_class"


def _columns(bind, table: str) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _COLUMN not in _columns(bind, _TABLE):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=20), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _COLUMN in _columns(bind, _TABLE):
        op.drop_column(_TABLE, _COLUMN)
