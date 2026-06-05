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
    if _email_unique_exists(insp):
        op.drop_index("uq_users_email", table_name="users")
    for col in ("auth_provider", "email"):
        if col in _columns(insp, "users"):
            op.drop_column("users", col)
    for col in ("snmp_version", "snmp_community"):
        if col in _columns(insp, "subnets"):
            op.drop_column("subnets", col)
