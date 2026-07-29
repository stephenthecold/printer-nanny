"""vendor driver packages

Revision ID: 0032_driver_packages
Revises: 0031_workstation_enrollment
Create Date: 2026-07-29

Lets an operator upload a vendor driver so ``driver_required`` printers can be
provisioned instead of skipped.

Note what this table does NOT contain: the driver bytes. They live on a volume
(``central/driver_store.py``) because ``/admin/backup`` streams a ``pg_dump``
through the API, and a handful of 200 MB packages would turn a fast backup into
a slow multi-gigabyte one -- which is how operators stop taking backups. The
trade is stated in the store module and surfaced in the UI: a database restore
does not bring driver packages back.

Conditional, like 0027-0031, because revision 0001 is
``Base.metadata.create_all()``.
"""

from alembic import op
import sqlalchemy as sa

revision = "0032_driver_packages"
down_revision = "0031_workstation_enrollment"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "driver_packages"):
        op.create_table(
            "driver_packages",
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
            sa.Column("model", sa.String(length=200), nullable=False, server_default=""),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "stored_at", sa.String(length=1000), nullable=False, server_default=""
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(
            "ix_driver_packages_client_id", "driver_packages", ["client_id"]
        )
        op.create_index(
            "ix_driver_packages_client_model",
            "driver_packages",
            ["client_id", "model"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "driver_packages"):
        # The rows go; the files on the volume do not. Deliberate -- a
        # downgrade should not delete an operator's uploaded binaries, and
        # re-upgrading then re-uploading is recoverable where deletion is not.
        op.drop_table("driver_packages")
