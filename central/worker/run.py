"""Worker entrypoint.

`python -m central.worker.run`         → run forever on a schedule (APScheduler)
`python -m central.worker.run --once`  → run every job once and exit (CI / cron / demo)
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from central.config import settings
from central.db import SessionLocal, create_all
from central.reports import run_scheduled_reports
from central.worker import jobs

log = logging.getLogger("printer_nanny.worker")

JOBS = (
    jobs.mark_offline_agents,
    jobs.evaluate_alerts,
    jobs.check_maintenance_due,
    # Re-send any failed/pending channel deliveries that are due (backoff).
    jobs.retry_deliveries,
    jobs.forecast_supplies,
    # Cheap no-op unless a weekly/monthly report is due (marker-gated).
    run_scheduled_reports,
)


def run_cycle() -> dict:
    summary: dict = {}
    db = SessionLocal()
    try:
        for job in JOBS:
            try:
                summary.update(job(db))
            except Exception:  # noqa: BLE001 - keep the cycle alive on a single job failure
                log.exception("job %s failed", job.__name__)
                db.rollback()
    finally:
        db.close()
    log.info("worker cycle: %s", summary)
    return summary


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Printer Nanny worker")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--interval", type=int, default=60, help="seconds between cycles")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # SQLite dev convenience only. On Postgres, schema is owned by Alembic — if
    # we create_all() here, we race the api container's `alembic upgrade head`
    # at startup and the next additive migration crashes with a duplicate-table
    # error (see #8 / migration 0007 follow-up).
    if settings.is_sqlite:
        create_all()

    if args.once:
        print(run_cycle())
        return 0

    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_cycle, "interval", seconds=args.interval, id="cycle")
    log.info("worker started; cycle every %ss", args.interval)
    run_cycle()  # immediate first pass
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
