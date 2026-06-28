"""Single-leader hardening for the worker.

Covers two independent safety nets so a second worker process can't
double-process and a scheduled report can't be sent twice:

  1. ReportRun marker uniqueness (transactional, race-safe) prevents a
     double-send for one period, while a new period still sends.
  2. The try_leader_lock helper acquires on SQLite (single-process no-op) and
     keeps its acquire/skip/release contract, and run.py's cycle still runs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from central import models as m
from central import reports
from central.db import WORKER_CYCLE_LOCK_KEY, try_leader_lock
from central.runtime import save_settings
from central.worker import run as worker_run


# --------------------------------------------------------------------------- #
# Report-marker idempotency
# --------------------------------------------------------------------------- #
def _enable_weekly(db):
    save_settings(db, {"reports.weekly_day": "mon", "reports.send_hour": "7"})
    save_settings(db, {"reports.weekly_enabled": "on"})


def _stub_delivery(monkeypatch, ok=True):
    sent = []

    def fake(db, rt, subject, body, attachments=None):
        sent.append(subject)
        return ok, "stubbed"

    monkeypatch.setattr(reports, "_deliver", fake)
    return sent


def test_report_marker_unique_constraint_blocks_duplicate(db):
    """A second ReportRun insert for the same (type, period) raises IntegrityError."""
    db.add(m.ReportRun(report_type="weekly", period_key="2026-06-08"))
    db.commit()
    db.add(m.ReportRun(report_type="weekly", period_key="2026-06-08"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    rows = db.scalars(select(m.ReportRun)).all()
    assert len(rows) == 1


def test_claim_report_run_wins_once_then_loses(db):
    """First claim of a period wins (True); a second claim of the same period
    loses (False) without leaving a duplicate row."""
    assert reports._claim_report_run(db, "weekly", "2026-06-08") is True
    assert reports._claim_report_run(db, "weekly", "2026-06-08") is False
    rows = db.scalars(
        select(m.ReportRun).where(
            m.ReportRun.report_type == "weekly",
            m.ReportRun.period_key == "2026-06-08",
        )
    ).all()
    assert len(rows) == 1


def test_weekly_report_sends_once_per_period_then_no_op(db, monkeypatch):
    """Running the weekly path twice for the SAME period sends exactly once and
    records exactly one ReportRun row; the second cycle is a no-op."""
    _enable_weekly(db)
    sent = _stub_delivery(monkeypatch)
    monday = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)

    out1 = reports.run_scheduled_reports(db, now=monday)
    out2 = reports.run_scheduled_reports(db, now=monday)

    assert out1["weekly_report"] == "sent"
    assert out2["weekly_report"] == "skipped"
    assert len(sent) == 1
    rows = db.scalars(
        select(m.ReportRun).where(m.ReportRun.report_type == "weekly")
    ).all()
    assert len(rows) == 1
    assert rows[0].period_key == "2026-06-08"


def test_weekly_report_sends_again_in_a_new_period(db, monkeypatch):
    """A different period (next Monday) claims a fresh marker and sends again."""
    _enable_weekly(db)
    sent = _stub_delivery(monkeypatch)

    reports.run_scheduled_reports(db, now=datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc))
    out = reports.run_scheduled_reports(
        db, now=datetime(2026, 6, 15, 9, 0, tzinfo=timezone.utc)
    )

    assert out["weekly_report"] == "sent"
    assert len(sent) == 2
    periods = sorted(
        r.period_key
        for r in db.scalars(select(m.ReportRun).where(m.ReportRun.report_type == "weekly"))
    )
    assert periods == ["2026-06-08", "2026-06-15"]


def test_weekly_failed_delivery_releases_marker_for_retry(db, monkeypatch):
    """A failed send releases the claim so the next cycle retries -- no period
    is silently dropped, and no stale ReportRun row is left behind."""
    _enable_weekly(db)
    _stub_delivery(monkeypatch, ok=False)
    monday = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)

    out = reports.run_scheduled_reports(db, now=monday)
    assert out["weekly_report"].startswith("failed")
    rows = db.scalars(select(m.ReportRun)).all()
    assert rows == []  # marker released; nothing recorded

    # The retry on a later cycle (now delivering ok) sends and records once.
    sent = _stub_delivery(monkeypatch, ok=True)
    out = reports.run_scheduled_reports(db, now=monday)
    assert out["weekly_report"] == "sent"
    assert len(sent) == 1
    assert len(db.scalars(select(m.ReportRun)).all()) == 1


# --------------------------------------------------------------------------- #
# Leader lock helper
# --------------------------------------------------------------------------- #
def test_try_leader_lock_acquires_on_sqlite():
    """On SQLite the lock is a no-op that always acquires (single-process)."""
    with try_leader_lock() as acquired:
        assert acquired is True


def test_try_leader_lock_releases_cleanly_and_is_reentrant_on_sqlite():
    """The acquire/release contract holds: a second acquisition after the first
    block exits still succeeds (the no-op released cleanly)."""
    with try_leader_lock(WORKER_CYCLE_LOCK_KEY) as a:
        assert a is True
    # After release, acquiring again still works.
    with try_leader_lock(WORKER_CYCLE_LOCK_KEY) as b:
        assert b is True


def test_try_leader_lock_releases_on_exception():
    """An exception inside the block still releases the lock (no leak)."""
    with pytest.raises(RuntimeError):
        with try_leader_lock() as acquired:
            assert acquired is True
            raise RuntimeError("boom")
    # Lock released -> can re-acquire.
    with try_leader_lock() as acquired:
        assert acquired is True


# --------------------------------------------------------------------------- #
# run.py cycle under the lock
# --------------------------------------------------------------------------- #
def test_run_cycle_runs_under_lock(db, monkeypatch):
    """run_cycle acquires the (no-op on SQLite) lock and runs the jobs."""
    out = worker_run.run_cycle()
    # The summary carries keys from the real jobs (e.g. alert evaluation).
    assert "alerts_opened" in out
    assert out.get("skipped") is None


def test_run_cycle_skips_when_not_leader(monkeypatch):
    """If the leader lock is held elsewhere (acquired=False), the cycle SKIPs and
    never opens a DB session / runs jobs."""
    from contextlib import contextmanager

    @contextmanager
    def _not_leader(*a, **k):
        yield False

    ran = {"jobs": False}

    def _fail_jobs(db):
        ran["jobs"] = True
        return {}

    monkeypatch.setattr(worker_run, "try_leader_lock", _not_leader)
    monkeypatch.setattr(worker_run, "_run_jobs", _fail_jobs)
    out = worker_run.run_cycle()
    assert out == {"skipped": "not_leader"}
    assert ran["jobs"] is False
