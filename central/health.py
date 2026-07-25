"""Liveness / readiness checks.

One module owns "is this deployment actually working", and three consumers read
from it so they can never disagree:

  * ``/readyz`` (central.main) -- the HTTP readiness probe: DB reachable and the
    worker's per-job stamps fresh, 503 otherwise.
  * ``python -m central.worker.health`` -- the worker container's healthcheck.
    The worker has no HTTP surface, so its probe is command-based.
  * ``worker_banner`` -- the dashboard's degraded-worker banner, so an operator
    who never curls anything still sees a stalled worker.

Why this exists: ``mark_offline_agents`` is itself a worker job, so a dead worker
stops marking agents offline and the dashboard shows a green, healthy fleet over
frozen data -- the one failure mode that reports nothing. See docs/audit-2026-07.md.

Staleness is per job and derived from the interval the worker is actually running
with (stamped on each row by central.worker.run), never from a hardcoded age --
an operator running ``--interval 900`` on a slow-polling fleet must not be paged
because a 5-minute constant said so.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from central import models as m

# Default worker cycle cadence in seconds. Declared here rather than in
# central.worker.run so the read side has a fallback for a row written before
# the interval was recorded, without importing the worker. run.py takes its
# argparse default from this constant, so the two can't drift.
DEFAULT_CYCLE_SECONDS = 60

# A job counts as stale once it has gone this many times its own cycle interval
# with no success. 3x absorbs one slow or skipped cycle plus scheduler jitter
# before crying wolf, while still catching a dead worker inside a few minutes at
# the default cadence.
WORKER_STALE_MULTIPLIER = 3.0
# Floor for very short intervals: at ``--interval 5`` a bare 15s threshold would
# flap on any GC pause, lock contention or slow forecast pass.
WORKER_STALE_FLOOR_SECONDS = 120

# A job that has raised this many cycles in a row is unhealthy NOW, without
# waiting for its age threshold to expire. Age alone is absence of evidence; a
# climbing failure count is evidence. It matters most on a slow cadence -- at
# ``--interval 900`` the age threshold is 45 minutes, so a job failing on every
# single pass would otherwise stay green for 45 minutes while we already knew.
# 3 rather than 1 so a transient blip (one DB failover, one channel timeout)
# doesn't page anyone.
WORKER_MAX_CONSECUTIVE_FAILURES = 3

# worker_health states.
STATE_OK = "ok"
STATE_STALE = "stale"
STATE_NEVER_RAN = "never_ran"
STATE_UNKNOWN = "unknown"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; treat them as UTC (as jobs.py does)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def stale_after_seconds(interval_seconds: Optional[int]) -> float:
    """Staleness threshold for a job that is scheduled every ``interval_seconds``."""
    interval = int(interval_seconds or 0)
    if interval <= 0:
        interval = DEFAULT_CYCLE_SECONDS
    return max(interval * WORKER_STALE_MULTIPLIER, float(WORKER_STALE_FLOOR_SECONDS))


def database_ok(db: Session) -> bool:
    """True when the database answers a trivial query.

    Rolls back on failure so the caller's session isn't left in a failed
    transaction, and swallows the exception -- the caller only needs the boolean,
    and the reason must never reach an unauthenticated probe response.
    """
    try:
        db.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - any driver/connection error means "not ready"
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _known_job_names() -> set:
    """Job names the *current* code runs, so a retired job's row can't 503 forever.

    Imported lazily: central.worker.run pulls in the whole job/report stack, and
    this module is imported by central.main at boot. An import failure degrades
    to "consider every row", which is the safe (stricter) direction.
    """
    try:
        from central.worker.run import JOBS

        return {job.__name__ for job in JOBS}
    except Exception:  # noqa: BLE001
        return set()


def worker_health(db: Session, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Per-job freshness of the worker's ``last_success_at`` stamps.

    Returns ``{"state", "stale_jobs", "jobs"}`` where state is:

      ``ok``         every known job is completing.
      ``stale``      at least one job is either *overdue* (no success inside its
                     own threshold -- a dead or wedged worker) or *failing*
                     (raised ``WORKER_MAX_CONSECUTIVE_FAILURES`` cycles in a
                     row). Each job's ``reason`` says which.
      ``never_ran``  no stamps at all. A fresh install before the worker's first
                     cycle looks like this, so it deliberately does NOT fail
                     ``/readyz``; the banner and the worker's own container
                     healthcheck (which has a start_period) cover it instead.
      ``unknown``    the table isn't there yet (stack mid-migration) or the read
                     failed. Never fails a probe -- that would turn a migration
                     window into an outage.

    ``jobs`` carries a per-job detail row for the dashboard. It includes the last
    error's exception CLASS NAME only, never its message: messages routinely
    carry the DSN, SQL text or bound parameters, and this value is rendered in
    HTML. The full traceback stays in the worker's log where it belongs.
    """
    now = now or _now()
    try:
        if not sa_inspect(db.get_bind()).has_table(m.WorkerJobRun.__tablename__):
            return {"state": STATE_UNKNOWN, "stale_jobs": [], "jobs": []}
        known = _known_job_names()
        rows = [
            row
            for row in db.scalars(select(m.WorkerJobRun))
            if not known or row.job in known
        ]
    except Exception:  # noqa: BLE001 - a failed read is "unknown", not "stale"
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"state": STATE_UNKNOWN, "stale_jobs": [], "jobs": []}

    if not rows:
        return {"state": STATE_NEVER_RAN, "stale_jobs": [], "jobs": []}

    jobs: List[Dict[str, Any]] = []
    stale: List[str] = []
    for row in sorted(rows, key=lambda r: r.job):
        threshold = stale_after_seconds(row.expected_interval_seconds)
        last = _aware(row.last_success_at)
        age = (now - last).total_seconds() if last is not None else None
        failures = row.consecutive_failures or 0
        # Two independent ways to be unhealthy: overdue (no success inside the
        # threshold -- catches a dead or wedged worker) or failing (raising every
        # cycle -- caught immediately rather than waiting out the threshold).
        overdue = age is None or age > threshold
        failing = failures >= WORKER_MAX_CONSECUTIVE_FAILURES
        reason = "overdue" if overdue else ("failing" if failing else "ok")
        if overdue or failing:
            stale.append(row.job)
        jobs.append(
            {
                "job": row.job,
                "last_success_at": last,
                "age_seconds": None if age is None else round(age, 1),
                "threshold_seconds": round(threshold, 1),
                "stale": overdue or failing,
                "reason": reason,
                "consecutive_failures": failures,
                "last_error_type": row.last_error_type,
            }
        )
    return {
        "state": STATE_STALE if stale else STATE_OK,
        "stale_jobs": stale,
        "jobs": jobs,
    }


def worker_banner(db: Session, user: Any = None) -> Optional[Dict[str, Any]]:
    """Banner context for base.html, or ``None`` when there is nothing to say.

    Broader than the ``/readyz`` 503 on purpose: ``never_ran`` is shown here (an
    operator who forgot to start the worker needs to know) but does not fail the
    readiness probe. ``unknown`` shows nothing -- a migration window is not an
    incident.

    Requires a signed-in non-tenant user. ``user is None`` covers the login page,
    which base.html also renders: an anonymous visitor must not be told this
    deployment is degraded, nor be handed internal job names. ``client_readonly``
    is excluded because the customer portal is a tenant-facing surface and the
    worker's internals are not their business.

    Never raises -- a DB hiccup must not 500 every page on the dashboard.
    """
    try:
        if user is None:
            return None
        role = getattr(getattr(user, "role", None), "value", None)
        if role == "client_readonly":
            return None
        health = worker_health(db)
        if health["state"] not in (STATE_STALE, STATE_NEVER_RAN):
            return None
        return {"state": health["state"], "jobs": health["stale_jobs"]}
    except Exception:  # noqa: BLE001
        return None
