"""Container healthcheck for the worker service.

    python -m central.worker.health

The worker has no HTTP surface, so its Docker healthcheck is command-based and
inspects the same ``worker_job_runs`` stamps ``/readyz`` reads (central.health) --
one definition of "working", three consumers.

Exit 0 = the worker's per-job stamps are fresh. Exit 1 = a job is stale, no stamp
exists at all, or the database is unreachable (a worker that can't reach the DB
cannot do its job, so that is correctly unhealthy).

Deliberately reads the stamps *from the database* rather than asking "did THIS
process stamp them". Two worker containers serialise on the leader lock, so the
standby writes nothing every cycle; judging it on its own writes would mark a
perfectly good standby unhealthy. The question that matters is whether the
fleet's worker work is getting done.

"No stamp at all" is unhealthy here even though it does not fail ``/readyz``: a
worker container that is up and has never recorded a cycle is exactly the thing
this probe exists to catch. Compose gives it a ``start_period`` so first boot
(waiting on the db, then the first cycle) is not counted against it.
"""

from __future__ import annotations

import sys
from typing import Optional

from central.db import SessionLocal
from central.health import STATE_OK, database_ok, worker_health


def main(argv: Optional[list] = None) -> int:
    db = SessionLocal()
    try:
        if not database_ok(db):
            print("unhealthy: database unreachable")
            return 1
        health = worker_health(db)
        state = health["state"]
        if state == STATE_OK:
            print("healthy: {} job(s) fresh".format(len(health["jobs"])))
            return 0
        # "unknown" means the table isn't there yet (stack mid-migration). The
        # worker genuinely has nowhere to stamp, so it can't be called healthy,
        # but say which so an operator isn't sent hunting for a dead process.
        stale = ",".join(health["stale_jobs"])
        print("unhealthy: worker {}{}".format(state, " ({})".format(stale) if stale else ""))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
