"""Move printer identity from IP to serial.

A printer used to be resolved by ``(site_id, ip)``, enforced by a UNIQUE
constraint. That encodes "an IP identifies a printer at a site", which gets
DHCP churn wrong in both directions: a printer that changes lease looks brand
new (a duplicate row, with page meters then double-counted across the two), and
a different printer that inherits a recycled IP silently adopts the old row's
id, reading history, meters and maintenance records.

``services.find_printer_in_sites`` now prefers serial. That makes the old
constraint actively wrong rather than merely unhelpful: once a replacement
device is recorded as its own row, both it and the device it replaced
legitimately reference the same address (one retired, one live), and UNIQUE
could only be satisfied by merging their histories -- the exact thing that
corrupts billing.

So: drop the unique constraint, put uniqueness on ``(site_id, serial)`` where a
serial exists, and keep a plain (non-unique) index on ``(site_id, ip)`` since
IP is still the fallback lookup. The serial index is partial because many
devices report no serial over SNMP; those rows must not all collide on NULL.

Data safety: nothing is rewritten or deleted, and the new constraint is strictly
weaker than the old one, so no existing row can fail it. Duplicate serials
already in a fleet WOULD fail -- see the guard below, which skips creating the
unique index and leaves a warning rather than blocking the upgrade.

Revision ID: 0022_printer_serial_index
Revises: 0021_meter_counters
Create Date: 2026-07-24
"""

import logging

import sqlalchemy as sa
from alembic import op

revision = "0022_printer_serial_index"
down_revision = "0021_meter_counters"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

OLD_UNIQUE = "uq_printer_site_ip"
SERIAL_UNIQUE = "uq_printer_site_serial"
IP_INDEX = "ix_printer_site_ip"


def _indexes(bind) -> set:
    return {i["name"] for i in sa.inspect(bind).get_indexes("printers")}


def _uniques(bind) -> set:
    return {c["name"] for c in sa.inspect(bind).get_unique_constraints("printers")}


def _duplicate_serials(bind) -> int:
    return bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM (SELECT site_id, serial FROM printers "
            "WHERE serial IS NOT NULL GROUP BY site_id, serial HAVING COUNT(*) > 1) d"
        )
    ).scalar() or 0


def upgrade() -> None:
    bind = op.get_bind()

    if OLD_UNIQUE in _uniques(bind):
        # batch_alter_table: SQLite cannot drop a constraint in place, so Alembic
        # rebuilds the table. Postgres takes the direct path.
        with op.batch_alter_table("printers", schema=None) as batch:
            batch.drop_constraint(OLD_UNIQUE, type_="unique")

    if IP_INDEX not in _indexes(bind):
        op.create_index(IP_INDEX, "printers", ["site_id", "ip"])

    if SERIAL_UNIQUE not in _indexes(bind):
        dupes = _duplicate_serials(bind)
        if dupes:
            # Pre-existing duplicates are exactly what this change is meant to
            # stop creating; failing the upgrade would strand the operator on an
            # old schema instead. Skip the unique index -- everything else here
            # still applies -- and tell them to merge.
            log.warning(
                "printers: %d duplicate (site_id, serial) group(s) found; skipping "
                "the %s unique index. Merge the duplicate printers, then re-run "
                "this migration to enforce it.",
                dupes,
                SERIAL_UNIQUE,
            )
        else:
            op.create_index(
                SERIAL_UNIQUE,
                "printers",
                ["site_id", "serial"],
                unique=True,
                postgresql_where=sa.text("serial IS NOT NULL"),
                sqlite_where=sa.text("serial IS NOT NULL"),
            )


def downgrade() -> None:
    bind = op.get_bind()

    for name in (SERIAL_UNIQUE, IP_INDEX):
        if name in _indexes(bind):
            op.drop_index(name, table_name="printers")

    # Only restorable when no site has two printers sharing an address, which
    # is precisely what the forward migration allows. Skip rather than fail.
    if OLD_UNIQUE not in _uniques(bind):
        clashes = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM (SELECT site_id, ip FROM printers "
                "GROUP BY site_id, ip HAVING COUNT(*) > 1) d"
            )
        ).scalar() or 0
        if clashes:
            log.warning(
                "printers: %d site/ip group(s) share an address; leaving %s off.",
                clashes,
                OLD_UNIQUE,
            )
        else:
            with op.batch_alter_table("printers", schema=None) as batch:
                batch.create_unique_constraint(OLD_UNIQUE, ["site_id", "ip"])
