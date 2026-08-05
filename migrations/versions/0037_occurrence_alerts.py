"""Occurrence-rate alert rules: count matching events in a rolling window.

Every existing ``AlertConditionType`` asks "is this true right now" -- a supply
is under a level, an agent has missed its heartbeat, an error is unresolved.
Nothing could express "not every jam, but ten jams a day", which is the shape
most fleet complaints actually take: a device that fails intermittently never
trips a state condition long enough to be noticed, and error_severity raises one
alert for the standing condition and then goes quiet while it keeps happening.

``occurrence_rate`` fills that in, and needs three things the rule table could
not carry:

``window_minutes``  -- the rolling window W. NULL on every other condition type.
                       The evaluator refuses a rule without one rather than
                       defaulting: a window is half the operator's statement of
                       what "too often" means, and an unbounded one is a scan of
                       every event ever recorded on an append-only table.
``match_code``      -- case-insensitive SUBSTRING of ``printer_events.code``, so
                       "jam" catches the agent's "jammed". NULL counts
                       everything.
``match_min_severity`` -- optional floor for what counts. Deliberately separate
                       from ``alert_rules.severity``, which is the severity of
                       the alert this rule RAISES: an operator may well want a
                       critical alert about a flood of warnings, and
                       error_severity's existing conflation of the two cannot
                       express that.

Also adds ``ix_printer_events_printer_ts``. The pre-existing indexes on that
table are single-column (printer_id, ts), so a windowed per-printer count is a
scan of everything the printer has ever emitted followed by a filter -- run once
per rule per worker cycle, forever, on a table nothing prunes. Leading with
printer_id and ranging on ts makes it one index range. The index is ALSO declared
in ``PrinterEvent.__table_args__``, because revision 0001 is a
``Base.metadata.create_all()``: an index living only here is silently absent on
every fresh install.

Data safety: purely additive. Three nullable columns with no default (NULL is
meaningful -- "this rule is not an occurrence-rate rule") so existing rows need
no backfill and no rewrite, plus one index. Every step is guarded, so a re-run
and a run against a database that already has the ORM-created index are both
no-ops.

Revision ID: 0037_occurrence_alerts
Revises: 0036_billing_rate_cards
Create Date: 2026-08-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0037_occurrence_alerts"
down_revision = "0036_billing_rate_cards"
branch_labels = None
depends_on = None

RULES = "alert_rules"
EVENTS = "printer_events"
INDEX = "ix_printer_events_printer_ts"

# (name, type) — all nullable, no server_default. NULL means "not an
# occurrence-rate rule", which is the truth for every row that already exists.
NEW_COLUMNS = (
    ("window_minutes", sa.Integer()),
    ("match_code", sa.String(length=80)),
    # Enums are portable VARCHARs here (see central.models._enum), so there is
    # no native DB enum type to alter and adding a member later needs no DDL.
    ("match_min_severity", sa.String(length=32)),
)


def _columns(bind, table) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table) -> set:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()

    existing = _columns(bind, RULES)
    for name, type_ in NEW_COLUMNS:
        if name not in existing:
            op.add_column(RULES, sa.Column(name, type_, nullable=True))

    # Mirrors PrinterEvent.__table_args__ for the upgrade-an-existing-database
    # path. On a fresh install revision 0001 already created it from ORM
    # metadata, which is exactly why this is guarded rather than unconditional.
    if INDEX not in _indexes(bind, EVENTS):
        op.create_index(INDEX, EVENTS, ["printer_id", "ts"])


def downgrade() -> None:
    bind = op.get_bind()

    # Guarded so a downgrade against a database that never had these (a
    # pre-Alembic install stamped forward) doesn't fail, and so this revision
    # only ever removes what it added.
    if INDEX in _indexes(bind, EVENTS):
        op.drop_index(INDEX, table_name=EVENTS)

    existing = _columns(bind, RULES)
    present = [name for name, _ in NEW_COLUMNS if name in existing]
    if not present:
        return
    # One batch for all three: SQLite rebuilds the table per batch block, so
    # three separate blocks would rebuild alert_rules three times.
    with op.batch_alter_table(RULES, schema=None) as batch:
        for name in present:
            batch.drop_column(name)
