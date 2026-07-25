"""directory sync connections

Revision ID: 0028_directory_connections
Revises: 0027_end_users_assignments
Create Date: 2026-07-25

One row per (client, provider). Secrets live in their own Fernet-encrypted
column rather than inside ``config``, because ``config`` is rendered in the
settings UI, echoed in audit detail and dumped in diagnostics -- the only
reliable way to keep a client secret out of all three is for it to live
somewhere those paths never read.

Created conditionally for the same reason as 0027: revision 0001 is
``Base.metadata.create_all()``, so a fresh database already has this table by
the time the revision runs, while an upgraded one does not.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_directory_connections"
down_revision = "0027_end_users_assignments"
branch_labels = None
depends_on = None

TABLE = "directory_connections"


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def _bool(bind, value: bool):
    if bind.dialect.name == "sqlite":
        return sa.text("1" if value else "0")
    return sa.text("true" if value else "false")


def upgrade() -> None:
    bind = op.get_bind()

    if TABLE not in _tables(bind):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("client_id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False,
                      server_default=_bool(bind, True)),
            sa.Column("config", sa.JSON(), nullable=True),
            # Fernet ciphertext (enc:v1:...), never plaintext.
            sa.Column("secret", sa.Text(), nullable=True),
            sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_ok", sa.Boolean(), nullable=True),
            sa.Column("last_error", sa.String(length=500), nullable=True),
            sa.Column("last_result", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            # Two connections to the same provider for one client would race,
            # each deactivating the users the other just created.
            sa.UniqueConstraint("client_id", "provider",
                                name="uq_directory_conn_client_provider"),
            # `manual` is a real provenance for a person, but not a thing you
            # can connect to.
            sa.CheckConstraint("provider <> 'manual'",
                               name="ck_directory_conn_not_manual"),
        )

    if "ix_directory_connections_client_id" not in {
        i["name"] for i in sa.inspect(bind).get_indexes(TABLE)
    }:
        op.create_index("ix_directory_connections_client_id", TABLE, ["client_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE in _tables(bind):
        op.drop_table(TABLE)
