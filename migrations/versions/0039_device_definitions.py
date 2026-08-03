"""server-pushed device/model definitions

Revision ID: 0039_device_definitions
Revises: 0038_client_branding
Create Date: 2026-08-03

Lets a new printer model be supported by adding a row instead of shipping an
agent release. The row holds the *validated, normalised* definition produced by
``central.device_definitions.validate_definition`` -- never an operator's raw
text -- and the agent re-validates it on receipt and on every load of its local
cache.

Two things about the indexes are load-bearing rather than tidy:

* ``uq_device_definitions_key_scope`` does NOT cover the common case. SQL treats
  NULLs as distinct in a UNIQUE constraint, and ``client_id IS NULL`` is exactly
  the *global* definition every agent receives -- so that constraint would allow
  any number of global rows sharing a key. ``uq_device_definitions_global_key``
  is a partial unique index over ``client_id IS NULL``, which is what actually
  enforces it. Both SQLite and Postgres support partial indexes.
* Both are declared in the model's ``__table_args__`` as well, because revision
  0001 is ``Base.metadata.create_all()`` -- an index that lives only in a
  migration is silently absent on every fresh install.

Conditional, like 0027-0038, because of that same ``create_all`` at 0001.
"""

from alembic import op
import sqlalchemy as sa

revision = "0039_device_definitions"
down_revision = "0035_event_bus"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _has_index(bind, table: str, name: str) -> bool:
    return any(ix["name"] == name for ix in sa.inspect(bind).get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "device_definitions"):
        op.create_table(
            "device_definitions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column(
                "client_id",
                sa.Integer(),
                sa.ForeignKey("clients.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column(
                "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("spec", sa.JSON(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.UniqueConstraint(
                "key", "client_id", name="uq_device_definitions_key_scope"
            ),
        )

    if not _has_index(bind, "device_definitions", "ix_device_definitions_key"):
        op.create_index("ix_device_definitions_key", "device_definitions", ["key"])
    if not _has_index(bind, "device_definitions", "ix_device_definitions_client_id"):
        op.create_index(
            "ix_device_definitions_client_id", "device_definitions", ["client_id"]
        )
    if not _has_index(bind, "device_definitions", "ix_device_definitions_enabled"):
        op.create_index(
            "ix_device_definitions_enabled", "device_definitions", ["enabled"]
        )
    if not _has_index(bind, "device_definitions", "uq_device_definitions_global_key"):
        # The one that actually stops two global definitions sharing a key.
        op.create_index(
            "uq_device_definitions_global_key",
            "device_definitions",
            ["key"],
            unique=True,
            sqlite_where=sa.text("client_id IS NULL"),
            postgresql_where=sa.text("client_id IS NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "device_definitions"):
        op.drop_table("device_definitions")
