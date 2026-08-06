"""Failed-sign-in throttling (central/ratelimit.py).

This module had no test coverage at all -- a security control whose behaviour was
asserted nowhere, which is how the concurrency defect below survived. These tests
pin the throttle's contract: both scopes count, the block expires on its own, a
proven password clears it, and the *transition* into a blocked state is reported
to the caller so it can be audited exactly once.

The transition test is the regression: ``record_failure`` compared the live count
to the threshold with ``==``, so two failures racing could both observe
``threshold - 1``, both commit, and carry the bucket past its limit with equality
never seen -- no ``login.throttled`` audit row for the burst that most needed one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from central import models as m
from central import ratelimit


POLICY = ratelimit.Policy(
    enabled=True, user_threshold=3, ip_threshold=5, window_minutes=15
)


def _fail(db, *, username="admin", ip="10.0.0.9", policy=POLICY, now=None):
    return ratelimit.record_failure(
        db, username=username, ip=ip, policy=policy, now=now
    )


def _check(db, *, username="admin", ip="10.0.0.9", policy=POLICY, now=None):
    return ratelimit.check(db, username=username, ip=ip, policy=policy, now=now)


# --------------------------------------------------------------------------- #
# The basic bucket
# --------------------------------------------------------------------------- #
def test_below_the_threshold_nothing_is_refused(db):
    for _ in range(POLICY.user_threshold - 1):
        _fail(db)
    assert _check(db) is None


def test_at_the_threshold_the_username_is_refused(db):
    for _ in range(POLICY.user_threshold):
        _fail(db)
    throttled = _check(db)
    assert throttled is not None
    assert throttled.scope == ratelimit.USER_SCOPE
    assert throttled.retry_after_seconds > 0
    # The message must not disclose which scope filled or whether the account
    # exists -- an invented username and a real one behave identically.
    assert "Too many failed sign-in attempts" in throttled.message()


def test_the_username_bucket_is_case_folded(db):
    """Otherwise the per-username budget is bypassed by pressing shift."""
    for _ in range(POLICY.user_threshold):
        _fail(db, username="Admin")
    assert _check(db, username="admin") is not None


def test_rotating_the_username_still_fills_the_source_bucket(db):
    """The two scopes exist because each is the other's bypass."""
    for i in range(POLICY.ip_threshold):
        _fail(db, username=f"invented-{i}")
    throttled = _check(db, username="yet-another")
    assert throttled is not None
    assert throttled.scope == ratelimit.IP_SCOPE


def test_a_different_source_is_unaffected(db):
    for i in range(POLICY.ip_threshold):
        _fail(db, username=f"invented-{i}", ip="10.0.0.9")
    assert _check(db, username="fresh", ip="10.0.0.10") is None


def test_the_block_expires_as_failures_age_out(db):
    """There is no locked_until column: the refusal has to lapse by itself, or a
    throttle becomes a denial of service against the real administrator."""
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=POLICY.window_minutes + 1)
    for _ in range(POLICY.user_threshold):
        _fail(db, now=old)
    assert _check(db, now=old) is not None
    assert _check(db, now=now) is None


def test_a_proven_password_empties_both_buckets(db):
    for _ in range(POLICY.user_threshold):
        _fail(db)
    ratelimit.clear(db, username="admin", ip="10.0.0.9")
    assert _check(db) is None
    assert db.query(m.LoginAttempt).count() == 0


def test_a_disabled_policy_refuses_nothing_and_counts_nothing(db):
    off = ratelimit.Policy(enabled=False)
    for _ in range(10):
        assert _fail(db, policy=off) is None
    assert _check(db, policy=off) is None
    assert db.query(m.LoginAttempt).count() == 0


# --------------------------------------------------------------------------- #
# The transition report -- the regression
# --------------------------------------------------------------------------- #
def test_the_filling_failure_reports_its_scope(db):
    """The caller audits the transition, so exactly the failure that crosses the
    threshold must report which scope crossed."""
    reported = [_fail(db) for _ in range(POLICY.user_threshold)]
    assert reported[:-1] == [None] * (POLICY.user_threshold - 1)
    assert reported[-1] == ratelimit.USER_SCOPE


def test_overshooting_the_threshold_still_reports(db):
    """The regression. Two failures racing can both read ``threshold - 1`` and
    both commit, so the count jumps the threshold without ever equalling it.

    Simulated deterministically by seeding rows directly -- the outcome under a
    real race is identical and this needs no concurrency to assert. Under the old
    ``== threshold`` comparison the bucket crossed its limit unreported, so no
    ``login.throttled`` audit row was ever written for that burst.
    """
    now = datetime.now(timezone.utc)
    for _ in range(POLICY.user_threshold):
        db.add(m.LoginAttempt(scope=ratelimit.USER_SCOPE, key="admin", ts=now))
    db.flush()
    # This failure takes the count to threshold + 1: equality is skipped entirely.
    assert _fail(db, now=now) == ratelimit.USER_SCOPE


def test_the_source_scope_transition_is_reported_too(db):
    reported = [
        _fail(db, username=f"invented-{i}") for i in range(POLICY.ip_threshold)
    ]
    assert reported[-1] == ratelimit.IP_SCOPE


def test_a_refused_attempt_is_never_counted(db):
    """``check`` runs before ``record_failure`` and the caller returns early, so
    a blocked attacker cannot extend their own block -- and cannot write audit
    rows at will, which is what the ``==`` comparison was protecting.

    Asserted as the property the login route relies on: once ``check`` refuses,
    the route never reaches ``record_failure``, so the row count stops moving.
    """
    for _ in range(POLICY.user_threshold):
        _fail(db)
    before = db.query(m.LoginAttempt).count()
    for _ in range(20):
        if _check(db) is not None:
            continue  # the route returns here without recording
        _fail(db)
    assert db.query(m.LoginAttempt).count() == before


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #
def test_locked_usernames_lists_only_those_over_budget(db):
    for _ in range(POLICY.user_threshold):
        _fail(db, username="locked-out")
    _fail(db, username="merely-clumsy")
    locked = ratelimit.locked_usernames(db, POLICY)
    assert "locked-out" in locked
    assert "merely-clumsy" not in locked


def test_prune_removes_only_rows_past_the_margin(db):
    now = datetime.now(timezone.utc)
    _fail(db, now=now)
    _fail(db, now=now - timedelta(minutes=POLICY.window_minutes * 3))
    removed = ratelimit.prune(db, policy=POLICY, now=now)
    assert removed == 2  # one failure writes a row in EACH scope
    assert db.query(m.LoginAttempt).count() == 2
