"""Collector redundancy: one subnet, two agents, never both collecting.

PrintFleet publishes that 10-20% of SMB data collectors stop working at some
point. Before this module a site agent owned its subnets outright, so when it
died the site simply stopped reporting and the dashboard froze on stale data
with nothing to fail over to.

The whole difficulty is **split brain**. Two agents sweeping one subnet at once
means two reading series for the same printers, and that is not a cosmetic
duplicate -- ``queries._printed_pages_for_printer`` bills by summing POSITIVE
page-count deltas over readings ordered by ``ts``, and ``ts`` is stamped by the
agent. Two collectors with clocks a few seconds apart produce a series that dips
and recovers (100, 110, 105, 110), and every recovery re-charges pages that were
already billed. So the requirement is not "prefer one collector", it is "prove
two collectors cannot both believe they own a subnet".

Mechanism: a **lease**, granted only by a conditional UPDATE
---------------------------------------------------------
``subnets.collector_agent_id`` names the agent currently entitled to collect,
and ``subnets.collector_lease_expires_at`` says until when. Ownership only ever
changes through a single-row UPDATE whose WHERE clause states what the row must
still look like, and the statement's **rowcount is the interlock** -- the same
discipline ``services.redeem_claim_token`` uses so two boxes booted from one
image cannot both redeem a claim code. Nothing here reads a row, decides, and
then writes; a read-then-write would let two workers or two heartbeats pass the
same check.

That gives the central-side guarantee directly: a row holds exactly one
``collector_agent_id``, and every transition is one atomic compare-and-swap on
that row, so at most one agent is ever the holder. The loser sees rowcount 0 and
is told it does not hold the subnet.

Why that is not sufficient on its own, and what closes it
--------------------------------------------------------
Central's answer only binds an agent that can hear it. The agent deliberately
keeps polling through a central outage (``runner.TargetCache`` exists precisely
so an outage does not punch a permanent hole in the meters), so a primary that
loses its link to central but still reaches the printers would keep collecting
while a standby was handed the subnet -- both collecting, neither wrong.

So the lease is a **time bounded grant that the holder enforces on itself**. The
agent records ``time.monotonic()`` *before* it sends the heartbeat, and treats
the grant as expiring at ``that instant + lease_seconds``. Central sets the
row's expiry at ``its own now + lease_seconds``, and its now is necessarily
*later* than the instant the agent sampled (the request had to travel). Hence:

    agent-side deadline  <=  central-side expiry

for every grant, always, with no assumption whatsoever about the two clocks
agreeing -- the agent measures a duration on a monotonic clock, central measures
a duration on its own. The holder therefore always stops collecting *before*
central will consider the lease free, so the window in which a successor could
be granted a subnet the predecessor is still sweeping does not exist.

The third layer, because billing reads those rows
-------------------------------------------------
``admits()`` refuses a reading pushed by an agent that does not hold the
subnet's lease. This is not redundant with the two layers above: it is what
protects the append-only ``readings`` table from an agent too old to know what a
lease is, from a hand-rolled client with a stolen key, and from any future bug
above it. It admits exactly one exception -- the immediate predecessor replaying
its spool for the period it *did* hold the lease -- because those readings are
history, strictly older than the successor's first one, and dropping them would
punch the very hole in the meters the spool exists to prevent.

Takeover is conservative, in the shape of ``services.adopt_by_name``
--------------------------------------------------------------------
A takeover is a weaker claim than an operator's assignment, so every way it can
be wrong is refused rather than resolved:

* **Only the worker moves a lease between agents.** A heartbeat may renew its
  own lease, and may pick up a lease nobody holds, but it can never take one
  from another agent. So no amount of agent-side eagerness can displace anyone.
* **The lease must have lapsed**, and the CAS re-checks that in the WHERE
  clause. A primary that is merely slow renews, the compare-and-swap misses, and
  nothing moves. That is the proof, not the intention.
* **The holder must look gone** -- ``offline`` per ``mark_offline_agents`` *and*
  silent for ``collector.takeover_after_seconds``, which is deliberately longer
  than the offline grace so "briefly missed a heartbeat" and "stopped working"
  are different events.
* **The successor must be alive.** Handing a subnet to a second dead agent turns
  one outage into two and loses the audit trail of who is responsible.
* **Exactly one candidate**, structurally: primary and standby, and a subnet
  with no primary may not be given a standby at all.

Hand-back: deliberately NOT automatic
-------------------------------------
When the primary comes back it does **not** reclaim the subnet. The standby is
collecting correctly, so a second handover buys nothing and costs another
transition -- and the characteristic failure of a dying collector is flapping
(it restarts, runs for minutes, dies), which automatic hand-back would convert
into an oscillation, one handover per flap. The rule is therefore symmetric and
preference-free: whoever is eligible takes a lease nobody holds, and nobody is
displaced while their lease is live. An operator who wants the primary back says
so explicitly (``release_lease`` behind the Agents page's "Return to primary"),
and even then the barrier applies -- the lease is revoked, not reassigned, and
the successor waits out the old holder's deadline.
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import case, or_, select, update
from sqlalchemy.orm import Session

from central import models as m

log = logging.getLogger("printer_nanny.collector")

#: Lease length, and the takeover threshold, are operator-tunable via
#: ``central.runtime`` (``collector.*``). These mirror the spec defaults so this
#: module is usable with a plain dict in tests.
DEFAULT_LEASE_SECONDS = 900
DEFAULT_TAKEOVER_AFTER_SECONDS = 900


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Rows written before timezone-awareness was universal can be naive.

    Treat those as UTC rather than raising in the middle of a heartbeat -- the
    same accommodation ``services.adopt_by_name`` and the worker jobs make.
    """
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def lease_settings(runtime: dict) -> Tuple[int, int, bool]:
    """(lease_seconds, takeover_after_seconds, auto_takeover) from settings.

    Both intervals are floored at 60s. A lease shorter than a heartbeat interval
    would expire between renewals and make every subnet flap between "held" and
    "free", which is a worse failure than no redundancy at all.
    """
    def _int(key: str, fallback: int) -> int:
        try:
            return max(60, int(runtime.get(key, fallback) or fallback))
        except (TypeError, ValueError):
            return fallback

    return (
        _int("collector.lease_seconds", DEFAULT_LEASE_SECONDS),
        _int("collector.takeover_after_seconds", DEFAULT_TAKEOVER_AFTER_SECONDS),
        bool(runtime.get("collector.auto_takeover", True)),
    )


