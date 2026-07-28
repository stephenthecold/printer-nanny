"""machines + machine-scoped printer assignments

Revision ID: 0030_machines
Revises: 0029_driver_tier
Create Date: 2026-07-28

Adds machine-scoped assignment: "every printer on this PC, whoever is signed
in" -- shared floor terminals, kiosks, the machine by the warehouse door.

The interesting part is the CHECK. ``printer_assignments`` carried
``(end_user_id IS NULL) <> (group_id IS NULL)`` to enforce exactly one target,
and that idiom does not extend past two columns: with three, ``<>`` silently
starts meaning "an odd number of them are set". So it is rewritten as a sum,
which states the rule it actually means.

Written conditionally, like 0027-0029, because revision 0001 is
``Base.metadata.create_all()`` -- a fresh database already has everything the
ORM declares, so an unconditional add fails on exactly the installs that are
most likely to be new.
"""

from alembic import op
import sqlalchemy as sa

revision = "0030_machines"
down_revision = "0029_driver_tier"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _has_column(bind, table: str, column: str) -> bool:
    if not _has_table(bind, table):
        return False
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "machines"):
        op.create_table(
            "machines",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "client_id",
                sa.Integer(),
                sa.ForeignKey("clients.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("machine_uid", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column(
                "default_wins", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column(
                "active", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "client_id", "machine_uid", name="uq_machines_client_uid"
            ),
        )
        op.create_index("ix_machines_client_id", "machines", ["client_id"])
        op.create_index(
            "ix_machines_client_active", "machines", ["client_id", "active"]
        )

    if not _has_column(bind, "printer_assignments", "machine_id"):
        with op.batch_alter_table("printer_assignments") as batch:
            batch.add_column(sa.Column("machine_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                "fk_printer_assignments_machine",
                "machines",
                ["machine_id"],
                ["id"],
                ondelete="CASCADE",
            )
        op.create_index(
            "ix_printer_assignments_machine_id", "printer_assignments", ["machine_id"]
        )

        # Replace the two-column CHECK with one that means "exactly one of
        # three". batch_alter_table is what makes this work on SQLite, which
        # cannot drop a constraint and rebuilds the table instead.
        with op.batch_alter_table("printer_assignments") as batch:
            try:
                batch.drop_constraint(
                    "ck_printer_assignments_one_target", type_="check"
                )
            except Exception:
                # Older SQLite installs may carry the constraint unnamed, in
                # which case there is nothing to drop and the create below is
                # still the rule that governs.
                pass
            batch.create_check_constraint(
                "ck_printer_assignments_one_target",
                "((CASE WHEN end_user_id IS NULL THEN 0 ELSE 1 END) + "
                "(CASE WHEN group_id IS NULL THEN 0 ELSE 1 END) + "
                "(CASE WHEN machine_id IS NULL THEN 0 ELSE 1 END)) = 1",
            )
            batch.create_unique_constraint(
                "uq_printer_assignments_printer_machine", ["printer_id", "machine_id"]
            )


def downgrade() -> None:
    bind = op.get_bind()

    if _has_column(bind, "printer_assignments", "machine_id"):
        # Rows targeting a machine have no representation in the old schema, so
        # they go rather than silently becoming orphans the old CHECK forbids.
        op.execute(sa.text("DELETE FROM printer_assignments WHERE machine_id IS NOT NULL"))

        # copy_from is load-bearing on SQLite, which cannot drop a column and so
        # rebuilds the table. Left to reflect, batch_alter_table carries the
        # CURRENT 3-way CHECK across -- a CHECK that names machine_id -- and then
        # drops that column, producing "no such column: machine_id" from a
        # constraint it wrote itself. Describing the table explicitly, without
        # that CHECK, is what makes the rebuild well defined.
        meta = sa.MetaData()
        current = sa.Table(
            "printer_assignments",
            meta,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("printer_id", sa.Integer(), nullable=False),
            sa.Column("end_user_id", sa.Integer(), nullable=True),
            sa.Column("group_id", sa.Integer(), nullable=True),
            sa.Column("machine_id", sa.Integer(), nullable=True),
            sa.Column("is_default", sa.Boolean(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        )
        with op.batch_alter_table("printer_assignments", copy_from=current) as batch:
            batch.drop_column("machine_id")
            batch.create_check_constraint(
                "ck_printer_assignments_one_target",
                "(end_user_id IS NULL) <> (group_id IS NULL)",
            )

    if _has_table(bind, "machines"):
        op.drop_table("machines")
