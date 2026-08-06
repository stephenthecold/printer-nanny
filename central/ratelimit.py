"""Failed-sign-in throttling for the local password login.

Audit logging already recorded every failure with the attempted username, which
tells an operator afterwards that they were attacked. Nothing slowed the attack
down while it happened. This does.

**Why the database.** The counter has to be shared and it has to be durable. The
api and worker are separate containers; the api is scalable to more than one
replica, and two replicas with in-process counters give an attacker twice the
budget with no notice. Worse, in-process state resets on restart -- and restarts
are something an attacker can *cause* (crash a worker, wait for a deploy) or
simply wait for. A DB row survives all of it, is visible to the operator, and
commits in the same transaction as the audit row that explains it.

**Two scopes, because each is the other's bypass.**

* ``user`` -- a normalised username. Stops password spraying against one account
  from anywhere, including a botnet. Bypassed by rotating the username.
* ``ip`` -- the resolved source (central/net.py). Stops username rotation from one
  source. Bypassed by having many sources.

Neither alone is worth much; together they cost an attacker either many accounts
or many hosts.

**Why a leaky bucket and not a lockout.** There is no ``locked_until`` column.
"Locked" means "at least N failures inside the trailing window", so the block
expires by itself as the failures age out. That is what stops this from becoming a
denial of service against a real administrator: an attacker cannot make the block
last longer than the window, cannot make it permanent, and cannot escalate it,
because attempts that are *refused* are not counted -- only attempts that actually
got a password checked. The remaining exposure is that an attacker who keeps the
bucket full is racing the real admin for each slot as it drains, which is inherent
to any throttle that has no bypass (and a throttle with a
"correct-password-gets-through" bypass has no teeth at all: deciding whether to
bypass means checking the password, which is exactly the guess you were trying to
ration). The mitigations that matter are elsewhere and are deliberate:

* an admin who is **already signed in stays signed in** -- this gates
  authentication, not authorisation, so nobody is thrown out of a live session;
* **SSO is not throttled** (no password is presented to us), so an install with an
  IdP always has a way in;
* another admin can **clear a throttle** from /manage/users, and it is audited;
* the window is short (15 minutes by default) and configurable.

**Unknown usernames count.** A spray of invented usernames is the attack, so
refusing to count them would leave the front door open. It also means the
throttled response leaks nothing about which accounts exist -- a real and an
invented username behave identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from central import models as m

USER_SCOPE = "user"
IP_SCOPE = "ip"

#: How far back a prune reaches beyond the live window. Rows older than the window
#: can never affect a decision again; the small extra margin keeps a just-expired
#: failure around long enough to be visibly counted in a debugging session.
_PRUNE_MARGIN = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; they are UTC by construction here."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def normalise_username(username: str) -> str:
    """The bucket key for an attempted username.

    Case-folded so ``Admin`` and ``admin`` share a budget -- otherwise the
    per-username throttle is bypassed by pressing shift. Truncated to the column
    width so an absurdly long submission cannot fail the insert (and take the
    login route down with it).
    """
    return (username or "").strip().lower()[:200]


@dataclass(frozen=True)
class Policy:
    """The operator's tuning, read from runtime settings."""

    enabled: bool = True
    user_threshold: int = 10
    ip_threshold: int = 50
    window_minutes: int = 15

    @classmethod
    def from_settings(cls, values: Dict[str, Any]) -> "Policy":
        def _int(key: str, fallback: int, floor: int = 1) -> int:
            try:
                return max(int(values.get(key, fallback)), floor)
            except (TypeError, ValueError):
                return fallback

        return cls(
            enabled=bool(values.get("auth.lockout_enabled", True)),
            user_threshold=_int("auth.lockout_user_threshold", 10),
            ip_threshold=_int("auth.lockout_ip_threshold", 50),
            window_minutes=_int("auth.lockout_window_min", 15),
        )

    @property
    def window(self) -> timedelta:
        return timedelta(minutes=self.window_minutes)


