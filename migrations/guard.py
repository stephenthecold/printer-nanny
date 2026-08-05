"""Refuse a downgrade that would discard rows, before it has changed anything.

**The problem this exists for.** Revision 0001 is ``Base.metadata.create_all()``,
so on *every* install -- not merely the pre-Alembic ones -- the tables were
created by the baseline rather than by the revision that later learned to create
them. Revisions 0003, 0007, 0012, 0014 and 0019 all guard their ``drop_table``
with ``has_table``/``_table_exists``, and those guards are genuinely present. But
existence is not authorship: the guard cannot tell "this revision created it"
from "it was already here", and on a create_all baseline the answer is always the
latter. So the guard passes and the drop proceeds.

Measured on a real Postgres 16, not reasoned about::

    alembic upgrade head                    # app_settings created by 0001
    INSERT INTO app_settings ...            # one operator secret
    alembic downgrade 0002_readings_brin    # -> app_settings, audit_log and
                                            #    app_assets all dropped
    alembic upgrade head                    # tables return, 0 rows

``app_settings`` holds the SMTP password, OAuth tokens, the FreeScout key, the
Slack/Teams/webhook URLs and the OIDC client secret. They are Fernet-encrypted at
rest, which protects them from a reader and not at all from a ``DROP TABLE`` --
the key survives in ``SECRET_KEY`` and there is nothing left for it to decrypt.
``audit_log`` is the security record of everything that ever happened. Both come
back empty on the next upgrade, and neither command says a word.

**Why a row check rather than authorship tracking.** Recording which revision
created which table would work for installs made after the record started, and
those are exactly the installs not at risk. For an existing database the
authorship of a table is *unknowable* -- the information was never written down
-- so the mechanism cannot answer the question in the only case that matters. A
row check can, on any database, today.

**Two layers, because they fail differently.**

*The pre-flight* (``install_downgrade_preflight``) runs once, in ``env.py``,
after the migration plan is built and before any step executes. That placement is
the point: it is what makes "nothing has been changed" true rather than
approximately true. Postgres would have rolled a mid-run refusal back anyway --
verified -- but **SQLite does not**: pysqlite commits implicitly around DDL, so a
refusal raised partway through left the database stranded between revisions.
Measured before this existed: a refused ``downgrade 0002_readings_brin`` on
SQLite came to rest at 0003 with ``audit_log`` already gone. The pre-flight also
covers ``drop_column``, which no per-drop table guard can see.

*The per-drop backstop* (``install_drop_table_guard``) wraps
``Operations.drop_table`` itself. It exists because the pre-flight depends on
reading the plan out of the migration context, and if a future alembic changes
that shape the pre-flight would silently stop guarding anything. The backstop
cannot be bypassed that way -- every revision's ``drop_table`` goes through it,
including revisions written by someone who never read this file.
``tests/test_migration_downgrade_guard.py`` asserts both fire, so a silent
regression in either is a test failure rather than an operator's discovery.

**The rule.** A downgrade against an empty database destroys nothing and is
allowed, so CI's ``alembic downgrade base`` on a freshly-migrated scratch
database is unaffected and needs no opt-in. A downgrade against a database
holding rows refuses, names the tables, and points at the override. The override
exists because an operator who means it must not be blocked: the intent is to
convert silent destruction into a decision, not to forbid the operation.

Set ``PN_ALLOW_DESTRUCTIVE_DOWNGRADE=1`` to proceed. Take a backup first;
``/admin/backup`` exists for exactly this.
"""

from __future__ import annotations

import os

import sqlalchemy as sa

#: Opt-in escape hatch. Named in every refusal, so the way forward is never
#: something the operator has to go looking for.
OVERRIDE_ENV = "PN_ALLOW_DESTRUCTIVE_DOWNGRADE"

_TRUTHY = {"1", "true", "yes", "on"}

#: Tables whose loss is not merely inconvenient, called out by name in the
#: message so the operator reads a consequence rather than a table list.
_CRITICAL = {
    "app_settings": "every operator secret (SMTP, OAuth, FreeScout, "
                    "Slack/Teams/webhook, OIDC)",
    "audit_log": "the security audit trail",
    "directory_connections": "directory-sync credentials",
    "agents": "agent API key hashes",
    "workstation_enroll_keys": "workstation enrollment keys",
}


