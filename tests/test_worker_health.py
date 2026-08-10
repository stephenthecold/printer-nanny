"""Worker liveness stamps, /healthz vs /readyz, and the dashboard banner.

The gap being closed: a dead worker was undetectable. No job recorded a last-run
timestamp, /healthz answered 200 with the database unreachable, and because
``mark_offline_agents`` is itself a worker job a dead worker also stopped marking
agents offline -- so the dashboard showed a green fleet over frozen data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from central import health
from central import models as m
from central.main import app
from central.security import hash_password
from central.worker import health as worker_health_cli
from central.worker import run as worker_run


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _user(db, role=m.UserRole.admin, username="admin") -> TestClient:
    db.add(m.User(username=username, password_hash=hash_password("pw"), role=role))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": username, "password": "pw"},
             follow_redirects=False)
    return cli


# --------------------------------------------------------------------------- #
# The stamp itself
# --------------------------------------------------------------------------- #
def test_cycle_stamps_every_job_with_a_real_timestamp(db):
    """One cycle writes one fresh row per job, with the cadence recorded."""
    before = _now()
    worker_run.run_cycle(60)

    rows = {r.job: r for r in db.scalars(select(m.WorkerJobRun))}
    expected = {job.__name__ for job in worker_run.JOBS}
    assert set(rows) == expected
    for row in rows.values():
        assert row.last_success_at is not None
        assert health._aware(row.last_success_at) >= before - timedelta(seconds=5)
        assert row.consecutive_failures == 0
        assert row.last_error_type is None
        assert row.expected_interval_seconds == 60


def test_stamp_records_the_interval_the_worker_actually_runs_at(db):
    """A slow-polling deployment stamps its own cadence, so the staleness
    threshold derives from configuration instead of a hardcoded age."""
    worker_run.run_cycle(900)
    row = db.scalar(select(m.WorkerJobRun))
    assert row.expected_interval_seconds == 900
    assert health.stale_after_seconds(900) == 2700.0


def test_failing_job_is_stamped_as_a_failure_not_a_success(db, monkeypatch):
    """A job that raises every cycle must NOT look fresh -- the whole reason the
    stamp is per job rather than one global 'cycle ran' timestamp."""
    def boom(_db, now=None):
        raise RuntimeError("connection to server at 10.0.0.1 failed: password=hunter2")

    monkeypatch.setattr(worker_run, "JOBS", (worker_run.jobs.mark_offline_agents, boom))
    worker_run.run_cycle(60)

    rows = {r.job: r for r in db.scalars(select(m.WorkerJobRun))}
    assert rows["mark_offline_agents"].last_success_at is not None
    failed = rows["boom"]
    assert failed.last_success_at is None
    assert failed.last_error_at is not None
    assert failed.consecutive_failures == 1
    # Class name only -- the message carried a host and a password.
    assert failed.last_error_type == "RuntimeError"
    assert "hunter2" not in str(failed.last_error_type)
    assert "10.0.0.1" not in str(failed.last_error_type)


def test_consecutive_failures_accumulate_then_reset_on_success(db, monkeypatch):
    state = {"fail": True}

    def flaky(_db, now=None):
        if state["fail"]:
            raise ValueError("nope")
        return {"flaky": "ok"}

    monkeypatch.setattr(worker_run, "JOBS", (flaky,))
    worker_run.run_cycle(60)
    worker_run.run_cycle(60)
    db.expire_all()
    assert db.get(m.WorkerJobRun, "flaky").consecutive_failures == 2

    state["fail"] = False
    worker_run.run_cycle(60)
    db.expire_all()
    row = db.get(m.WorkerJobRun, "flaky")
    assert row.consecutive_failures == 0
    assert row.last_error_type is None
    assert row.last_success_at is not None


def test_a_standby_worker_that_skips_the_cycle_stamps_nothing(db, monkeypatch):
    """Only the leader stamps; the loser of the lock race writes no rows (and
    reads the leader's, so it still reports healthy)."""
    from contextlib import contextmanager

    @contextmanager
    def _not_leader(*a, **k):
        yield False

    monkeypatch.setattr(worker_run, "try_leader_lock", _not_leader)
    assert worker_run.run_cycle(60) == {"skipped": "not_leader"}
    assert db.scalars(select(m.WorkerJobRun)).all() == []


# --------------------------------------------------------------------------- #
# worker_health / staleness derived from the configured interval
# --------------------------------------------------------------------------- #
def test_fresh_seeded_system_with_no_worker_yet_is_never_ran_not_stale(db):
    """A brand-new install must not false-alarm: no stamps reads as never_ran."""
    assert health.worker_health(db)["state"] == health.STATE_NEVER_RAN


def test_healthy_worker_reports_ok(db):
    worker_run.run_cycle(60)
    out = health.worker_health(db)
    assert out["state"] == health.STATE_OK
    assert out["stale_jobs"] == []
    assert len(out["jobs"]) == len(worker_run.JOBS)


def test_backdated_stamp_goes_stale_and_names_the_job(db):
    worker_run.run_cycle(60)
    row = db.get(m.WorkerJobRun, "evaluate_alerts")
    row.last_success_at = _now() - timedelta(minutes=30)
    db.commit()

    out = health.worker_health(db)
    assert out["state"] == health.STATE_STALE
    assert out["stale_jobs"] == ["evaluate_alerts"]


def test_a_job_failing_every_cycle_is_caught_without_waiting_out_the_threshold(db, monkeypatch):
    """A climbing failure count is positive evidence; age alone is only absence of
    evidence. At --interval 900 the age threshold is 45 min, so waiting for it
    would leave a known-broken job green for 45 minutes."""
    def boom(_db, now=None):
        raise RuntimeError("nope")
    boom.__name__ = "evaluate_alerts"

    # Succeed once first, so last_success_at is recent and "overdue" can't be
    # what trips the check.
    worker_run.run_cycle(900)
    monkeypatch.setattr(
        worker_run, "JOBS",
        tuple(boom if j.__name__ == "evaluate_alerts" else j for j in worker_run.JOBS),
    )
    for _ in range(health.WORKER_MAX_CONSECUTIVE_FAILURES - 1):
        worker_run.run_cycle(900)
    # Below the cap and not overdue -> still ok.
    assert health.worker_health(db)["state"] == health.STATE_OK

    worker_run.run_cycle(900)  # crosses the cap
    out = health.worker_health(db)
    assert out["state"] == health.STATE_STALE
    assert out["stale_jobs"] == ["evaluate_alerts"]
    detail = [j for j in out["jobs"] if j["job"] == "evaluate_alerts"][0]
    assert detail["reason"] == "failing"          # not "overdue"
    assert detail["age_seconds"] < detail["threshold_seconds"]
    assert detail["consecutive_failures"] == health.WORKER_MAX_CONSECUTIVE_FAILURES


def test_overdue_and_failing_are_distinguished_in_the_reason(db):
    worker_run.run_cycle(60)
    row = db.get(m.WorkerJobRun, "retry_deliveries")
    row.last_success_at = _now() - timedelta(hours=1)
    db.commit()
    detail = [j for j in health.worker_health(db)["jobs"] if j["job"] == "retry_deliveries"][0]
    assert detail["reason"] == "overdue"


def test_threshold_scales_with_interval_so_slow_fleets_dont_false_alarm(db):
    """The same 10-minute-old stamp is stale at --interval 60 and fine at 900."""
    worker_run.run_cycle(60)
    ten_min_ago = _now() - timedelta(minutes=10)
    for row in db.scalars(select(m.WorkerJobRun)):
        row.last_success_at = ten_min_ago
    db.commit()
    assert health.worker_health(db)["state"] == health.STATE_STALE

    for row in db.scalars(select(m.WorkerJobRun)):
        row.expected_interval_seconds = 900  # 3x900 = 45 min tolerance
    db.commit()
    assert health.worker_health(db)["state"] == health.STATE_OK


def test_short_interval_uses_the_floor_not_a_flapping_threshold(db):
    """At --interval 5, 3x would be 15s; the floor keeps it at 120s."""
    assert health.stale_after_seconds(5) == float(health.WORKER_STALE_FLOOR_SECONDS)
    # A row with no recorded interval is treated as running at the default
    # cadence (not clamped to the floor), matching the column's server_default.
    default_threshold = health.DEFAULT_CYCLE_SECONDS * health.WORKER_STALE_MULTIPLIER
    assert health.stale_after_seconds(0) == default_threshold
    assert health.stale_after_seconds(None) == default_threshold


def test_row_for_a_retired_job_cannot_wedge_readiness_forever(db):
    """A job removed from JOBS in an upgrade leaves a row that would otherwise
    never go fresh again -- unknown job names are ignored on read."""
    worker_run.run_cycle(60)
    db.add(m.WorkerJobRun(
        job="a_job_we_deleted",
        last_success_at=_now() - timedelta(days=9),
        expected_interval_seconds=60,
    ))
    db.commit()
    assert health.worker_health(db)["state"] == health.STATE_OK


def test_missing_table_reads_unknown_so_a_migration_window_is_not_an_outage(db):
    m.WorkerJobRun.__table__.drop(bind=db.get_bind())
    try:
        assert health.worker_health(db)["state"] == health.STATE_UNKNOWN
    finally:
        m.WorkerJobRun.__table__.create(bind=db.get_bind())


# --------------------------------------------------------------------------- #
# /healthz (liveness) vs /readyz (readiness)
# --------------------------------------------------------------------------- #
def test_healthz_is_liveness_only_and_never_touches_the_db(db, monkeypatch):
    """It answered 200 with the DB down before this change and still does -- by
    design now. /readyz is what fails; see the next test."""
    def _explode(*a, **k):
        raise AssertionError("/healthz must not query the database")

    monkeypatch.setattr(health, "database_ok", _explode)
    resp = TestClient(app).get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readyz_503s_when_the_database_is_unreachable(db, monkeypatch):
    monkeypatch.setattr("central.main.database_ok", lambda _db: False)
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"] == {"database": "error", "worker": "unknown"}


def test_readyz_ok_on_a_freshly_seeded_system_before_the_worker_runs(db):
    """No false alarm on a new deployment: never_ran passes the probe."""
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["checks"]["worker"] == health.STATE_NEVER_RAN


def test_readyz_ok_with_a_healthy_worker(db):
    worker_run.run_cycle(60)
    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "checks": {"database": "ok", "worker": "ok"}}


