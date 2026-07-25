"""Add suppression windows (quiet hours + maintenance) and per-client timezone.

Alert suppression, quiet hours and maintenance windows did not exist at all
(`docs/audit-2026-07.md`: *"none exist (exhaustive grep). This is the loudest
complaint in the field."*). Every alert notified immediately, at any hour, with no
way to say "not between 18:00 and 07:00" or "we're swapping the fleet Saturday".

Two additions:

``suppression_windows`` carries both shapes in one table -- recurring quiet hours
(a weekday mask plus local wall-clock minutes, wrap-aware) and one-off dated
maintenance ranges (absolute UTC). They share scope, break-through floor and
action, so splitting them would duplicate all three. See the model docstring for
which columns are authoritative per ``kind``.

``clients.timezone`` because quiet hours are meaningless without one: "after
18:00" means the *client's* 18:00, and an MSP's clients do not share a zone.
NULL means "use the global alerts.default_timezone", so existing rows need no
backfill and a single-region install never has to touch it.

Data safety: purely additive. One new table, one new nullable column, no rewrite
of existing rows and no change to any existing column. Both guarded, so a re-run
is a no-op.

Revision ID: 0025_suppression_windows
Revises: 0024_alert_flap_count
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0025_suppression_windows"
down_revision = "0024_alert_flap_count"
branch_labels = None
depends_on = None

TABLE = "suppression_windows"
CLIENT_TZ = "timezone"
INDEX = "ix_suppression_enabled_scope"


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def _client_columns(bind) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns("clients")}


def upgrade() -> None:
    bind = op.get_bind()

    if TABLE not in _tables(bind):
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=200), nullable=False),
            # Enums are portable VARCHARs here (see models._enum), so no native
            # DB enum type is created and adding a member later needs no DDL.
            sa.Column("kind", sa.String(length=32), nullable=False,
                      server_default="quiet_hours"),
            sa.Column("action", sa.String(length=32), nullable=False,
                      server_default="defer"),
            sa.Column("scope", sa.String(length=32), nullable=False,
                      server_default="global"),
            sa.Column("scope_id", sa.Integer(), nullable=True),
            sa.Column("min_severity_breakthrough", sa.String(length=32),
                      nullable=False, server_default="critical"),
            # False == total silence. A separate flag because "critical" is the
            # top of EventSeverity, so the floor alone can only ever loosen a
            # window, never make it fully quiet. Defaults True (safe).
            sa.Column("allow_breakthrough", sa.Boolean(), nullable=False,
                      server_default=sa.text("1" if bind.dialect.name == "sqlite" else "true")),
            sa.Column("start_minute", sa.Integer(), nullable=True),
            sa.Column("end_minute", sa.Integer(), nullable=True),
            sa.Column("weekdays", sa.JSON(), nullable=True),
            sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False,
                      server_default=sa.text("1" if bind.dialect.name == "sqlite" else "true")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
        )
        # Mirrors SuppressionWindow.__table_args__ for the upgrade-an-existing-DB
        # path. On a fresh install this whole branch is skipped (revision 0001
        # already built the table from ORM metadata, index included), which is
        # exactly why the index has to be declared on the model too.
        op.create_index(INDEX, TABLE, ["enabled", "scope", "scope_id"])

    if CLIENT_TZ not in _client_columns(bind):
        # Nullable with no default: NULL is meaningful (= inherit the global
        # setting), so there is deliberately nothing to backfill.
        op.add_column("clients", sa.Column(CLIENT_TZ, sa.String(length=64), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()

    if CLIENT_TZ in _client_columns(bind):
        with op.batch_alter_table("clients", schema=None) as batch:
            batch.drop_column(CLIENT_TZ)

    # Guarded so a downgrade against a database that never had the table (a
    # pre-Alembic install stamped forward) doesn't fail, and so this revision
    # only ever removes what it created.
    if TABLE in _tables(bind):
        # The index may legitimately not exist: on a fresh install revision 0001
        # builds the schema from ORM metadata and this migration's create_table
        # branch never runs, so dropping unconditionally raised "no such index"
        # and aborted the downgrade before drop_table.
        if INDEX in {i["name"] for i in sa.inspect(bind).get_indexes(TABLE)}:
            op.drop_index(INDEX, table_name=TABLE)
        op.drop_table(TABLE)
