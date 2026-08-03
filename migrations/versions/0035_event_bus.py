"""typed, signed outbound event bus

Revision ID: 0035_event_bus
Revises: 0034_readings_retention
Create Date: 2026-08-03

Three tables: subscriptions (configuration), outbound events (facts), and one
delivery row per (event, subscription) attempt.

``event_subscriptions.secret`` is Fernet ciphertext, in its own column for the
same reason ``directory_connections.secret`` is: the URL, the name and the type
filter are rendered in the UI and echoed in audit detail, and the only reliable
way to keep the signing key out of those paths is for it to live somewhere they
never read.

Created conditionally, like 0027/0028: revision 0001 is
``Base.metadata.create_all()``, so a fresh database already has these tables by
the time this revision runs while an upgraded one does not. Every index declared
here is also declared in the model's ``__table_args__`` / column ``index=True``,
because create_all is what builds a fresh install and an index declared only in
a migration is silently absent there.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_event_bus"
down_revision = "0034_readings_retention"
branch_labels = None
depends_on = None

SUBSCRIPTIONS = "event_subscriptions"
EVENTS = "outbound_events"
DELIVERIES = "event_deliveries"


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def _indexes(bind, table: str) -> set:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def _bool(bind, value: bool):
    if bind.dialect.name == "sqlite":
        return sa.text("1" if value else "0")
    return sa.text("true" if value else "false")


def upgrade() -> None:
    bind = op.get_bind()
    existing = _tables(bind)

    if SUBSCRIPTIONS not in existing:
        op.create_table(
            SUBSCRIPTIONS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            # NULL == global (every tenant). Non-NULL scopes every delivery to
            # that one client; the invariant is enforced in events.emit because
            # it spans this table and outbound_events.
            sa.Column("client_id", sa.Integer(), nullable=True),
            sa.Column("url", sa.String(length=500), nullable=False),
            # Fernet ciphertext (enc:v1:...), never plaintext.
            sa.Column("secret", sa.Text(), nullable=False),
            sa.Column("event_types", sa.JSON(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False,
                      server_default=_bool(bind, True)),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_ok", sa.Boolean(), nullable=True),
            sa.Column("last_error", sa.String(length=500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    if EVENTS not in existing:
        op.create_table(
            EVENTS,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("uid", sa.String(length=64), nullable=False),
            sa.Column("type", sa.String(length=64), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False,
                      server_default=sa.text("1")),
            sa.Column("idempotency_key", sa.String(length=200), nullable=False),
            sa.Column("client_id", sa.Integer(), nullable=True),
            sa.Column("data", sa.JSON(), nullable=True),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            # Uniqueness on uid / idempotency_key is carried by the UNIQUE
            # INDEXes below, not by a named constraint here. That is not a style
            # choice: the model declares them as `unique=True, index=True`, which
            # create_all renders as exactly those indexes -- and create_all is
            # what builds a fresh install (revision 0001). A named constraint
            # here plus a plain index would leave upgraded and fresh databases
            # with different schemas for the same rule.
        )

    if DELIVERIES not in existing:
        op.create_table(
            DELIVERIES,
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("event_id", sa.Integer(), nullable=False),
            sa.Column("subscription_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False,
                      server_default=sa.text("0")),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("response_status", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["event_id"], [EVENTS + ".id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["subscription_id"], [SUBSCRIPTIONS + ".id"],
                                    ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_id", "subscription_id",
                                name="uq_event_delivery_event_subscription"),
        )

    # Every index here is also declared on the model (column ``index=True`` or
    # ``__table_args__``), because revision 0001 is ``create_all`` -- an index
    # that lives only in a migration is silently absent on a fresh install.
    # ``unique`` mirrors the model exactly for the same reason.
    wanted = (
        (SUBSCRIPTIONS, "ix_event_subscriptions_client_id", ["client_id"], False),
        # The de-duplication contract, held by the database rather than by a
        # read-then-write: a worker cycle that re-evaluates an unchanged
        # condition must not be able to emit the same fact twice.
        (EVENTS, "ix_outbound_events_uid", ["uid"], True),
        (EVENTS, "ix_outbound_events_idempotency_key", ["idempotency_key"], True),
        (EVENTS, "ix_outbound_events_type", ["type"], False),
        (EVENTS, "ix_outbound_events_client_id", ["client_id"], False),
        # The prune sweep and the events page both read newest-first.
        (EVENTS, "ix_outbound_events_created_at", ["created_at"], False),
        (DELIVERIES, "ix_event_deliveries_event_id", ["event_id"], False),
        (DELIVERIES, "ix_event_deliveries_subscription_id", ["subscription_id"], False),
        # The sweeper's predicate: status IN (...) AND next_attempt_at <= now.
        (DELIVERIES, "ix_event_deliveries_status", ["status"], False),
        (DELIVERIES, "ix_event_deliveries_next_attempt_at", ["next_attempt_at"], False),
    )
    for table, name, columns, unique in wanted:
        if name not in _indexes(bind, table):
            op.create_index(name, table, columns, unique=unique)


def downgrade() -> None:
    """Drop only what this revision creates, children first.

    Deliberately not a blanket drop of anything event-shaped: revisions in this
    tree have twice dropped tables their upgrade did not create, and on a
    pre-Alembic install that is data loss with a documented trigger.
    """
    bind = op.get_bind()
    existing = _tables(bind)
    for table in (DELIVERIES, EVENTS, SUBSCRIPTIONS):
        if table in existing:
            op.drop_table(table)