def test_readyz_503s_and_names_the_stale_job(db):
    worker_run.run_cycle(60)
    row = db.get(m.WorkerJobRun, "mark_offline_agents")
    row.last_success_at = _now() - timedelta(hours=2)
    db.commit()

    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["worker"] == "stale"
    # Job names are withheld here on purpose: the route is unauthenticated and
    # publicly reverse-proxied, and naming the wedged job advertises that fleet
    # monitoring is blind. Operators read the names off the dashboard banner.
    assert "stale_jobs" not in body
    assert "mark_offline_agents" not in resp.text


def test_readyz_leaks_no_internals(db, monkeypatch):
    """Terse by contract: this route is unauthenticated behind the proxy, so
    neither the DB-down body nor the stale body may carry deployment detail."""
    worker_run.run_cycle(60)
    row = db.get(m.WorkerJobRun, "evaluate_alerts")
    row.last_success_at = _now() - timedelta(hours=2)
    row.last_error_type = "OperationalError"
    db.commit()

    cli = TestClient(app)
    bodies = [cli.get("/readyz").text]
    monkeypatch.setattr("central.main.database_ok", lambda _db: False)
    bodies.append(cli.get("/readyz").text)

    for body in bodies:
        for leak in ("Traceback", "sqlite", "postgresql", "psycopg", "password",
                     "SECRET_KEY", "/home/", "OperationalError", "SELECT"):
            assert leak not in body, f"{leak!r} leaked in {body!r}"