class DestructiveDowngrade(RuntimeError):
    """A downgrade would have discarded rows. Nothing has been changed."""


def override_enabled() -> bool:
    return os.environ.get(OVERRIDE_ENV, "").strip().lower() in _TRUTHY


def _has_rows(bind, table: str) -> bool:
    """Whether ``table`` holds at least one row.

    ``LIMIT 1`` rather than ``count(*)``: this runs over every table in the
    schema, and ``readings`` is an append-only series that reaches tens of
    millions of rows. A full count there would make the guard itself the reason a
    migration command appears to hang. The decision only needs "any", not "how
    many".

    A table that does not exist, or that this connection may not read, is not
    evidence of data -- and a guard that raised on its own inability to check
    would block every legitimate downgrade instead of the dangerous ones.
    """
    try:
        if not sa.inspect(bind).has_table(table):
            return False
        return bind.execute(sa.text(f'SELECT 1 FROM "{table}" LIMIT 1')).first() is not None
    except sa.exc.SQLAlchemyError:
        return False


def _refuse(populated: "list") -> None:
    listed = ", ".join(sorted(populated))
    stakes = [
        f"{name} holds {why}"
        for name, why in sorted(_CRITICAL.items())
        if name in populated
    ]
    detail = (" " + "; ".join(stakes) + ".") if stakes else ""
    raise DestructiveDowngrade(
        "Refusing to downgrade: it would discard rows from tables that still "
        f"hold data -- {listed}." + detail + " Revision 0001 is "
        "Base.metadata.create_all(), so these tables were created by the "
        "baseline rather than by the revision now dropping them; a has_table "
        "guard checks existence, not authorship, and cannot tell the "
        "difference. Nothing has been changed. Take a backup (/admin/backup), "
        f"then set {OVERRIDE_ENV}=1 to proceed."
    )


def refuse_if_populated(bind, *tables: str) -> None:
    """Raise if any of ``tables`` holds a row. Called before anything is dropped."""
    if override_enabled():
        return
    populated = [t for t in tables if _has_rows(bind, t)]
    if populated:
        _refuse(populated)


def install_downgrade_preflight(env_context, connection, metadata) -> bool:
    """Check the whole database once, before the first downgrade step runs.

    Returns whether the hook could be installed. Alembic builds the list of
    ``MigrationStep`` objects by calling the migration context's plan function;
    wrapping that is what lets this see the *direction* before any of it
    executes. ``is_downgrade`` is public on the step; the attribute holding the
    plan function is not, so a failure to find it returns False rather than
    raising -- an alembic upgrade must not break every migration command. The
    backstop in ``install_drop_table_guard`` still applies, and the suite asserts
    this path works so the degradation is never silent.
    """
    if override_enabled():
        return False
    migration_ctx = env_context.get_context()
    plan_fn = getattr(migration_ctx, "_migrations_fn", None)
    if plan_fn is None:
        return False

    def guarded_plan(heads, ctx):
        steps = plan_fn(heads, ctx)
        if any(getattr(step, "is_downgrade", False) for step in steps):
            names = [t.name for t in metadata.sorted_tables]
            populated = [name for name in names if _has_rows(connection, name)]
            if populated:
                _refuse(populated)
        return steps

    migration_ctx._migrations_fn = guarded_plan
    return True


def install_drop_table_guard() -> None:
    """Route every ``op.drop_table`` in every revision through the row check.

    Wrapping the operation rather than editing seventeen ``downgrade()`` bodies
    is deliberate: it covers the revisions that exist, the ones written later,
    and the ones whose author never reads this file. Same discipline as the
    dashboard's component layer -- enforced by construction rather than by
    remembering.

    Idempotent, because ``env.py`` is re-executed on every alembic command and a
    second wrap would probe every table twice.
    """
    from alembic.operations import Operations

    if getattr(Operations.drop_table, "_pn_guarded", False):
        return

    original = Operations.drop_table

    def drop_table(self, table_name, *args, **kwargs):
        refuse_if_populated(self.get_bind(), table_name)
        return original(self, table_name, *args, **kwargs)

    drop_table._pn_guarded = True
    drop_table.__doc__ = original.__doc__
    Operations.drop_table = drop_table
