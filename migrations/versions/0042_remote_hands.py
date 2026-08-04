"""Remote hands: EWS proxy requests, and per-device write capability.

Revision ID: 0042_remote_hands
Revises: 0040_collector_redundancy

Two things land here.

``remote_requests`` -- one row per operator-initiated remote action (fetch a
page, probe writability, perform a named write) and whatever came back. It holds
the captured device body, which is why every text column here is bounded and why
``body`` is the only unbounded one: it is capped at 256 KB on the agent as it is
read, and again by the ingest route before it is stored, so the column type is
the least interesting of the three limits.

Four columns on ``printers`` -- the recorded capability. ``remote_capability``
defaults to ``unknown``, which behaves exactly like ``read_only``: every
existing printer therefore lands in the state this feature ships in, and no
device becomes writable by being upgraded to. ``remote_write_disabled`` is the
operator's own pin, and it exists in only one direction on purpose (see
``central/remote.py``): an operator may pin a device read-only and may not pin
one writable.

Conditional, like 0027-0040, because revision 0001 is
``Base.metadata.create_all()`` -- on a fresh database the ORM metadata has
already produced the table, the columns and the index, so each step asks before
acting or a fresh install dies on "table already exists".

The server defaults on the two non-nullable ``printers`` columns are what makes
this safe on a table with rows in it: without them the ADD COLUMN would either
fail outright (Postgres, NOT NULL with no default) or leave NULLs that the
enum-backed attribute cannot read. They are written explicitly here and the ORM
carries the same values as Python-side defaults; the pair is what keeps a fresh
schema and an upgraded one identical.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042_remote_hands"
down_revision = "0042_auth_hardening"
branch_labels = None
depends_on = None

TABLE = "remote_requests"
PRINTERS = "printers"

#: (name, type, nullable, server_default) for the columns added to ``printers``.
PRINTER_COLUMNS = (
    ("remote_capability", sa.String(length=32), False, "unknown"),
    ("remote_capability_at", sa.DateTime(timezone=True), True, None),
    ("remote_capability_detail", sa.String(length=400), True, None),
    ("remote_write_disabled", sa.Boolean(), False, sa.false()),
)

#: Mirrors ``RemoteRequest.__table_args__``. The panel reads "this printer's
#: requests, newest first" and the rate limiter reads "its most recent one";
#: both are one printer over an ordering, which is exactly this index.
COMPOSITE_INDEX = ("ix_remote_requests_printer_created", ["printer_id", "created_at"])

#: The ``index=True`` columns on the model, mirrored. An index declared only in
#: the model is absent on an upgraded database; one declared only here is absent
#: on a fresh one.
SINGLE_INDEXES = (
    ("ix_remote_requests_printer_id", ["printer_id"]),
    ("ix_remote_requests_agent_id", ["agent_id"]),
    ("ix_remote_requests_status", ["status"]),
)


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    if TABLE not in _tables():
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "printer_id",
                sa.Integer(),
                sa.ForeignKey("printers.id", ondelete="CASCADE"),
                nullable=False,
            ),
            # SET NULL, not CASCADE: deleting an agent must not erase the record
            # that somebody restarted a customer's printer through it.
            sa.Column(
                "agent_id",
                sa.Integer(),
                sa.ForeignKey("agents.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "command_id",
                sa.Integer(),
                sa.ForeignKey("commands.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("scheme", sa.String(length=8), nullable=True),
            sa.Column("port", sa.Integer(), nullable=True),
            sa.Column("path", sa.String(length=600), nullable=True),
            sa.Column("op", sa.String(length=40), nullable=True),
            sa.Column("op_value", sa.String(length=400), nullable=True),
            sa.Column(
                "requested_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("requested_by", sa.String(length=120), nullable=True),
            sa.Column("http_status", sa.Integer(), nullable=True),
            sa.Column("content_type", sa.String(length=120), nullable=True),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("body_bytes", sa.Integer(), nullable=True),
            sa.Column("truncated", sa.Boolean(), nullable=True),
            sa.Column("verified", sa.Boolean(), nullable=True),
            sa.Column("error", sa.String(length=600), nullable=True),
            sa.Column("detail", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )

    have_idx = _indexes(TABLE)
    for name, cols in SINGLE_INDEXES + (COMPOSITE_INDEX,):
        if name not in have_idx:
            op.create_index(name, TABLE, cols)

    have = _columns(PRINTERS)
    for name, type_, nullable, default in PRINTER_COLUMNS:
        if name in have:
            continue
        op.add_column(
            PRINTERS,
            sa.Column(name, type_, nullable=nullable, server_default=default),
        )


def downgrade() -> None:
    have = _columns(PRINTERS)
    for name, _type, _nullable, _default in reversed(PRINTER_COLUMNS):
        if name in have:
            op.drop_column(PRINTERS, name)

    if TABLE in _tables():
        have_idx = _indexes(TABLE)
        for name, _cols in (COMPOSITE_INDEX,) + SINGLE_INDEXES:
            if name in have_idx:
                op.drop_index(name, table_name=TABLE)
        op.drop_table(TABLE)