def test_readyz_worker_skip_narrows_to_the_db_check(db):
    """What the api container's healthcheck uses: a stalled worker must not mark
    the api container unhealthy."""
    worker_run.run_cycle(60)
    row = db.get(m.WorkerJobRun, "evaluate_alerts")
    row.last_success_at = _now() - timedelta(hours=2)
    db.commit()

    cli = TestClient(app)
    assert cli.get("/readyz").status_code == 503
    resp = cli.get("/readyz?worker=skip")
    assert resp.status_code == 200
    assert resp.json()["checks"]["worker"] == "skipped"


def test_readyz_unknown_worker_param_fails_closed(db):
    """A typo must not silently weaken the check."""
    worker_run.run_cycle(60)
    row = db.get(m.WorkerJobRun, "evaluate_alerts")
    row.last_success_at = _now() - timedelta(hours=2)
    db.commit()
    assert TestClient(app).get("/readyz?worker=nonsense").status_code == 503


# --------------------------------------------------------------------------- #
# Worker container healthcheck (command-based)
# --------------------------------------------------------------------------- #
def test_worker_cli_probe_exit_codes(db, capsys):
    # Never stamped -> unhealthy (this is the probe that catches "worker never
    # started"; compose gives it a start_period for first boot).
    assert worker_health_cli.main([]) == 1
    assert "never_ran" in capsys.readouterr().out

    worker_run.run_cycle(60)
    assert worker_health_cli.main([]) == 0
    assert "healthy" in capsys.readouterr().out

    row = db.get(m.WorkerJobRun, "forecast_supplies")
    row.last_success_at = _now() - timedelta(hours=3)
    db.commit()
    assert worker_health_cli.main([]) == 1
    out = capsys.readouterr().out
    assert "stale" in out and "forecast_supplies" in out


