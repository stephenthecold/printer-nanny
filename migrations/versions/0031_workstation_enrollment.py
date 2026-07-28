"""workstation enroll keys + per-machine API keys

Revision ID: 0031_workstation_enrollment
Revises: 0030_machines
Create Date: 2026-07-28

Gives a workstation a way to enroll itself and then authenticate as itself.

``workstation_enroll_keys`` is client-scoped and deliberately long-lived and
multi-use, which is the opposite of ``agent_claim_tokens`` and for a concrete
reason: one MSI runs on hundreds of PCs, so a single-use code cannot be baked
into it. The safety comes from narrowing what the key can do rather than from a
TTL -- it can only mint a machine, ``client_id`` is fixed at mint time so the
holder cannot choose a tenant, and ``revoked_at`` stops new enrollments without
touching machines that already hold their own keys.

``machines.api_key_hash`` is that per-machine key, SHA-256 like
``agents.api_key_hash``. Nullable: an operator may create a machine row before
the PC exists, and it gets a key when it enrolls.

Conditional, like 0027-0030, because revision 0001 is
``Base.metadata.create_all()`` -- a fresh database already has what the ORM
declares.
"""

from alembic import op
import sqlalchemy as sa

revision = "0031_workstation_enrollment"
down_revision = "0030_machines"
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

    if not _has_table(bind, "workstation_enroll_keys"):
        op.create_table(
            "workstation_enroll_keys",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "client_id",
                sa.Integer(),
                sa.ForeignKey("clients.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("key_hash", sa.String(length=128), nullable=False, unique=True),
            sa.Column("label", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_workstation_enroll_keys_client_id",
            "workstation_enroll_keys",
            ["client_id"],
        )

    if not _has_column(bind, "machines", "api_key_hash"):
        op.add_column(
            "machines", sa.Column("api_key_hash", sa.String(length=128), nullable=True)
        )


def downgrade() -> None:
    bind = op.get_bind()

    if _has_column(bind, "machines", "api_key_hash"):
        with op.batch_alter_table("machines") as batch:
            batch.drop_column("api_key_hash")

    if _has_table(bind, "workstation_enroll_keys"):
        op.drop_table("workstation_enroll_keys")
