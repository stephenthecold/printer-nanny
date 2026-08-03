"""Per-client white-label branding: name, logo and primary colour on clients

Revision ID: 0038_client_branding
Revises: 0037_occurrence_alerts

Three nullable columns, because NULL is the whole feature: it means "inherit
the global app.* setting", which is what makes the fallback total for every
client that existed before this ran.

The logo BYTES are not here. They go in ``app_assets`` (migration 0007) under
``client:<id>:logo`` -- one blob store, one uploader, one set of magic-byte
checks -- so this migration has no table of its own to create.

Conditional, like 0027-0033, because revision 0001 is
``Base.metadata.create_all()``: on a fresh database the ORM metadata has
already produced these columns, so each step has to ask before acting or a
fresh install dies on "duplicate column name".
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_client_branding"
down_revision = "0033_macos_drivers"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("brand_name", sa.String(length=120)),
    ("brand_logo_url", sa.String(length=1000)),
    ("brand_primary_color", sa.String(length=32)),
)


def _columns(bind, table: str) -> set:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind, "clients")
    missing = [(name, type_) for name, type_ in _COLUMNS if name not in existing]
    if not missing:
        return
    with op.batch_alter_table("clients") as batch:
        for name, type_ in missing:
            batch.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind, "clients")
    present = [name for name, _type in _COLUMNS if name in existing]
    if not present:
        return
    # Dropping the columns discards the operator's branding, which is the
    # point of a downgrade -- but the uploaded logos live in app_assets and are
    # NOT removed here. A re-upgrade should find the bytes still there rather
    # than silently blank; an operator who wants them gone deletes the logo in
    # the UI, which is the path that also clears the URL.
    with op.batch_alter_table("clients") as batch:
        for name in present:
            batch.drop_column(name)