def test_worker_cli_probe_unhealthy_when_db_unreachable(db, monkeypatch):
    monkeypatch.setattr(worker_health_cli, "database_ok", lambda _db: False)
    assert worker_health_cli.main([]) == 1


# --------------------------------------------------------------------------- #
# Dashboard indicator
# --------------------------------------------------------------------------- #
def test_no_banner_on_a_healthy_system(db):
    cli = _user(db)
    worker_run.run_cycle(60)
    body = cli.get("/").text
    assert "Background worker stalled" not in body
    assert "never reported in" not in body


def test_banner_appears_when_the_worker_is_stalled(db):
    cli = _user(db)
    worker_run.run_cycle(60)
    row = db.get(m.WorkerJobRun, "mark_offline_agents")
    row.last_success_at = _now() - timedelta(hours=2)
    db.commit()

    body = cli.get("/").text
    assert "Background worker stalled" in body
    assert "mark_offline_agents" in body
    # The banner is on base.html, so it follows the operator around.
    assert "Background worker stalled" in cli.get("/manage/agents").text
    assert "Background worker stalled" in cli.get("/settings").text


def test_banner_says_never_reported_when_no_worker_has_run(db):
    cli = _user(db)
    assert "never reported in" in cli.get("/").text


def test_banner_hidden_from_anonymous_visitors_on_the_login_page(db):
    """base.html also renders /login. An unauthenticated visitor must not be told
    the deployment is degraded, nor handed internal job names."""
    worker_run.run_cycle(60)
    row = db.get(m.WorkerJobRun, "mark_offline_agents")
    row.last_success_at = _now() - timedelta(hours=2)
    db.commit()

    body = TestClient(app).get("/login").text
    assert "Background worker" not in body
    assert "mark_offline_agents" not in body


def test_banner_shown_to_tech_users_not_just_admins(db):
    """Role gating excludes only client_readonly -- a tech on call needs this."""
    cli = _user(db, role=m.UserRole.tech, username="tech")
    worker_run.run_cycle(60)
    row = db.get(m.WorkerJobRun, "retry_deliveries")
    row.last_success_at = _now() - timedelta(hours=2)
    db.commit()
    assert "Background worker stalled" in cli.get("/").text


def test_banner_hidden_from_client_readonly_portal_users(db):
    """Internal job names are not a tenant's business."""
    client = m.Client(name="Acme")
    db.add(client)
    db.commit()
    db.add(m.User(username="cust", password_hash=hash_password("pw"),
                  role=m.UserRole.client_readonly, client_id=client.id))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "cust", "password": "pw"},
             follow_redirects=False)

    body = cli.get("/portal").text
    assert "Background worker" not in body
    assert "never reported in" not in body


def test_worker_banner_is_none_when_healthy_and_never_raises(db):
    worker_run.run_cycle(60)
    assert health.worker_banner(db, None) is None
    # A broken session must not 500 every dashboard page.
    class _Broken:
        def get_bind(self):
            raise RuntimeError("db gone")

    assert health.worker_banner(_Broken(), None) is None


