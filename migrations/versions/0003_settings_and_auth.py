"""App settings table, subnet SNMP creds, and user SSO fields.

Written defensively (inspector-guarded) so it is safe on both an existing DB
created by the 0001 baseline and a fresh DB where create_all already built the
current schema.

Revision ID: 0003_settings_and_auth
Revises: 0002_readings_brin
Create Date: 2026-06-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_settings_and_auth"
down_revision = "0002_readings_brin"
branch_labels = None
depends_on = None


def _columns(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def _email_unique_exists(insp) -> bool:
    cols_sets = [set(uc["column_names"]) for uc in insp.get_unique_constraints("users")]
    cols_sets += [set(ix["column_names"]) for ix in insp.get_indexes("users") if ix.get("unique")]
    return {"email"} in cols_sets


def _named_index_exists(insp, table: str, name: str) -> bool:
    return any(ix.get("name") == name for ix in insp.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    subnet_cols = _columns(insp, "subnets")
    if "snmp_community" not in subnet_cols:
        op.add_column(
            "subnets",
            sa.Column("snmp_community", sa.String(120), server_default="public", nullable=False),
        )
    if "snmp_version" not in subnet_cols:
        op.add_column(
            "subnets",
            sa.Column("snmp_version", sa.String(8), server_default="2c", nullable=False),
        )

    user_cols = _columns(insp, "users")
    if "email" not in user_cols:
        op.add_column("users", sa.Column("email", sa.String(200), nullable=True))
    if "auth_provider" not in user_cols:
        op.add_column(
            "users",
            sa.Column("auth_provider", sa.String(40), server_default="local", nullable=False),
        )
    if not _email_unique_exists(insp):
        op.create_index("uq_users_email", "users", ["email"], unique=True)

    # SSO-only users have no local password.
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL")

    if not insp.has_table("app_settings"):
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(120), primary_key=True),
            sa.Column("value", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("app_settings"):
        op.drop_table("app_settings")
    # Only drop the index we created (by exact name) — on a fresh DB the unique
    # came from the model's column constraint under a different name.
    if _named_index_exists(insp, "users", "uq_users_email"):
        op.drop_index("uq_users_email", table_name="users")
    # Batch mode so SQLite can drop columns that carry a UNIQUE constraint
    # (it recreates the table); a no-op ALTER path on Postgres.
    user_drop = [c for c in ("auth_provider", "email") if c in _columns(insp, "users")]
    if user_drop:
        with op.batch_alter_table("users") as batch:
            for col in user_drop:
                batch.drop_column(col)
    subnet_drop = [c for c in ("snmp_version", "snmp_community") if c in _columns(insp, "subnets")]
    if subnet_drop:
        with op.batch_alter_table("subnets") as batch:
            for col in subnet_drop:
                batch.drop_column(col)
