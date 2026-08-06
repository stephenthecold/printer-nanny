"""Worker entrypoint.

`python -m central.worker.run`         → run forever on a schedule (APScheduler)
`python -m central.worker.run --once`  → run every job once and exit (CI / cron / demo)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

from central import models as m
from central.config import settings
from central.db import SessionLocal, create_all, try_leader_lock
from central.health import DEFAULT_CYCLE_SECONDS
from central.logging_config import configure_logging
from central.reports import run_scheduled_reports
from central.retention import roll_up_readings
from central.schema_check import DEFAULT_WAIT_SECONDS, wait_for_schema
from central.worker import jobs

log = logging.getLogger("printer_nanny.worker")

JOBS = (
    jobs.mark_offline_agents,
    # Immediately after, because it reads the statuses that job just wrote: a
    # standby may only take a subnet from a collector already marked offline.
    # Before mark_offline_printers, so a site whose standby takes over this
    # cycle counts as covered and its printers are not flagged as an outage.
    jobs.reassign_collectors,
    # Must run before evaluate_alerts so a printer that just went stale is
    # already marked offline when the rules are evaluated in this same cycle.
    jobs.mark_offline_printers,
    jobs.evaluate_alerts,
    jobs.check_maintenance_due,
    # Re-send any failed/pending channel deliveries that are due (backoff).
    jobs.retry_deliveries,
    # Release notifications a quiet-hours window held, once the window closes.
    # After retry_deliveries so a digest built this cycle isn't also swept by the
    # retry path in the same pass.
    jobs.flush_quiet_hours,
    # POST queued outbound events to their subscribers. After the alert jobs so
    # events they emitted this cycle go out in the same pass, and after the
    # notification paths because a human being paged is more urgent than an
    # integration being told.
    jobs.deliver_events,
    jobs.forecast_supplies,
    # Publish supply-reorder recommendations to the outbound event bus. After
    # forecast_supplies so it reads this cycle's estimates rather than the last
    # one's. Interval-gated internally (reorder.emit_interval_min) and off by
    # default, so on most installs and most cycles it is a settings read.
    jobs.publish_reorder_recommendations,
    # Fold new readings into cartridge cycles (central.supply_yield), which is
    # what makes pages-per-cartridge measurable at all. Interval-gated
    # internally (yield.scan_interval_min, default 6h), so on almost every cycle
    # this is one settings read and a marker comparison. After forecast_supplies
    # because both walk the same reading history and the forecast is the one an
    # operator is waiting on.
    jobs.scan_supply_cycles,
    # Interval-gated internally (directory.sync_interval_min); a no-op on most
    # cycles. After the alert path so a slow directory never delays alerting.
    jobs.sync_directories,
    # One indexed DELETE over failed-sign-in rows old enough to be unreadable by
    # the throttle. Cheap, and the only thing that reclaims buckets belonging to
    # usernames an attacker invented once and never returned to.
    jobs.prune_login_attempts,
    # Cheap no-op unless a weekly/monthly report is due (marker-gated).
    run_scheduled_reports,
    # LAST, deliberately. Retention is the only job here that can delete, and it
    # is the only one whose work is bounded by a budget rather than by how much
    # there is to do -- so it must never be the reason alerting, delivery retries
    # or reports were late in a cycle. An idle pass is one indexed MIN(ts) probe,
    # so it needs no schedule of its own (central.retention).
    roll_up_readings,
)


def _stamp(
    db,
    job_name: str,
    *,
    interval_seconds: int,
    ok: bool,
    error: Optional[BaseException] = None,
) -> None:
    """Record this job's outcome in ``worker_job_runs``. Never raises.

    This is the liveness stamp ``/readyz``, the worker's container healthcheck and
    the dashboard banner all read (central.health). It is written per job, not per
    cycle, because the loop below deliberately swallows one job's exception to
    keep the rest of the cycle alive -- a single cycle-level stamp would stay
    fresh while one job failed on every pass.

    ``expected_interval_seconds`` is re-stamped every time so the read side always
    derives its threshold from the cadence the worker is *currently* running with,
    picking up an operator's ``--interval`` change on the next cycle.

    Only the exception's class name is persisted: messages routinely carry the
    DSN, SQL text or bound parameters and this value is rendered in the dashboard.
    ``log.exception`` above has already put the full traceback in the worker log.
    Failing to stamp must never break the cycle, so everything here is guarded.
    """
    try:
        row = db.get(m.WorkerJobRun, job_name)
        if row is None:
            row = m.WorkerJobRun(job=job_name)
            db.add(row)
        now = datetime.now(timezone.utc)
        row.expected_interval_seconds = int(interval_seconds)
        if ok:
            row.last_success_at = now
            row.consecutive_failures = 0
            row.last_error_type = None
        else:
            row.last_error_at = now
            row.last_error_type = (type(error).__name__ if error else "Exception")[:80]
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
        db.commit()
    except Exception:  # noqa: BLE001 - a failed stamp must not kill the cycle
        log.exception("failed to stamp worker_job_runs for %s", job_name)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _run_jobs(db, interval_seconds: int = DEFAULT_CYCLE_SECONDS) -> dict:
    summary: dict = {}
    for job in JOBS:
        try:
            summary.update(job(db))
        except Exception as exc:  # noqa: BLE001 - keep the cycle alive on a single job failure
            log.exception("job %s failed", job.__name__)
            db.rollback()
            _stamp(db, job.__name__, interval_seconds=interval_seconds, ok=False, error=exc)
        else:
            _stamp(db, job.__name__, interval_seconds=interval_seconds, ok=True)
    return summary


def run_cycle(interval_seconds: int = DEFAULT_CYCLE_SECONDS) -> dict:
    """Run one worker cycle under the single-leader advisory lock.

    Only the worker that holds the lock runs the jobs; a second worker container
    that loses the race SKIPs the cycle entirely (no double-processing of alerts /
    maintenance / forecasts / reports). On SQLite/dev the lock is a no-op that
    always acquires (single-process by construction). The lock is held for the
    whole cycle and released at the end -- jobs commit their own work as they go,
    so a crash mid-cycle just releases the lock and the next tick re-runs.

    ``interval_seconds`` is the cadence this worker is scheduled at; it is stamped
    onto every job row so the readiness threshold tracks real configuration. A
    worker that skips the cycle (not the leader) stamps nothing -- the leader's
    stamps are what every consumer reads, so a standby stays healthy.
    """
    with try_leader_lock() as acquired:
        if not acquired:
            log.info("worker cycle skipped: another worker holds the leader lock")
            return {"skipped": "not_leader"}
        db = SessionLocal()
        try:
            summary = _run_jobs(db, interval_seconds)
        finally:
            db.close()
    log.info("worker cycle: %s", summary)
    return summary


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Printer Nanny worker")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_CYCLE_SECONDS,
        help="seconds between cycles",
    )
    parser.add_argument(
        "--schema-wait",
        type=float,
        default=DEFAULT_WAIT_SECONDS,
        metavar="SECONDS",
        help=(
            "wait up to SECONDS for the api container's migrations before the "
            "first cycle (0 disables the wait)"
        ),
    )
    args = parser.parse_args(argv)

    # Shared with the api container (central/logging_config.py) rather than a
    # second basicConfig here. The two copies had already drifted into "the
    # worker logs and the api does not"; this one also gains the logger name the
    # old format omitted, and the LOG_LEVEL knob.
    configure_logging()
    # Same refusal as the api container. The worker writes and reads the same
    # encrypted credentials (SMTP password, webhook URLs) through the same
    # SECRET_KEY-derived key, so "the api refuses but the worker runs" would
    # leave half the deployment operating on a published key.
    settings.assert_secure()
    # SQLite dev convenience only. On Postgres, schema is owned by Alembic — if
    # we create_all() here, we race the api container's `alembic upgrade head`
    # at startup and the next additive migration crashes with a duplicate-table
    # error (see #8 / migration 0007 follow-up).
    if settings.is_sqlite:
        create_all()

    # Close the startup race with the api container's migrations. `docker
    # compose up -d` starts api and worker in PARALLEL, and only the api runs
    # `alembic upgrade head` -- so without this the first cycle can query a
    # schema that is still being built. That is not hypothetical: on 2026-08-05
    # a production stack with 2.4M readings took long enough over fifteen
    # revisions that the worker's first cycle lost seven jobs to
    # UndefinedColumn/UndefinedTable, and every cycle after it was clean. The
    # database was fine; the worker asked too early.
    #
    # Waiting rather than refusing, and giving up rather than waiting forever:
    # an install whose operator genuinely has not migrated must still come up,
    # because the dashboard is how they fix it.
    if args.schema_wait > 0:
        wait_for_schema(timeout=args.schema_wait, where="worker")

    if args.once:
        print(run_cycle(args.interval))
        return 0

    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone="UTC")
    # args=(...) hands the cadence to run_cycle so the liveness stamps record the
    # interval this worker is actually running at, not the module default.
    scheduler.add_job(
        run_cycle, "interval", seconds=args.interval, id="cycle", args=(args.interval,)
    )
    log.info("worker started; cycle every %ss", args.interval)
    run_cycle(args.interval)  # immediate first pass
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