# --------------------------------------------------------------------------- #
# "Starting" is not "stalled"
#
# 2026-08-10, a real install: one table had gone missing from a database already
# stamped at head, so the worker spent its full 300s schema wait on every
# restart. It writes no liveness stamp while it waits, the staleness threshold
# at the default cadence is 180s, and the previous worker's stamps were still in
# the table -- so the dashboard reported a stalled worker with all fifteen jobs
# down, once per restart, about a worker that was doing exactly what it should.
# --------------------------------------------------------------------------- #
def _stale_stamp(db, job="evaluate_alerts", age=4000, interval=60):
    """An upgrade restart: a previous worker's stamps, nothing refreshing them."""
    db.query(m.WorkerJobRun).delete()
    db.add(
        m.WorkerJobRun(
            job=job,
            last_success_at=_now() - timedelta(seconds=age),
            expected_interval_seconds=interval,
        )
    )
    db.commit()


def test_stale_stamps_without_a_marker_still_read_as_stale(db):
    """The behaviour being refined, asserted first so the change is visible: with
    no marker this is exactly as before, and 'starting' cannot be a blanket
    excuse for a worker that never said it was starting."""
    _stale_stamp(db)
    assert health.worker_health(db)["state"] == health.STATE_STALE
    assert worker_health_cli.main() == 1


def test_a_worker_in_its_schema_wait_reads_as_starting(db):
    _stale_stamp(db)
    health.mark_worker_starting(db, 300)

    got = health.worker_health(db)
    assert got["state"] == health.STATE_STARTING
    assert got["stale_jobs"] == []
    # The container probe must PASS: a worker inside its stated budget is doing
    # the right thing, and failing it here leaves compose's start_period as the
    # only thing between a slow migration and an unhealthy container.
    assert worker_health_cli.main() == 0


def test_the_banner_does_not_accuse_a_starting_worker(db):
    _stale_stamp(db)
    client = _user(db)
    health.mark_worker_starting(db, 300)
    assert health.worker_banner(db, db.scalars(select(m.User)).first()) is None
    # and /readyz must not 503 for it either
    assert client.get("/readyz").status_code == 200
    assert client.get("/readyz").json()["checks"]["worker"] == health.STATE_STARTING


def test_the_marker_never_appears_as_a_job(db):
    """It is not a job and never runs. Listed to an operator it would read as the
    one job that never completes -- an invented fault."""
    _stale_stamp(db)
    health.mark_worker_starting(db, 300)
    assert health.worker_health(db)["jobs"] == []
    health.clear_worker_starting(db)
    assert health.WORKER_STARTING_JOB not in {
        j["job"] for j in health.worker_health(db)["jobs"]
    }


def test_an_expired_marker_stops_sheltering_a_dead_worker(db):
    """The one that matters. A worker that dies mid-wait must go back to being
    judged on its stamps -- a 'starting' that never ages out would hide a dead
    worker behind a reassuring word, which is the failure this module exists to
    prevent."""
    _stale_stamp(db)
    health.mark_worker_starting(db, 300)
    row = db.get(m.WorkerJobRun, health.WORKER_STARTING_JOB)
    row.last_success_at = _now() - timedelta(
        seconds=300 + health.WORKER_STARTING_GRACE_SECONDS + 60
    )
    db.commit()

    assert health.worker_health(db)["state"] == health.STATE_STALE
    assert worker_health_cli.main() == 1


def test_clearing_the_marker_restores_the_normal_rules(db):
    _stale_stamp(db)
    health.mark_worker_starting(db, 300)
    assert health.worker_health(db)["state"] == health.STATE_STARTING
    health.clear_worker_starting(db)
    assert db.get(m.WorkerJobRun, health.WORKER_STARTING_JOB) is None
    assert health.worker_health(db)["state"] == health.STATE_STALE


def test_marking_never_raises_when_the_table_is_missing(db):
    """Called on the startup path, before the schema is known to exist. A worker
    that refused to boot because it could not write a *marker* would be strictly
    worse than the banner it prevents."""
    m.WorkerJobRun.__table__.drop(bind=db.get_bind())
    try:
        health.mark_worker_starting(db, 300)  # must not raise
        health.clear_worker_starting(db)  # must not raise
    finally:
        m.WorkerJobRun.__table__.create(bind=db.get_bind())
