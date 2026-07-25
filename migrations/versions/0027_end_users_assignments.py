"""end users, groups, and printer assignments

Revision ID: 0027_end_users_assignments
Revises: 0026_onboarding
Create Date: 2026-07-25

The identity half of print management: WHO prints, and WHICH printers they
should have. Deliberately separate from ``users`` (dashboard operators) -- see
the EndUser docstring for why folding them together breaks on the first
customer whose staff list collides with another's.

Every object is created conditionally. Revision 0001 is
``Base.metadata.create_all()``, so on a FRESH database these tables and indexes
already exist by the time this revision runs -- the models declare them. On an
UPGRADED database none of it exists yet. Both paths must converge on the same
schema, which is also why the indexes here mirror the models' ``__table_args__``
exactly: one declared in only one of the two places is silently absent from half
the installs in the field.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_end_users_assignments"
down_revision = "0026_onboarding"
branch_labels = None
depends_on = None


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def _index(bind, name: str, table: str, cols: list) -> None:
    """Create an index only if it is missing (see the module docstring)."""
    if name not in {i["name"] for i in sa.inspect(bind).get_indexes(table)}:
        op.create_index(name, table, cols)


def _bool(bind, value: bool):
    """Portable boolean literal -- SQLite has no boolean type."""
    if bind.dialect.name == "sqlite":
        return sa.text("1" if value else "0")
    return sa.text("true" if value else "false")


def upgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)

    if "end_users" not in existing:
        op.create_table(
            "end_users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("client_id", sa.Integer(), nullable=False),
            # All identity columns are nullable because the three directory
            # sources disagree about what is mandatory: Entra and Google are
            # email-centric, on-prem AD may supply a sAMAccountName and no
            # mailbox at all.
            sa.Column("email", sa.String(length=320), nullable=True),
            sa.Column("upn", sa.String(length=320), nullable=True),
            sa.Column("display_name", sa.String(length=200), nullable=True),
            sa.Column("directory_source", sa.String(length=32), nullable=False,
                      server_default="manual"),
            sa.Column("directory_id", sa.String(length=200), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False,
                      server_default=_bool(bind, True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            # Tenant-scoped uniqueness, never global: two customers each having
            # a "jsmith" is the normal case.
            sa.UniqueConstraint("client_id", "email", name="uq_end_users_client_email"),
            sa.UniqueConstraint("client_id", "directory_source", "directory_id",
                                name="uq_end_users_client_directory"),
        )
    _index(bind, "ix_end_users_client_id", "end_users", ["client_id"])
    _index(bind, "ix_end_users_client_active", "end_users", ["client_id", "active"])

    if "end_user_groups" not in existing:
        op.create_table(
            "end_user_groups",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("client_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("directory_source", sa.String(length=32), nullable=False,
                      server_default="manual"),
            sa.Column("directory_id", sa.String(length=200), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("client_id", "name",
                                name="uq_end_user_groups_client_name"),
            sa.UniqueConstraint("client_id", "directory_source", "directory_id",
                                name="uq_end_user_groups_client_directory"),
        )
    _index(bind, "ix_end_user_groups_client_id", "end_user_groups", ["client_id"])

    if "end_user_group_members" not in existing:
        op.create_table(
            "end_user_group_members",
            sa.Column("group_id", sa.Integer(), nullable=False),
            sa.Column("end_user_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["group_id"], ["end_user_groups.id"],
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["end_user_id"], ["end_users.id"],
                                    ondelete="CASCADE"),
            # The composite PK is itself the "one row per person per group"
            # guarantee -- no surrogate key to let duplicates through.
            sa.PrimaryKeyConstraint("group_id", "end_user_id"),
        )
    _index(bind, "ix_end_user_group_members_user", "end_user_group_members",
           ["end_user_id"])

    if "printer_assignments" not in existing:
        op.create_table(
            "printer_assignments",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("printer_id", sa.Integer(), nullable=False),
            sa.Column("end_user_id", sa.Integer(), nullable=True),
            sa.Column("group_id", sa.Integer(), nullable=True),
            sa.Column("is_default", sa.Boolean(), nullable=False,
                      server_default=_bool(bind, False)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_by_user_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["printer_id"], ["printers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["end_user_id"], ["end_users.id"],
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["group_id"], ["end_user_groups.id"],
                                    ondelete="CASCADE"),
            # SET NULL, not CASCADE: deleting a departed admin must not silently
            # delete every printer assignment they ever made.
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"],
                                    ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            # A row targeting both a user and a group has no defined meaning; one
            # targeting neither is an orphan the UI renders as a blank. Both are
            # cheap to produce from a form handler and expensive to find later.
            sa.CheckConstraint("(end_user_id IS NULL) <> (group_id IS NULL)",
                               name="ck_printer_assignments_one_target"),
            sa.UniqueConstraint("printer_id", "end_user_id",
                                name="uq_printer_assignments_printer_user"),
            sa.UniqueConstraint("printer_id", "group_id",
                                name="uq_printer_assignments_printer_group"),
        )
    _index(bind, "ix_printer_assignments_printer_id", "printer_assignments",
           ["printer_id"])
    _index(bind, "ix_printer_assignments_end_user_id", "printer_assignments",
           ["end_user_id"])
    _index(bind, "ix_printer_assignments_group_id", "printer_assignments",
           ["group_id"])


def downgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)
    # Child-first, so the foreign keys never block the drop.
    for table in ("printer_assignments", "end_user_group_members",
                  "end_user_groups", "end_users"):
        if table in existing:
            op.drop_table(table)
