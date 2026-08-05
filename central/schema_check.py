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
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import inspect

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
    """
    deadline = time.monotonic() + timeout
    first = True
    while True:
        drift = check(bind)
        if drift is not None and drift.ok:
            if not first:
                log.info("database schema is complete; continuing")
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
