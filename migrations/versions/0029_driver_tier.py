"""workstation driver tier on printers

Revision ID: 0029_driver_tier
Revises: 0028_directory_connections
Create Date: 2026-07-27

Records, per printer, whether a workstation queue needs a driver installed at
all -- the step that strands most setups. See models.DriverTier for why there
are five values rather than two.

Two columns for the tier, not one: ``driver_tier`` is what the agent's IPP probe
observed and ``driver_tier_override`` is what an operator decided if they
disagreed. A re-probe must be free to update what it saw without discarding a
human's decision, and the UI must be able to show both.

Columns are added conditionally for the same reason as 0027/0028: revision 0001
is ``Base.metadata.create_all()``, so a fresh database already has them by the
time this runs, while an upgraded one does not.

The enum is stored as a plain string rather than a native DB enum, matching how
the other status columns on this table are persisted -- adding a tier later then
needs no ALTER TYPE.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_driver_tier"
down_revision = "0028_directory_connections"
branch_labels = None
depends_on = None

TABLE = "printers"

COLUMNS = (
    ("driver_tier", sa.String(length=32)),
    ("driver_tier_reason", sa.String(length=400)),
    ("driver_tier_override", sa.String(length=32)),
    ("driver_probed_at", sa.DateTime(timezone=True)),
    ("ipp_endpoint", sa.String(length=300)),
    ("ipp_capabilities", sa.JSON()),
)

INDEX = "ix_printers_driver_tier"


def _existing() -> set:
    bind = op.get_bind()
    return {c["name"] for c in sa.inspect(bind).get_columns(TABLE)}


def _index_names() -> set:
    bind = op.get_bind()
    return {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}


def upgrade() -> None:
    have = _existing()
    for name, type_ in COLUMNS:
        if name not in have:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))

    # Declared in the model's mapped_column(index=True) as well, so a fresh
    # create_all() database already has it -- mirrored here for upgrades.
    if INDEX not in _index_names():
        op.create_index(INDEX, TABLE, ["driver_tier"])


def downgrade() -> None:
    if INDEX in _index_names():
        op.drop_index(INDEX, table_name=TABLE)
    have = _existing()
    for name, _type in reversed(COLUMNS):
        if name in have:
            op.drop_column(TABLE, name)
