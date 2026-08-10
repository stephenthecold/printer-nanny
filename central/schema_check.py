"""Does the live database actually have the schema this build expects?

**The failure this exists for is a race, observed in production 2026-08-05.**
``docker compose up -d`` starts the api and the worker in parallel. The api
container's command is ``alembic upgrade head && ... && uvicorn``; the worker's
is not -- it waits for nothing and runs its first cycle immediately. On a
database with 2.4M readings, fifteen pending revisions take minutes, so the
worker spent its first cycle querying a schema that was still being built:
seven jobs died with ``UndefinedColumn`` / ``UndefinedTable``, and the cycle
summary that followed listed only the jobs that had survived. Every later cycle
was clean. Nothing was wrong with the database; the worker simply asked too
early.

Two things made that expensive to diagnose, and both are addressed here:

* **The worker reported a normal-looking cycle summary alongside the
  tracebacks.** A shorter dict is not a visible failure, so the run read as
  "mostly working" rather than "ran against a schema that does not exist yet".
* **``alembic current`` said ``head``, truthfully, by the time anyone looked.**
  The evidence of the race had already evaporated. A first reading of this
  incident concluded the stamp was lying and the schema was twelve revisions
  stale; it was not, and the remedy that follows from that reading -- ``alembic
  stamp`` backwards -- is refused by ``migrations/guard.py`` on any populated
  database, correctly.

**So this compares the SCHEMA, never ``alembic_version``.** The version is a
record of intent and answers a different question: mid-migration it is already
moving, and after the fact it cannot say what was true two minutes ago. Only the
columns can be asked, and they are also what a query actually needs.

**Not the same question as ``tests/test_schema_drift.py``**, which builds a
fresh database through the migration chain and compares the result to the models
-- a build-time check that the chain is correct, run in CI. This is a runtime
check that one particular *deployed* database, with its own history of partial
upgrades and hand-run commands, currently has what this build needs. The first
can pass forever while the second fails on a customer's server.

Two scoping decisions:

* **Missing only.** A column the database has and the models do not is what
  running older code against a newer database looks like, and it breaks nothing.
  A column the models have and the database does not is a guaranteed
  ``UndefinedColumn`` the first time that feature is touched. Reporting only the
  second keeps the check quiet during a rollback.
* **Never fatal.** This reports; it does not refuse to boot, and
  ``wait_for_schema`` gives up rather than blocking forever. A half-migrated
  database still serves most of the product, and an install where the operator
  genuinely has not migrated must still come up far enough to be fixed from the
  dashboard. ``install.sh`` and the CLI treat drift as failure, because there a
  human is watching and the next step is theirs.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import inspect, text

from central.db import Base, engine

log = logging.getLogger(__name__)

#: How long the worker waits for the api's migrations before giving up and
#: running anyway. Generous because the thing being waited on is a schema
#: migration over a production-sized table, and short enough that a genuinely
#: un-migrated install still starts and can be repaired from the dashboard.
DEFAULT_WAIT_SECONDS = 300


class SchemaDrift:
    """What the models require that the database does not have."""

    def __init__(
        self,
        missing_tables: Sequence[str] = (),
        missing_columns: Sequence[Tuple[str, str]] = (),
    ) -> None:
        self.missing_tables: List[str] = sorted(missing_tables)
        self.missing_columns: List[Tuple[str, str]] = sorted(missing_columns)

    @property
    def ok(self) -> bool:
        return not self.missing_tables and not self.missing_columns

    def summary(self) -> str:
        return (
            f"{len(self.missing_tables)} missing table(s), "
            f"{len(self.missing_columns)} missing column(s)"
        )

    def describe(self) -> str:
        """One block of text an operator can act on."""
        if self.ok:
            return "schema OK: every table and column the models declare is present"
        lines = ["SCHEMA DRIFT: this build expects objects the database does not have."]
        if self.missing_tables:
            lines.append(f"  missing tables ({len(self.missing_tables)}):")
            lines += [f"    - {t}" for t in self.missing_tables]
        if self.missing_columns:
            lines.append(f"  missing columns ({len(self.missing_columns)}):")
            lines += [f"    - {t}.{c}" for t, c in self.missing_columns]
        lines += [
            "",
            "  If the stack has just been started, migrations may still be running",
            "  in the api container -- they run there, not here. Otherwise they have",
            "  not been applied:",
            "",
            "    docker compose logs api | tail -40",
            "    docker compose run --rm api alembic upgrade head",
            "",
            "  Check THIS rather than `alembic current`: the version records which",
            "  revisions ran, which is a different question from what the schema has.",
        ]
        return "\n".join(lines)


def inspect_schema(bind=None) -> SchemaDrift:
    """Compare ``Base.metadata`` against the live database.

    Raises whatever the driver raises if the database cannot be reached --
    callers guard it, because "cannot connect" is a different problem with its
    own reporting and must not be dressed up as drift.
    """
    import central.models  # noqa: F401  (register every model on Base)

    insp = inspect(bind if bind is not None else engine)
    present = set(insp.get_table_names())

    missing_tables: List[str] = []
    missing_columns: List[Tuple[str, str]] = []
    for name, table in Base.metadata.tables.items():
        if name not in present:
            missing_tables.append(name)
            continue
        # Columns only. Types are compared by migration 0045 and by
        # tests/test_upgraded_schema_alignment.py, which can be precise about
        # per-dialect rendering in a way an introspection here cannot.
        have = {c["name"] for c in insp.get_columns(name)}
        missing_columns += [(name, c.name) for c in table.columns if c.name not in have]
    return SchemaDrift(missing_tables, missing_columns)


def check(bind=None) -> Optional[SchemaDrift]:
    """``inspect_schema`` that never raises. None means "could not check".

    Distinct from a clean result on purpose: an unreachable database is not a
    healthy schema, and the two must not collapse into one answer.
    """
    try:
        return inspect_schema(bind)
    except Exception as exc:  # pragma: no cover - needs an unreachable database
        log.warning("could not verify the database schema: %s", exc)
        return None


def _head_revision() -> Optional[str]:
    """The head revision this build's migration scripts declare, or None.

    Read straight from ``ScriptDirectory`` rather than through
    ``alembic.config.Config``: that would parse ``alembic.ini`` with
    ConfigParser, and this repo has already been bitten once by a credential
    meeting ``BasicInterpolation`` (see migrations/env.py). Nothing here needs
    the ini file -- only the revision graph on disk.
    """
    try:
        from alembic.script import ScriptDirectory

        root = Path(__file__).resolve().parent.parent
        # Reading the head means alembic EXECUTES every revision module, and
        # 0001_baseline does `from migrations.guard import refuse_if_populated`
        # -- so the repo root has to be importable or the whole read fails with
        # ModuleNotFoundError. In the container it happens to be (WORKDIR /app,
        # and `python -m` puts cwd on sys.path), which is exactly the kind of
        # accident this repo has been bitten by before: it works until something
        # runs from another directory, and then degrades silently. Put the root
        # on the path ourselves and take it back off.
        added = str(root) not in sys.path
        if added:
            sys.path.insert(0, str(root))
        try:
            heads = ScriptDirectory(str(root / "migrations")).get_heads()
        finally:
            if added:
                try:
                    sys.path.remove(str(root))
                except ValueError:  # pragma: no cover - someone else removed it
                    pass
        # tests/test_migration_chain.py asserts exactly one head; anything else
        # is a forked chain, which is not a question this function can answer.
        return heads[0] if len(heads) == 1 else None
    except Exception as exc:  # pragma: no cover - needs a broken/absent tree
        log.debug("could not read the migration head: %s", exc)
        return None


def migrations_are_pending(bind=None) -> Optional[bool]:
    """Is there migration work still to do? None means "cannot tell".

    **This is the one legitimate use of ``alembic_version`` in this module**,
    and it does not contradict the docstring above. That paragraph refuses the
    version as an answer to *does the schema have what we need* -- a question
    only the columns can answer. This asks a different one: *is anybody still
    migrating?* -- which is precisely what the version records and the columns
    cannot say. The verdict on the schema stays with ``inspect_schema``.

    Why it matters: ``wait_for_schema`` cannot otherwise tell a migration that
    is still running from a schema that is simply wrong, so it spent the full
    budget on both. Observed in production 2026-08-10 -- ``app_assets`` had gone
    missing from a database already stamped at head, so every worker restart
    waited the whole 300s for a table no migration was going to create, and the
    dashboard called the worker stalled for the duration.

    Returns True when the stamp is behind head or absent (migrations pending or
    in flight), False when it is exactly at head (they are done -- waiting will
    not help), and None when that cannot be established. **None and True both
    keep the caller waiting**, because giving up early is the new behaviour and
    an unreadable version must not be the thing that triggers it.
    """
    head = _head_revision()
    if head is None:
        return None
    try:
        insp = inspect(bind if bind is not None else engine)
        if "alembic_version" not in set(insp.get_table_names()):
            # Never migrated: everything is pending.
            return True
        target = bind if bind is not None else engine
        with target.connect() as conn:
            stamped = [r[0] for r in conn.execute(text("SELECT version_num FROM alembic_version"))]
    except Exception as exc:  # pragma: no cover - needs an unreachable database
        log.debug("could not read alembic_version: %s", exc)
        return None
    if len(stamped) != 1:
        # No rows, or a forked/multi-head stamp. Either way not a clean "done".
        return True
    return stamped[0] != head


def wait_for_schema(
    timeout: float = DEFAULT_WAIT_SECONDS,
    interval: float = 2.0,
    bind=None,
    where: str = "process",
) -> Optional[SchemaDrift]:
    """Block until the schema is complete, the timeout expires, or we give up.

    This is what closes the race. The worker calls it before its first cycle,
    so a stack that has just come up waits for the api's ``alembic upgrade
    head`` instead of querying halfway through it.

    It returns rather than raising in every case, because a worker that refuses
    to start is worse than one that runs and logs why its jobs are failing --
    the operator's route to fixing either is the dashboard, which needs the
    stack up.

    **It also stops early when nobody is migrating.** Waiting only makes sense
    while something is still applying revisions; against a database stamped at
    head the missing objects are never going to arrive, and the budget is spent
    for nothing. Worse than nothing, in fact -- the worker writes no liveness
    stamps while it waits, so a wait longer than ``health.stale_after_seconds``
    makes the dashboard announce a stalled worker on every restart. That is what
    a real install did on 2026-08-10, once per restart, for a single dropped
    table. See ``migrations_are_pending``.
    """
    deadline = time.monotonic() + timeout
    first = True
    while True:
        drift = check(bind)
        if drift is not None and drift.ok:
            if not first:
                log.info("database schema is complete; continuing")
            return drift
        if drift is not None and migrations_are_pending(bind) is False:
            # Stamped at head with objects still missing: this is drift, not a
            # race, and no amount of waiting fixes drift. Report it now and let
            # the caller get on with it -- deliberately at the same volume as
            # the timeout path below, because the operator's next step is
            # identical and only the reason differs.
            log.error(
                "database schema is incomplete in the %s and migrations are "
                "already at head, so waiting cannot help (%s). Running anyway; "
                "jobs touching these will fail. %s",
                where, drift.summary(), drift.describe(),
            )
            return drift
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if drift is None:
                log.error(
                    "gave up waiting for the database in the %s after %ss",
                    where, int(timeout),
                )
            else:
                log.error(
                    "database schema still incomplete in the %s after %ss (%s). "
                    "Running anyway; jobs touching these will fail. %s",
                    where, int(timeout), drift.summary(), drift.describe(),
                )
            return drift
        if first:
            log.warning(
                "waiting up to %ss for the database schema in the %s (%s) -- "
                "migrations run in the api container and may still be in progress",
                int(timeout), where,
                drift.summary() if drift is not None else "database unreachable",
            )
            first = False
        time.sleep(min(interval, remaining))


def main(argv: Optional[list] = None) -> int:
    """``python -m central.schema_check``.

    Exit 0 clean, 1 drifted, 2 could not check. ``install.sh`` reads these, so a
    database it cannot reach must never be reported as a healthy one.
    """
    parser = argparse.ArgumentParser(
        description="Verify the database has the schema this build expects.",
    )
    parser.add_argument("--quiet", action="store_true", help="print only on drift")
    parser.add_argument(
        "--wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="wait up to SECONDS for migrations to finish before deciding",
    )
    args = parser.parse_args(argv)

    if args.wait > 0:
        drift = wait_for_schema(timeout=args.wait, where="check")
    else:
        drift = check()

    if drift is None:
        print("could not check the schema (database unreachable)", file=sys.stderr)
        return 2
    if drift.ok:
        if not args.quiet:
            print(drift.describe())
        return 0
    print(drift.describe(), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