def is_redundant(subnet: m.Subnet) -> bool:
    """Whether this subnet is under a lease at all.

    A subnet with no standby is not leased and never consults one: its primary
    collects it exactly as it did before this module existed. Redundancy is
    opt-in per subnet because the subnet row is where an operator already made a
    deliberate decision about who collects.
    """
    return subnet.standby_agent_id is not None


def eligible_agent_ids(subnet: m.Subnet) -> set:
    """The agents allowed to hold this subnet's lease: primary and standby."""
    return {a for a in (subnet.agent_id, subnet.standby_agent_id) if a is not None}


def holder_state(subnet: m.Subnet, now: Optional[datetime] = None) -> str:
    """UI-facing summary: ``none`` | ``held`` | ``lapsed`` | ``released``."""
    if not is_redundant(subnet):
        return "none"
    now = now or _now()
    if subnet.collector_agent_id is None:
        return "released"
    expires = _aware(subnet.collector_lease_expires_at)
    if expires is None or expires <= now:
        return "lapsed"
    return "held"


# --------------------------------------------------------------------------- #
# Grant / renew / revoke -- every one a single-row conditional UPDATE
# --------------------------------------------------------------------------- #
def _displaced(agent_id: int):
    """SET-clause expressions that record the predecessor, only when displaced.

    All SET expressions in one UPDATE read the row's OLD values, so this records
    who held the lease before *this* statement and when their tenure ended,
    without a second round trip and without a read-then-write. A renewal (the
    holder is already me) and an acquisition of an unheld lease both leave the
    predecessor fields alone -- a renewal displaces nobody, and an unheld lease's
    predecessor was already recorded when it was released.
    """
    untouched = or_(
        m.Subnet.collector_agent_id.is_(None),
        m.Subnet.collector_agent_id == agent_id,
    )
    return {
        "collector_prev_agent_id": case(
            (untouched, m.Subnet.collector_prev_agent_id),
            else_=m.Subnet.collector_agent_id,
        ),
        "collector_prev_until": case(
            (untouched, m.Subnet.collector_prev_until),
            else_=m.Subnet.collector_lease_expires_at,
        ),
    }