@dataclass(frozen=True)
class Throttled:
    """A refusal, and enough detail to explain it without leaking anything."""

    scope: str
    failures: int
    retry_after_seconds: int

    @property
    def retry_after_minutes(self) -> int:
        return max(1, (self.retry_after_seconds + 59) // 60)

    def message(self) -> str:
        minutes = self.retry_after_minutes
        unit = "minute" if minutes == 1 else "minutes"
        return (
            f"Too many failed sign-in attempts. Try again in about {minutes} {unit}."
        )

    def audit_detail(self) -> str:
        return (f"scope={self.scope} failures={self.failures} "
                f"retry_after_s={self.retry_after_seconds}")


def _counts(db: Session, *, username: str, ip: str, since: datetime) -> Dict[str, int]:
    rows = db.execute(
        select(m.LoginAttempt.scope, func.count())
        .where(m.LoginAttempt.ts >= since)
        .where(
            or_(
                and_(m.LoginAttempt.scope == USER_SCOPE, m.LoginAttempt.key == username),
                and_(m.LoginAttempt.scope == IP_SCOPE, m.LoginAttempt.key == ip),
            )
        )
        .group_by(m.LoginAttempt.scope)
    ).all()
    return {str(scope): int(count) for scope, count in rows}


def _retry_after_seconds(
    db: Session, *, scope: str, key: str, since: datetime,
    threshold: int, failures: int, policy: Policy, now: datetime,
) -> int:
    """When the bucket next has room: the moment the (failures - threshold + 1)-th
    oldest failure leaves the window."""
    offset = max(failures - threshold, 0)
    oldest = db.scalar(
        select(m.LoginAttempt.ts)
        .where(m.LoginAttempt.scope == scope, m.LoginAttempt.key == key)
        .where(m.LoginAttempt.ts >= since)
        .order_by(m.LoginAttempt.ts)
        .offset(offset)
        .limit(1)
    )
    oldest = _aware(oldest)
    if oldest is None:
        return int(policy.window.total_seconds())
    return max(1, int((oldest + policy.window - now).total_seconds()))


def check(
    db: Session, *, username: str, ip: str, policy: Policy,
    now: Optional[datetime] = None,
) -> Optional[Throttled]:
    """Refuse this attempt, or None to let it be checked against the password."""
    if not policy.enabled:
        return None
    now = now or _now()
    since = now - policy.window
    key = normalise_username(username)
    counts = _counts(db, username=key, ip=ip, since=since)
    for scope, bucket, threshold in (
        (USER_SCOPE, key, policy.user_threshold),
        (IP_SCOPE, ip, policy.ip_threshold),
    ):
        failures = counts.get(scope, 0)
        if failures < threshold:
            continue
        return Throttled(
            scope=scope,
            failures=failures,
            retry_after_seconds=_retry_after_seconds(
                db, scope=scope, key=bucket, since=since, threshold=threshold,
                failures=failures, policy=policy, now=now,
            ),
        )
    return None


def record_failure(
    db: Session, *, username: str, ip: str, policy: Policy,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Count one failure in both scopes. Returns the scope that just filled, if any.

    Flushed, not committed: the caller commits it together with the audit row for
    the same failure, so a rolled-back request leaves neither behind.

    The returned scope exists so the caller can audit the *transition* rather than
    every subsequent refusal. Auditing each refused attempt would let an attacker
    write unbounded rows into the audit log at no cost -- and the audit viewer
    shows a fixed window, so flooding it is how you hide the rest of an attack.
    """
    if not policy.enabled:
        return None
    now = now or _now()
    key = normalise_username(username)
    db.add(m.LoginAttempt(scope=USER_SCOPE, key=key, ts=now))
    db.add(m.LoginAttempt(scope=IP_SCOPE, key=ip, ts=now))
    db.flush()
    # Keep each touched bucket bounded even where no worker runs (a bare
    # `uvicorn` dev box has no prune job). Indexed, and only ever the two keys
    # this request touched.
    cutoff = now - policy.window * _PRUNE_MARGIN
    db.execute(
        delete(m.LoginAttempt)
        .where(m.LoginAttempt.ts < cutoff)
        .where(
            or_(
                and_(m.LoginAttempt.scope == USER_SCOPE, m.LoginAttempt.key == key),
                and_(m.LoginAttempt.scope == IP_SCOPE, m.LoginAttempt.key == ip),
            )
        )
    )
    counts = _counts(db, username=key, ip=ip, since=now - policy.window)
    for scope, threshold in ((USER_SCOPE, policy.user_threshold),
                             (IP_SCOPE, policy.ip_threshold)):
        # ``>=``, not ``==``. Two failures racing can both read ``threshold - 1``
        # and then both commit, so the bucket crosses its limit with equality
        # never observed -- and the transition goes unaudited precisely during
        # the burst that makes it worth auditing.
        #
        # This cannot flood the audit log, which is what the equality was
        # protecting: ``check`` runs BEFORE this and refuses without calling
        # ``record_failure`` at all once the bucket is full, so a failure only
        # reaches this line when it was let through. Past the threshold the only
        # way here is the race itself, which is bounded by the number of
        # in-flight requests -- and a duplicate audit row for one real
        # transition is strictly better than no row for it.
        if counts.get(scope, 0) >= threshold:
            return scope
    return None


def clear(db: Session, *, username: Optional[str] = None, ip: Optional[str] = None) -> int:
    """Forget the failures for a username and/or a source. Returns rows removed.

    Called on a successful sign-in for both scopes. Clearing the source scope on
    success is deliberate: behind a proxy the whole office shares one source
    bucket, and a human proving they know a password is the best available
    evidence that the bucket is full of typos rather than of an attack. It
    concedes that somebody who *already holds valid credentials* can reset that
    counter -- which buys them nothing, because the per-username budget they are
    actually spraying against is untouched by it.
    """
    conditions = []
    if username is not None:
        conditions.append(
            and_(m.LoginAttempt.scope == USER_SCOPE,
                 m.LoginAttempt.key == normalise_username(username))
        )
    if ip is not None:
        conditions.append(
            and_(m.LoginAttempt.scope == IP_SCOPE, m.LoginAttempt.key == ip)
        )
    if not conditions:
        return 0
    result = db.execute(delete(m.LoginAttempt).where(or_(*conditions)))
    return int(result.rowcount or 0)


def locked_usernames(db: Session, policy: Policy,
                     now: Optional[datetime] = None) -> Dict[str, int]:
    """``{username: failures}`` for every username currently over its budget.

    One grouped query for the whole users page, rather than one per row.
    """
    if not policy.enabled:
        return {}
    now = now or _now()
    rows = db.execute(
        select(m.LoginAttempt.key, func.count())
        .where(m.LoginAttempt.scope == USER_SCOPE)
        .where(m.LoginAttempt.ts >= now - policy.window)
        .group_by(m.LoginAttempt.key)
        .having(func.count() >= policy.user_threshold)
    ).all()
    return {str(key): int(count) for key, count in rows}


def prune(db: Session, *, older_than: Optional[datetime] = None,
          policy: Optional[Policy] = None, now: Optional[datetime] = None) -> int:
    """Delete rows that can no longer affect any decision. Returns rows removed."""
    now = now or _now()
    if older_than is None:
        window = (policy or Policy()).window
        older_than = now - window * _PRUNE_MARGIN
    result = db.execute(delete(m.LoginAttempt).where(m.LoginAttempt.ts < older_than))
    return int(result.rowcount or 0)
