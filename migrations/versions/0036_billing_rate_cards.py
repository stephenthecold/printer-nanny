"""billing rate cards + graduated volume tiers

Revision ID: 0036_billing_rate_cards
Revises: 0035_event_bus

Cost-per-page contract terms, per client. Two tables: the card (base mono and
colour rates, currency, optional minimum commitment, and what to do with pages a
device reports no split for) and its optional graduated volume bands.

Three things here are correctness rather than shape, and each mirrors a decision
stated in ``central/models.py`` and ``central/money.py``:

* **Rates and amounts are NUMERIC, never DOUBLE PRECISION.** A cost per page is
  fractions of a cent and an invoice multiplies it by five-figure page counts;
  binary floating point cannot represent either operand exactly. On SQLite --
  which has no decimal storage and where SQLAlchemy would silently fall back to
  a float -- the ORM type stores a zero-padded fixed-scale string instead, so
  this migration asks the dialect rather than hardcoding one shape.
* **The partial unique index on (client_id) WHERE active** is what makes "the
  client's rate card" a lookup instead of a choice. Without it two active cards
  can coexist and an invoice's rates depend on row order.
* ``up_to > 0`` on a tier: a band ceiling of zero prices nothing and silently
  swallows the band below it.

Conditional, like 0027-0035, because revision 0001 is
``Base.metadata.create_all()`` -- on a fresh database the ORM metadata has
already produced every table, column, constraint and index below, so an
unconditional create dies on exactly the installs most likely to be new.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036_billing_rate_cards"
down_revision = "0035_event_bus"
branch_labels = None
depends_on = None

#: Must match ``central.money``: NUMERIC where the backend has it, fixed-width
#: text where it does not. Declaring NUMERIC on SQLite would not fail -- SQLite
#: accepts any type name -- it would just store what the ORM writes as text
#: under a column typed NUMERIC, and then anything that later trusted the
#: declared type would be reading the wrong thing.
_RATE_PRECISION, _RATE_SCALE = 12, 6
_MONEY_PRECISION, _MONEY_SCALE = 12, 2


def _fixed_point(bind, precision: int, scale: int):
    if bind.dialect.name == "sqlite":
        return sa.String(precision + 1)
    return sa.Numeric(precision=precision, scale=scale)


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _indexes(bind, table: str) -> set:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    rate = _fixed_point(bind, _RATE_PRECISION, _RATE_SCALE)
    money = _fixed_point(bind, _MONEY_PRECISION, _MONEY_SCALE)

    if not _has_table(bind, "billing_rate_cards"):
        op.create_table(
            "billing_rate_cards",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "client_id",
                sa.Integer(),
                sa.ForeignKey("clients.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=False,
                      server_default="USD"),
            sa.Column("mono_rate", rate, nullable=False),
            sa.Column("color_rate", rate, nullable=False),
            sa.Column("minimum_charge", money, nullable=True),
            sa.Column("unsplit_policy", sa.String(length=32), nullable=False,
                      server_default="exclude"),
            sa.Column("active", sa.Boolean(), nullable=False,
                      server_default=sa.true()),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("client_id", "name", name="uq_rate_card_client_name"),
        )
        op.create_index(
            "ix_billing_rate_cards_client_id", "billing_rate_cards", ["client_id"]
        )

    if "uq_rate_card_client_active" not in _indexes(bind, "billing_rate_cards"):
        # Partial: any number of retired cards per client, at most one active.
        # Both backends support this; the same shape as uq_printer_site_serial.
        op.create_index(
            "uq_rate_card_client_active",
            "billing_rate_cards",
            ["client_id"],
            unique=True,
            postgresql_where=sa.text("active"),
            sqlite_where=sa.text("active"),
        )

    if not _has_table(bind, "billing_rate_tiers"):
        op.create_table(
            "billing_rate_tiers",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "rate_card_id",
                sa.Integer(),
                sa.ForeignKey("billing_rate_cards.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("up_to", sa.Integer(), nullable=False),
            sa.Column("rate", rate, nullable=False),
            sa.UniqueConstraint(
                "rate_card_id", "kind", "up_to", name="uq_rate_tier_card_kind_upto"
            ),
            sa.CheckConstraint("up_to > 0", name="ck_rate_tier_up_to_positive"),
        )
        op.create_index(
            "ix_billing_rate_tiers_rate_card_id", "billing_rate_tiers", ["rate_card_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "billing_rate_tiers"):
        op.drop_table("billing_rate_tiers")
    if _has_table(bind, "billing_rate_cards"):
        op.drop_table("billing_rate_cards")