def renew_lease(
    db: Session, subnet_id: int, agent_id: int, *, now: datetime, ttl_seconds: int
) -> bool:
    """Extend a lease this agent already holds, or pick up one nobody holds.

    Deliberately incapable of taking a lease from another agent: the WHERE
    clause matches only "already mine" or "unheld and past the barrier". A
    heartbeat can therefore never displace a working collector, whatever the
    agent believes, however stale its view is, and however many agents heartbeat
    at once -- so the aggressive path is the one that cannot do harm, and the
    conservative worker path is the only one that can move a lease.

    ``eligible`` is re-checked in SQL rather than in Python because an operator
    may reassign the subnet while the holder is mid-cycle: an agent that is no
    longer the primary or the standby stops being able to renew, its lease
    lapses on its own schedule, and only then can the worker hand the subnet on.
    That is what makes an operator's reassignment take effect without ever
    yanking a subnet out from under an agent that is still sweeping it.
    """
    expires = now + timedelta(seconds=ttl_seconds)
    values = {
        "collector_agent_id": agent_id,
        "collector_lease_expires_at": expires,
        # Only stamp "since" when the holder actually changes; a renewal must
        # not keep resetting the operator-visible "collecting since".
        "collector_since": case(
            (m.Subnet.collector_agent_id == agent_id, m.Subnet.collector_since),
            else_=now,
        ),
    }
    values.update(_displaced(agent_id))
    result = db.execute(
        update(m.Subnet)
        .where(
            m.Subnet.id == subnet_id,
            m.Subnet.standby_agent_id.is_not(None),
            # Eligibility: primary or standby, never anyone else.
            or_(m.Subnet.agent_id == agent_id, m.Subnet.standby_agent_id == agent_id),
            or_(
                # Renew what is already mine...
                m.Subnet.collector_agent_id == agent_id,
                # ...or take one nobody holds, but only past the barrier that a
                # release leaves behind (the previous holder's own deadline).
                (
                    m.Subnet.collector_agent_id.is_(None)
                    & or_(
                        m.Subnet.collector_lease_expires_at.is_(None),
                        m.Subnet.collector_lease_expires_at <= now,
                    )
                ),
            ),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0) == 1


def grant_lease(
    db: Session,
    subnet_id: int,
    *,
    to_agent_id: int,
    from_agent_id: Optional[int],
    now: datetime,
    ttl_seconds: int,
) -> bool:
    """Compare-and-swap a lapsed lease from ``from_agent_id`` to ``to_agent_id``.

    ``from_agent_id`` is the holder the caller judged to be gone; it is asserted
    in the WHERE clause, so if that agent renewed in the meantime the swap misses
    and returns False. **This is the guarantee that a merely-slow primary is not
    displaced** -- not the checks the caller ran beforehand, which are all
    inevitably stale by the time the statement executes.

    The expiry is asserted too, for the same reason from the other direction: a
    renewal moves the deadline into the future, so a stale takeover fails even in
    the impossible case that the holder id still matched.
    """
    expires = now + timedelta(seconds=ttl_seconds)
    values = {
        "collector_agent_id": to_agent_id,
        "collector_lease_expires_at": expires,
        "collector_since": now,
    }
    values.update(_displaced(to_agent_id))
    where = [
        m.Subnet.id == subnet_id,
        m.Subnet.standby_agent_id.is_not(None),
        or_(m.Subnet.agent_id == to_agent_id, m.Subnet.standby_agent_id == to_agent_id),
        or_(
            m.Subnet.collector_lease_expires_at.is_(None),
            m.Subnet.collector_lease_expires_at <= now,
        ),
    ]
    if from_agent_id is None:
        where.append(m.Subnet.collector_agent_id.is_(None))
    else:
        where.append(m.Subnet.collector_agent_id == from_agent_id)
    result = db.execute(
        update(m.Subnet).where(*where).values(**values)
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0) == 1


def release_lease(db: Session, subnet_id: int, *, holder_id: int, now: datetime) -> bool:
    """Revoke a lease without handing it anywhere -- the safe way to move one.

    The holder is cleared but ``collector_lease_expires_at`` is deliberately left
    where it was. That value is then read as an acquisition **barrier**: no agent
    may pick the subnet up until the instant the old holder's own deadline
    passes, and the old holder is guaranteed to have stopped by then (see the
    module docstring). So an operator pressing "Return to primary" while the
    standby is mid-sweep produces a short gap, never an overlap.

    Reassigning the lease directly instead would be the one way to manufacture
    the exact failure this module exists to prevent: the displaced agent's local
    deadline is still live, so it would keep sweeping while its replacement
    started.
    """
    result = db.execute(
        update(m.Subnet)
        .where(m.Subnet.id == subnet_id, m.Subnet.collector_agent_id == holder_id)
        .values(
            collector_agent_id=None,
            collector_prev_agent_id=holder_id,
            collector_prev_until=m.Subnet.collector_lease_expires_at,
            collector_since=None,
        )
        .execution_options(synchronize_session=False)
    )
    return int(result.rowcount or 0) == 1


def seed_lease(db: Session, subnet: m.Subnet, *, now: datetime, ttl_seconds: int) -> None:
    """Give a newly-redundant subnet's primary the lease immediately.

    Without this there is a window between "operator added a standby" and "the
    primary's next heartbeat" in which nobody holds the lease -- and during it
    ``admits()`` would refuse the primary's readings for a subnet it has been
    collecting all along. Seeding is not a takeover: it names the agent that is
    already collecting.
    """
    if subnet.agent_id is None:
        return
    subnet.collector_agent_id = subnet.agent_id
    subnet.collector_lease_expires_at = now + timedelta(seconds=ttl_seconds)
    subnet.collector_since = now
    subnet.collector_prev_agent_id = None
    subnet.collector_prev_until = None


def renew_all(db: Session, agent: m.Agent, *, now: datetime, ttl_seconds: int) -> List[str]:
    """Renew/acquire every leased subnet this agent is eligible for.

    Returns the CIDRs it holds afterwards -- which is exactly what the heartbeat
    response carries, and the only thing the agent is entitled to believe.
    """
    candidates = list(
        db.scalars(
            select(m.Subnet).where(
                m.Subnet.standby_agent_id.is_not(None),
                or_(
                    m.Subnet.agent_id == agent.id,
                    m.Subnet.standby_agent_id == agent.id,
                ),
            )
        )
    )
    if not candidates:
        return []
    held: List[str] = []
    for subnet in candidates:
        if renew_lease(db, subnet.id, agent.id, now=now, ttl_seconds=ttl_seconds):
            held.append(subnet.cidr)
    # The UPDATEs bypassed the identity map (synchronize_session=False), so any
    # Subnet already loaded in this session still shows pre-UPDATE values.
    for subnet in candidates:
        db.expire(subnet)
    return held


# --------------------------------------------------------------------------- #
# Which subnet is a printer on, and may this agent collect it
# --------------------------------------------------------------------------- #
def _network(cidr: str):
    try:
        return ipaddress.ip_network(str(cidr).strip(), strict=False)
    except ValueError:
        return None


def subnet_for_ip(subnets: Sequence[m.Subnet], ip: str) -> Optional[m.Subnet]:
    """The most specific configured subnet containing ``ip``, or None.

    Longest prefix wins, so a /24 carved out of a site's /16 decides -- the same
    rule the nav uses for its active link, and the same one every router uses.
    ``uq_subnet_site_cidr`` stops two rows sharing a CIDR but does not stop them
    overlapping, so "most specific" is the only answer that is deterministic.

    Device-supplied text reaches this function (a reading's ``ip``), so an
    unparseable address resolves to None rather than raising -- it lands on the
    unleased path and is handled exactly as it was before.
    """
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return None
    best: Optional[m.Subnet] = None
    best_len = -1
    for subnet in subnets:
        net = _network(subnet.cidr)
        if net is None or addr not in net:
            continue
        if net.prefixlen > best_len:
            best, best_len = subnet, net.prefixlen
    return best


def subnets_for_sites(db: Session, site_ids) -> Dict[int, List[m.Subnet]]:
    """Every subnet of the given sites, grouped by site. One query."""
    if not site_ids:
        return {}
    grouped: Dict[int, List[m.Subnet]] = {}
    for subnet in db.scalars(select(m.Subnet).where(m.Subnet.site_id.in_(list(site_ids)))):
        grouped.setdefault(subnet.site_id, []).append(subnet)
    return grouped


def any_redundancy(db: Session, site_ids) -> bool:
    """Whether any subnet in these sites is leased at all.

    Every guard in this module short-circuits on this, so an install that has
    never configured a standby -- which is every install, until an operator says
    otherwise -- pays one indexed EXISTS per request and nothing else.
    """
    if not site_ids:
        return False
    return db.scalar(
        select(m.Subnet.id)
        .where(m.Subnet.site_id.in_(list(site_ids)), m.Subnet.standby_agent_id.is_not(None))
        .limit(1)
    ) is not None


def may_collect(subnet: Optional[m.Subnet], agent_id: int) -> bool:
    """Whether ``agent_id`` may collect this subnet right now.

    A printer that falls in no configured subnet, and a subnet with no standby,
    both answer True -- unchanged behaviour for everything that has not opted in.

    Note what is NOT checked: the lease's expiry. Holding a lapsed lease still
    means nobody else has been granted the subnet, and the agent has already
    stopped collecting it on its own clock. Refusing here as well would discard
    the readings it took while the lease was live and is now replaying, which is
    the hole in the meters the spool exists to prevent.
    """
    if subnet is None or not is_redundant(subnet):
        return True
    return subnet.collector_agent_id == agent_id


def admits(subnet: Optional[m.Subnet], agent_id: int, ts: Optional[datetime]) -> bool:
    """Whether a reading from ``agent_id`` stamped ``ts`` may enter ``readings``.

    The current holder is admitted. So is the *immediate* predecessor, for
    readings stamped no later than the instant its tenure ended -- a displaced
    collector replaying its spool is handing over history, strictly older than
    anything its successor has recorded, and appending it cannot interleave the
    two series (the billing sum orders by ``ts``). One predecessor deep and
    time-bounded: anything older, or from any other agent, is refused rather than
    reconciled, because there is no evidence left to reconcile it against.
    """
    if may_collect(subnet, agent_id):
        return True
    assert subnet is not None  # may_collect returns True for None
    if subnet.collector_prev_agent_id != agent_id:
        return False
    until = _aware(subnet.collector_prev_until)
    if until is None or ts is None:
        return False
    ts = _aware(ts)
    return ts is not None and ts <= until
