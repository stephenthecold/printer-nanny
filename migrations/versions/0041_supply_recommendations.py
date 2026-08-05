"""supplies.pages_to_empty for reorder recommendations

Revision ID: 0041_supply_recommendations
Revises: 0037_occurrence_alerts
Create Date: 2026-08-03

Reorder recommendations (central.reorder) trigger on supply level OR
days-remaining OR pages-remaining. The first two are already persisted; this
adds the third.

It is a stored MEASUREMENT, not a stored recommendation. The recommendation
itself is computed on read from level + days + pages against the operator's
thresholds and is persisted nowhere -- there is no recommendation table, no
order state and no lifecycle, by decision. What is stored here is the same
class of thing as ``days_to_empty`` beside it: the output of a regression over
30 days of readings, cached on the row so a page render does not have to re-fit
it. The worker's existing forecast pass writes it off rows it already loads,
so the column costs no additional query anywhere.

Added conditionally for the same reason as 0027-0033: revision 0001 is
``Base.metadata.create_all()``, so a fresh database already has this column from
the ORM metadata by the time this migration runs, while an upgraded one does not.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0041_supply_recommendations"
down_revision = "0037_occurrence_alerts"
branch_labels = None
depends_on = None

TABLE = "supplies"
COLUMN = "pages_to_empty"


def _existing() -> set:
    bind = op.get_bind()
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def upgrade() -> None:
    if COLUMN not in _existing():
        # Nullable with no default: NULL means "no trustworthy estimate", which
        # is a different statement from 0, and backfilling it would be inventing
        # a forecast for a cartridge nobody has measured yet. The worker fills it
        # in on its next pass.
        op.add_column(TABLE, sa.Column(COLUMN, sa.Float(), nullable=True))


def downgrade() -> None:
    if COLUMN in _existing():
        op.drop_column(TABLE, COLUMN)
