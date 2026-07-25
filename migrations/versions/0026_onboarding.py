"""Add trusted subnets and agent claim tokens (zero-touch onboarding).

Two additions, both in service of getting a new site monitored without a human
clicking through four forms and then approving printers one at a time.

``subnets.trusted`` gates auto-approval of discovered devices. It is a property
of the subnet rather than a global switch because the subnet row is the existing
deliberate act: somebody had to type that CIDR and its SNMP credentials to
create it. Marking it trusted re-uses that decision instead of inventing new
trust, and a device found on a CIDR nobody enrolled still queues for review.
Defaults false, so applying this migration changes no existing behaviour.

``agent_claim_tokens`` lets an agent enroll itself with a short-lived, single-use
code instead of the operator carrying a long-lived API key to the site by hand.
The code is stored hashed (SHA-256, mirroring ``agents.api_key_hash``) so a
database dump yields no working enrollment, and ``site_id`` is fixed by the
operator at mint time -- a bearer credential must never let its holder choose
which tenant it lands in.

Data safety: purely additive. One new table, one new column with a server
default so existing rows need no rewrite, and no change to any existing column.
Both guarded, so a re-run is a no-op.

Revision ID: 0026_onboarding
Revises: 0025_suppression_windows
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0026_onboarding"
down_revision = "0025_suppression_windows"
branch_labels = None
depends_on = None

TABLE = "agent_claim_tokens"
SUBNET_TRUSTED = "trusted"
INDEX = "ix_agent_claim_token_expires"


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def _subnet_columns(bind) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns("subnets")}


def _false(bind):
    """Portable SQL false literal -- SQLite has no boolean type."""
    return sa.text("0" if bind.dialect.name == "sqlite" else "false")


def upgrade() -> None:
    bind = op.get_bind()

    if SUBNET_TRUSTED not in _subnet_columns(bind):
        # NOT NULL with a server default so existing rows are backfilled to
        # "not trusted" in place. Defaulting the other way would silently make
        # every already-enrolled subnet auto-approve on the next sweep, which is
        # precisely the surprise this column exists to prevent.
        op.add_column(
            "subnets",
            sa.Column(
                SUBNET_TRUSTED,
                sa.Boolean(),
                nullable=False,
                server_default=_false(bind),
            ),
        )

    if TABLE not in _tables(bind):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            # SHA-256 hex of the claim code. Unique so a hash collision or a
            # duplicate insert surfaces as an integrity error rather than two
            # rows racing the same redemption.
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column(
                "site_id",
                sa.Integer(),
                sa.ForeignKey("sites.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("agent_name", sa.String(length=200), nullable=True),
            sa.Column(
                "created_by_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            # NULL == unredeemed. Redemption is a conditional UPDATE on this
            # column, so it is the single-use interlock, not just a record.
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "used_by_agent_id",
                sa.Integer(),
                sa.ForeignKey("agents.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        # Mirrors AgentClaimToken.__table_args__ / the mapped_column index flags
        # for the upgrade-an-existing-DB path. On a fresh install this whole
        # branch is skipped (revision 0001 builds from ORM metadata, indexes
        # included), which is exactly why they are declared on the model too.
        op.create_index(INDEX, TABLE, ["expires_at"])
        op.create_index(f"ix_{TABLE}_token_hash", TABLE, ["token_hash"], unique=True)
        op.create_index(f"ix_{TABLE}_site_id", TABLE, ["site_id"])


def downgrade() -> None:
    bind = op.get_bind()

    # Guarded so a downgrade against a database that never had the table (a
    # pre-Alembic install stamped forward) doesn't fail, and so this revision
    # only ever removes what it created.
    if TABLE in _tables(bind):
        # The indexes may legitimately not exist: on a fresh install revision
        # 0001 builds the schema from ORM metadata and the create_table branch
        # above never runs. Dropping unconditionally would raise "no such index"
        # and abort the downgrade before drop_table, stranding a
        # _alembic_tmp_* table behind it.
        existing = {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}
        for name in (INDEX, f"ix_{TABLE}_token_hash", f"ix_{TABLE}_site_id"):
            if name in existing:
                op.drop_index(name, table_name=TABLE)
        op.drop_table(TABLE)

    if SUBNET_TRUSTED in _subnet_columns(bind):
        with op.batch_alter_table("subnets", schema=None) as batch:
            batch.drop_column(SUBNET_TRUSTED)
