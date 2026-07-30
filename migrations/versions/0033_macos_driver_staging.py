"""macOS vendor driver staging: per-platform driver packages, machine platform

Revision ID: 0033_macos_drivers
Revises: 0032_driver_packages

``driver_packages.platform`` is the load-bearing one. Matching is by model
substring, so a client holding both a Windows and a macOS package for one printer
would produce two equally-specific matches -- which ``driver_package_for``
correctly refuses as ambiguous, silently breaking the Windows staging that used
to work. Scoping candidates by platform first is a correctness requirement, not
an optimisation, which is why the index carries it too.

Conditional, like 0027-0032, because revision 0001 is
``Base.metadata.create_all()`` -- on a fresh database the ORM metadata has
already produced every column, index and CHECK here, so each step has to ask
before acting or a fresh install dies on "duplicate column name".
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033_macos_drivers"
down_revision = "0032_driver_packages"
branch_labels = None
depends_on = None

#: Spelled with an explicit IS NOT NULL rather than relying on IN. SQL's
#: three-valued logic makes ``NULL IN ('ppd',...)`` evaluate to NULL, not FALSE,
#: so ``(platform='macos' AND NULL)`` is NULL, the whole OR is NULL, and a CHECK
#: that evaluates to NULL **passes**. Verified: without the IS NOT NULL, a macOS
#: row carrying no kind was accepted. Same three-valued trap as the parity bug in
#: the printer-assignment CHECK.
_MACOS_KIND_CHECK = (
    "(platform = 'windows' AND macos_kind IS NULL) OR "
    "(platform = 'macos' AND macos_kind IS NOT NULL AND "
    "macos_kind IN ('ppd', 'pkg', 'system'))"
)


def _columns(bind, table: str) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if "platform" not in _columns(bind, "driver_packages"):
        with op.batch_alter_table("driver_packages") as batch:
            # server_default, not a bare default: existing rows need a value, and
            # every package uploaded before macOS support IS a Windows one.
            # Getting this wrong does not error -- it makes existing packages
            # match nothing, so Windows staging quietly stops working.
            batch.add_column(
                sa.Column(
                    "platform",
                    sa.String(length=16),
                    nullable=False,
                    server_default="windows",
                )
            )
            batch.add_column(
                sa.Column("macos_kind", sa.String(length=16), nullable=True)
            )
            batch.add_column(
                sa.Column("macos_ref", sa.String(length=1000), nullable=True)
            )

        # SEPARATE batches, deliberately. Adding columns and creating CHECK
        # constraints in one batch sends alembic down its partial-reordering
        # path, which cannot topologically sort the result and raises
        # CircularDependencyError -- after the added columns have already been
        # committed, since SQLite DDL auto-commits per statement. One batch
        # therefore leaves the database half-migrated with alembic_version
        # unmoved, which is the worst of both.
        with op.batch_alter_table("driver_packages") as batch:
            batch.create_check_constraint(
                "ck_driver_packages_platform", "platform IN ('windows', 'macos')"
            )
        with op.batch_alter_table("driver_packages") as batch:
            batch.create_check_constraint(
                "ck_driver_packages_macos_kind", _MACOS_KIND_CHECK
            )

    # Mirrored from the model's __table_args__: an index declared only here is
    # absent on a fresh install, and one declared only in the model is absent on
    # an upgraded one.
    if "ix_driver_packages_client_platform" not in _indexes(bind, "driver_packages"):
        op.create_index(
            "ix_driver_packages_client_platform",
            "driver_packages",
            ["client_id", "platform", "model"],
        )

    if "platform" not in _columns(bind, "machines"):
        with op.batch_alter_table("machines") as batch:
            batch.add_column(
                sa.Column("platform", sa.String(length=16), nullable=True)
            )


def downgrade() -> None:
    bind = op.get_bind()

    if "platform" in _columns(bind, "machines"):
        with op.batch_alter_table("machines") as batch:
            batch.drop_column("platform")

    if "ix_driver_packages_client_platform" in _indexes(bind, "driver_packages"):
        op.drop_index(
            "ix_driver_packages_client_platform", table_name="driver_packages"
        )

    if "platform" not in _columns(bind, "driver_packages"):
        return

    # copy_from, for the same reason as 0030: batch_alter_table on SQLite
    # reflects the table, and reflection picks up the CHECK constraints added
    # above -- which name the very columns being dropped, so the rebuilt table
    # fails with "no such column". Spelling the original shape out means the
    # reflected constraints are never consulted.
    driver_packages = sa.Table(
        "driver_packages",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Integer(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("driver_name", sa.String(length=255), nullable=False),
        sa.Column("inf_relpath", sa.String(length=500), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("macos_kind", sa.String(length=16), nullable=True),
        sa.Column("macos_ref", sa.String(length=1000), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("stored_at", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Index("ix_driver_packages_client_model", "client_id", "model"),
    )
    with op.batch_alter_table("driver_packages", copy_from=driver_packages) as batch:
        batch.drop_column("macos_ref")
        batch.drop_column("macos_kind")
        batch.drop_column("platform")
