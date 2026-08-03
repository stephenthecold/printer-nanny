"""Collector redundancy: a standby agent per subnet, and a collection lease.

Revision ID: 0040_collector_redundancy
Revises: 0039_device_definitions

Adds to ``subnets``:

* ``standby_agent_id``  -- the operator's second collector. NULL (the default,
  and the value every existing row gets) means this subnet has no redundancy and
  is not leased at all, so nothing about its collection changes.
* ``collector_agent_id`` / ``collector_lease_expires_at`` -- who is collecting
  now, and until when. Granted only by a conditional UPDATE; the expiry doubles
  as an acquisition barrier after a lease is revoked.
* ``collector_since`` -- when the current holder took over ("last takeover").
* ``collector_prev_agent_id`` / ``collector_prev_until`` -- the immediate
  predecessor and the instant its tenure ended, so a displaced collector can
  still replay the spool it took while it DID hold the lease without that being
  mistaken for a second, overlapping reading series.

Every column is nullable with no server default, so the upgrade is additive and
existing rows land on exactly the "no redundancy" state described above.

Conditional, like 0027-0039, because revision 0001 is
``Base.metadata.create_all()`` -- on a fresh database the ORM metadata has
already produced every column and index here, so each step has to ask before
acting or a fresh install dies on "duplicate column name".

**Deliberately NOT ``batch_alter_table`` on the upgrade.** Batch mode rebuilds
the table on SQLite by reflecting it, and reflection does not carry the
``ON DELETE`` clauses back: run through batch, this migration silently
downgraded the *pre-existing* ``subnets.site_id`` from ON DELETE CASCADE and
``subnets.agent_id`` from ON DELETE SET NULL to NO ACTION. Verified by
inspecting ``PRAGMA foreign_key_list`` before and after. Plain ``ADD COLUMN`` is
supported natively by SQLite and rebuilds nothing, so nothing can be lost in a
round trip that never happens. The three new foreign keys are then added only
where a constraint can be added to a live table -- which excludes SQLite, and
SQLite is the dev/test backend where revision 0001 (``create_all``) has already
produced them anyway.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040_collector_redundancy"
down_revision = "0039_device_definitions"
branch_labels = None
depends_on = None

#: (name, type) of every column added here, in creation order.
_NEW_COLUMNS = (
    ("standby_agent_id", sa.Integer()),
    ("collector_agent_id", sa.Integer()),
    ("collector_lease_expires_at", sa.DateTime(timezone=True)),
    ("collector_since", sa.DateTime(timezone=True)),
    ("collector_prev_agent_id", sa.Integer()),
    ("collector_prev_until", sa.DateTime(timezone=True)),
)

#: Mirrored from ``Subnet.__table_args__`` and the ``index=True`` columns. An
#: index declared only here is absent on a fresh install; one declared only in
#: the model is absent on an upgraded one.
_NEW_INDEXES = (
    ("ix_subnets_standby_agent_id", ["standby_agent_id"]),
    ("ix_subnets_collector_agent_id", ["collector_agent_id"]),
    ("ix_subnet_collector_lease", ["collector_lease_expires_at"]),
)

#: (constraint name, column) for the three new references to ``agents``.
#: SET NULL rather than CASCADE on every one: deleting an agent must never
#: delete the subnet it collected. A lease left naming a deleted agent is
#: handled above the database anyway -- ``reassign_collectors`` reads a missing
#: agent row as "gone" and a deleted agent cannot heartbeat, so it cannot renew.
_NEW_FKS = (
    ("fk_subnets_standby_agent", "standby_agent_id"),
    ("fk_subnets_collector_agent", "collector_agent_id"),
    ("fk_subnets_collector_prev_agent", "collector_prev_agent_id"),
)


def _columns(bind, table: str) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def _columns_with_fk_to_agents(bind, table: str) -> set:
    """Columns of ``table`` that ALREADY reference ``agents``.

    Deliberately keyed on the column, not the constraint name. Revision 0001 is
    ``create_all()``, so on a fresh database SQLAlchemy has already made these
    foreign keys under ITS auto-generated names (``subnets_standby_agent_id_fkey``)
    -- and a name-based check therefore misses them and adds a second, identical
    constraint on the same column. Observed on a real Postgres run: every fresh
    install ended up with two FKs per new column, so a fresh schema and an
    upgraded one were not the same schema. The column is what the constraint is
    actually about, so the column is what to ask about.
    """
    return {
        col
        for fk in sa.inspect(bind).get_foreign_keys(table)
        if fk.get("referred_table") == "agents"
        for col in fk.get("constrained_columns") or ()
    }


def _subnets_before_0040() -> sa.Table:
    """The ``subnets`` table exactly as it stood before this revision.

    Handed to ``batch_alter_table(copy_from=...)`` on the downgrade so the
    rebuild reproduces the original constraints verbatim instead of reflecting
    them -- the same reason 0033's downgrade spells its table out. Without it
    the reverse migration would restore the columns and quietly lose the
    ON DELETE behaviour on ``site_id`` and ``agent_id``.
    """
    return sa.Table(
        "subnets",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "site_id", sa.Integer(),
            sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "agent_id", sa.Integer(),
            sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("cidr", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column("snmp_community", sa.String(length=120), nullable=False),
        sa.Column("snmp_version", sa.String(length=8), nullable=False),
        sa.Column("snmp_v3", sa.JSON(), nullable=True),
        sa.Column("bind_interface", sa.String(length=64), nullable=True),
        sa.Column("trusted", sa.Boolean(), nullable=False),
        sa.Column("last_discovery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_discovery_found_count", sa.Integer(), nullable=True),
        sa.Column("last_discovery_new_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        *(sa.Column(name, type_, nullable=True) for name, type_ in _NEW_COLUMNS),
        sa.UniqueConstraint("site_id", "cidr", name="uq_subnet_site_cidr"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"

    existing = _columns(bind, "subnets")
    for name, type_ in _NEW_COLUMNS:
        if name not in existing:
            op.add_column("subnets", sa.Column(name, type_, nullable=True))

    if not sqlite:
        constrained = _columns_with_fk_to_agents(bind, "subnets")
        for fk_name, column in _NEW_FKS:
            if column not in constrained:
                op.create_foreign_key(
                    fk_name, "subnets", "agents", [column], ["id"],
                    ondelete="SET NULL",
                )

    have_idx = _indexes(bind, "subnets")
    for name, cols in _NEW_INDEXES:
        if name not in have_idx:
            op.create_index(name, "subnets", cols)


def downgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"

    have_idx = _indexes(bind, "subnets")
    for name, _cols in reversed(_NEW_INDEXES):
        if name in have_idx:
            op.drop_index(name, table_name="subnets")

    if not sqlite:
        # Only the constraints THIS revision named. One created by ``create_all``
        # under SQLAlchemy's own name belongs to the column, and dropping the
        # column below takes it with it.
        have = {fk.get("name") for fk in sa.inspect(bind).get_foreign_keys("subnets")}
        for fk_name, _column in reversed(_NEW_FKS):
            if fk_name in have:
                op.drop_constraint(fk_name, "subnets", type_="foreignkey")

    present = [name for name, _ in _NEW_COLUMNS if name in _columns(bind, "subnets")]
    if not present:
        return
    with op.batch_alter_table("subnets", copy_from=_subnets_before_0040()) as batch:
        for name in reversed(present):
            batch.drop_column(name)
