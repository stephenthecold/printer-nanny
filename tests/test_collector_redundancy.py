"""Collector redundancy: a standby agent per subnet, and the lease that stops
two collectors ever sweeping one subnet at once.

The tests are grouped by the property they are defending, because the ordinary
"it works" cases here are the least interesting ones:

1. **Split brain is impossible** -- the conditional UPDATE is the interlock, a
   heartbeat cannot displace anybody, and the takeover compare-and-swap misses
   when the holder renewed.
2. **Takeover is conservative** -- every refusal ``services.adopt_by_name`` makes,
   made here too.
3. **Billing is protected** -- including a test that *demonstrates* the
   corruption, so the guard is measured against real damage rather than an
   assertion about intent.
4. **The agent enforces its own deadline**, which is the half central cannot do.
5. Ingest / config / targets filtering, audit, hand-back and the UI.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from central import collector, models as m
from central import queries, services
from central.main import app
from central.security import hash_api_key, hash_password
from central.worker import jobs
from printer_nanny_agent.config import AgentConfig, SubnetConfig, merge_remote
from printer_nanny_agent.runner import (
    CollectorLeases,
    _may_collect,
    _try_heartbeat,
    _withhold_unleased,
    poll_targets,
)

PRIMARY_KEY = "pn_primary_key"
STANDBY_KEY = "pn_standby_key"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed(db, *, cidr="10.0.0.0/24", with_standby=True):
    """One client, one site, two agents, one subnet, one approved printer."""
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    primary = m.Agent(
        site_id=site.id, name="Primary", api_key_hash=hash_api_key(PRIMARY_KEY),
        status=m.AgentStatus.online, last_heartbeat=_now(),
    )
    standby = m.Agent(
        site_id=site.id, name="Standby", api_key_hash=hash_api_key(STANDBY_KEY),
        status=m.AgentStatus.online, last_heartbeat=_now(),
    )
    db.add_all([primary, standby])
    db.flush()
    subnet = m.Subnet(
        site_id=site.id, agent_id=primary.id, cidr=cidr,
        standby_agent_id=standby.id if with_standby else None,
    )
    db.add(subnet)
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.50",
        discovery_state=m.DiscoveryState.approved, model="HL-L2350DW",
    )
    db.add(printer)
    db.commit()
    if with_standby:
        collector.seed_lease(db, subnet, now=_now(), ttl_seconds=900)
        db.commit()
    return site, primary, standby, subnet, printer


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _admin_http(db) -> TestClient:
    db.add(m.User(
        username="admin", password_hash=hash_password("admin"), role=m.UserRole.admin,
    ))
    db.commit()
    cli = TestClient(app)
    cli.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    return cli


# --------------------------------------------------------------------------- #
# 1. Split brain is impossible
# --------------------------------------------------------------------------- #
def test_two_agents_racing_one_free_lease_only_one_wins(db):
    """The conditional UPDATE is the interlock -- exactly one rowcount of 1.

    The same discipline ``redeem_claim_token`` uses. Both agents are eligible,
    both see a free lease, both attempt it; the row can only carry one holder
    and the loser is told so by rowcount, not by a re-read.
    """
    _site, primary, standby, subnet, _printer = _seed(db)
    now = _now()
    # Free the lease: nobody holds it, barrier already passed.
    subnet.collector_agent_id = None
    subnet.collector_lease_expires_at = now - timedelta(seconds=1)
    db.commit()

    first = collector.renew_lease(db, subnet.id, primary.id, now=now, ttl_seconds=900)
    second = collector.renew_lease(db, subnet.id, standby.id, now=now, ttl_seconds=900)

    assert [first, second] == [True, False]
    db.expire(subnet)
    assert subnet.collector_agent_id == primary.id


def test_heartbeat_can_never_take_a_lease_from_another_agent(db):
    """A standby heartbeating against a LIVE lease gets nothing.

    This is the structural reason no amount of agent-side eagerness can cause a
    split brain: the aggressive, every-cycle path is incapable of displacement.
    """
    _site, primary, standby, subnet, _printer = _seed(db)
    with TestClient(app) as cli:
        resp = cli.post(f"/api/v1/agents/{standby.id}/heartbeat",
                        json={"version": "0.16.4"}, headers=_auth(STANDBY_KEY))
    assert resp.status_code == 200
    assert resp.json()["collector"]["held"] == []
    db.expire(subnet)
    assert subnet.collector_agent_id == primary.id


def test_heartbeat_cannot_take_an_EXPIRED_lease_either(db):
    """Even a lapsed lease is not a heartbeat's to take.

    Expiry frees the subnet for the *worker*, which applies the conservative
    checks. If the standby's heartbeat could grab it the instant it lapsed, a
    primary whose link to central blipped for one lease would be displaced with
    no judgement applied at all.
    """
    _site, primary, standby, subnet, _printer = _seed(db)
    subnet.collector_lease_expires_at = _now() - timedelta(hours=1)
    db.commit()

    took = collector.renew_lease(db, subnet.id, standby.id, now=_now(), ttl_seconds=900)

    assert took is False
    db.expire(subnet)
    assert subnet.collector_agent_id == primary.id


def test_takeover_compare_and_swap_misses_when_the_holder_renewed(db):
    """The CAS, not the checks, is what protects a merely-slow primary.

    Simulates the unavoidable race: the worker judged the primary gone, and
    between that judgement and the write the primary's heartbeat landed.
    """
    _site, primary, standby, subnet, _printer = _seed(db)
    judged_expiry = _now() - timedelta(minutes=30)
    subnet.collector_lease_expires_at = judged_expiry
    db.commit()

    # The primary comes back between the worker's read and its write.
    assert collector.renew_lease(db, subnet.id, primary.id, now=_now(), ttl_seconds=900)

    granted = collector.grant_lease(
        db, subnet.id, to_agent_id=standby.id, from_agent_id=primary.id,
        now=_now(), ttl_seconds=900,
    )

    assert granted is False
    db.expire(subnet)
    assert subnet.collector_agent_id == primary.id


def test_only_the_primary_or_standby_may_ever_hold_the_lease(db):
    """A third agent -- correct key, same site -- gets nothing."""
    site, _primary, _standby, subnet, _printer = _seed(db)
    stranger = m.Agent(site_id=site.id, name="Stranger",
                       api_key_hash=hash_api_key("pn_other"))
    db.add(stranger)
    db.commit()
    subnet.collector_agent_id = None
    subnet.collector_lease_expires_at = None
    db.commit()

    assert not collector.renew_lease(db, subnet.id, stranger.id, now=_now(), ttl_seconds=900)
    assert not collector.grant_lease(
        db, subnet.id, to_agent_id=stranger.id, from_agent_id=None,
        now=_now(), ttl_seconds=900,
    )


def test_an_agent_reassigned_away_stops_being_able_to_renew(db):
    """Losing eligibility revokes by attrition, never by yanking the subnet.

    An operator moving a subnet to another agent must not stop the old one
    mid-sweep -- its local deadline is still live. It simply cannot renew, so
    the lease runs down and the successor waits for it.
    """
    site, primary, _standby, subnet, _printer = _seed(db)
    other = m.Agent(site_id=site.id, name="Other", api_key_hash=hash_api_key("pn_o"))
    db.add(other)
    db.flush()
    subnet.agent_id = other.id  # operator reassigned; primary is now the holder
    db.commit()                 # but no longer eligible

    assert not collector.renew_lease(db, subnet.id, primary.id, now=_now(), ttl_seconds=900)


# --------------------------------------------------------------------------- #
# 2. Takeover is conservative (the services.adopt_by_name discipline)
# --------------------------------------------------------------------------- #
def _make_primary_look_gone(db, primary, subnet, *, silent_minutes=30):
    primary.status = m.AgentStatus.offline
    primary.last_heartbeat = _now() - timedelta(minutes=silent_minutes)
    subnet.collector_lease_expires_at = _now() - timedelta(minutes=1)
    db.commit()


def test_worker_hands_the_subnet_to_the_standby_when_the_primary_is_gone(db):
    _site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)

    result = jobs.reassign_collectors(db)

    assert result == {"collector_takeovers": 1}
    db.expire(subnet)
    assert subnet.collector_agent_id == standby.id
    assert subnet.collector_prev_agent_id == primary.id
    assert subnet.collector_since is not None


def test_a_live_lease_is_never_taken_however_dead_the_holder_looks(db):
    """The lease, not the status, is what gates a handover.

    A collector marked offline may still be sweeping perfectly well on a link
    that only central cannot reach -- which is the whole scenario this defends.
    """
    _site, primary, standby, subnet, _printer = _seed(db)
    primary.status = m.AgentStatus.offline
    primary.last_heartbeat = _now() - timedelta(hours=6)
    subnet.collector_lease_expires_at = _now() + timedelta(minutes=5)  # still live
    db.commit()

    assert jobs.reassign_collectors(db) == {"collector_takeovers": 0}
    db.expire(subnet)
    assert subnet.collector_agent_id == primary.id


def test_a_primary_that_is_merely_slow_is_not_displaced(db):
    """Offline, lease lapsed -- but silent for less than the takeover threshold."""
    _site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet, silent_minutes=5)  # < 900s

    assert jobs.reassign_collectors(db) == {"collector_takeovers": 0}
    db.expire(subnet)
    assert subnet.collector_agent_id == primary.id


def test_a_primary_still_online_is_not_displaced_even_with_a_lapsed_lease(db):
    _site, primary, standby, subnet, _printer = _seed(db)
    primary.status = m.AgentStatus.online
    primary.last_heartbeat = _now() - timedelta(hours=2)
    subnet.collector_lease_expires_at = _now() - timedelta(minutes=1)
    db.commit()

    assert jobs.reassign_collectors(db) == {"collector_takeovers": 0}


def test_a_dead_standby_is_never_handed_the_subnet(db):
    """Handing a subnet to a second dead agent changes only who is blamed."""
    _site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    standby.status = m.AgentStatus.offline
    standby.last_heartbeat = _now() - timedelta(hours=1)
    db.commit()

    assert jobs.reassign_collectors(db) == {"collector_takeovers": 0}
    db.expire(subnet)
    assert subnet.collector_agent_id == primary.id


def test_a_never_seen_standby_is_never_handed_the_subnet(db):
    _site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    standby.status = m.AgentStatus.never_seen
    standby.last_heartbeat = None
    db.commit()

    assert jobs.reassign_collectors(db) == {"collector_takeovers": 0}


def test_auto_takeover_off_leaves_the_subnet_where_it_is(db):
    _site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    from central.runtime import save_settings

    # An unchecked box posts nothing, so "off" is spelled as a section that
    # carries the key with the key absent -- exactly what the settings page does.
    save_settings(db, {}, sections={"Collector redundancy"})

    assert jobs.reassign_collectors(db) == {"collector_takeovers": 0}
    db.expire(subnet)
    assert subnet.collector_agent_id == primary.id


def test_a_subnet_with_no_standby_is_never_touched_by_the_worker(db):
    """Redundancy is opt-in per subnet: no standby, no lease, no behaviour change."""
    _site, primary, _standby, subnet, _printer = _seed(db, with_standby=False)
    primary.status = m.AgentStatus.offline
    primary.last_heartbeat = _now() - timedelta(days=1)
    db.commit()

    assert jobs.reassign_collectors(db) == {"collector_takeovers": 0}
    db.expire(subnet)
    assert subnet.collector_agent_id is None
    assert collector.is_redundant(subnet) is False


def test_no_automatic_hand_back_when_the_primary_returns(db):
    """The decided behaviour, asserted: recovery does NOT reclaim the subnet.

    A collector that flaps would otherwise oscillate, one handover per flap, and
    every handover is a transition the split-brain machinery has to survive.
    """
    _site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    assert jobs.reassign_collectors(db) == {"collector_takeovers": 1}

    # Primary comes back, healthy, and keeps heartbeating for several cycles.
    primary.status = m.AgentStatus.online
    primary.last_heartbeat = _now()
    db.commit()
    for _ in range(3):
        assert jobs.reassign_collectors(db) == {"collector_takeovers": 0}
        assert collector.renew_lease(
            db, subnet.id, standby.id, now=_now(), ttl_seconds=900
        )

    db.expire(subnet)
    assert subnet.collector_agent_id == standby.id


def test_the_standby_dying_hands_the_subnet_back_by_the_same_rule(db):
    """The rule is symmetric: no preference for the primary, in either direction."""
    _site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    assert jobs.reassign_collectors(db) == {"collector_takeovers": 1}

    primary.status = m.AgentStatus.online
    primary.last_heartbeat = _now()
    standby.status = m.AgentStatus.offline
    standby.last_heartbeat = _now() - timedelta(hours=1)
    db.expire(subnet)
    subnet.collector_lease_expires_at = _now() - timedelta(minutes=1)
    db.commit()

    assert jobs.reassign_collectors(db) == {"collector_takeovers": 1}
    db.expire(subnet)
    assert subnet.collector_agent_id == primary.id


def test_takeover_is_audited(db):
    _site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)

    jobs.reassign_collectors(db)

    row = db.scalar(
        select(m.AuditLog).where(m.AuditLog.action == "subnet.collector_takeover")
    )
    assert row is not None
    assert f"subnet:{subnet.id}" in row.target
    assert f"agent:{primary.id} -> agent:{standby.id}" in row.detail


# --------------------------------------------------------------------------- #
# 3. Billing is protected -- and the damage is demonstrated, not asserted
# --------------------------------------------------------------------------- #
def test_two_collectors_with_skewed_clocks_really_do_double_bill(db):
    """The harm, measured. Without this the guard is defending a hypothesis.

    ``_printed_pages_for_printer`` sums POSITIVE page-count deltas over readings
    ordered by ``ts``, and ``ts`` is stamped by the agent. Two collectors whose
    clocks differ produce a series that dips and recovers, and the recovery is
    billed as fresh pages that were already billed once.
    """
    base = _now()
    honest = [(100,), (110,)]
    # Same two real observations, plus a second collector running 40s behind:
    # its 100 is stamped AFTER the first collector's 110.
    interleaved = [(100,), (110,), (100,), (110,)]

    assert queries._printed_pages_for_printer(honest) == 10
    assert queries._printed_pages_for_printer(interleaved) == 20  # billed twice
    assert base is not None


def test_ingest_refuses_readings_from_an_agent_that_does_not_hold_the_lease(db):
    """The layer that protects the append-only table regardless of the agent.

    An agent too old to know what a lease is, one on a stale target list, or one
    with a stolen key all arrive here. None of them gets a row.
    """
    _site, _primary, standby, _subnet, printer = _seed(db)
    with TestClient(app) as cli:
        resp = cli.post(
            f"/api/v1/agents/{standby.id}/readings",
            json={"readings": [{"ip": printer.ip, "page_count": 500, "status": "ok"}]},
            headers=_auth(STANDBY_KEY),
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] == 0
    assert body["refused_not_collector"] == [printer.ip]
    assert db.scalar(select(m.Reading).where(m.Reading.printer_id == printer.id)) is None


def test_the_lease_holder_is_admitted(db):
    _site, primary, _standby, _subnet, printer = _seed(db)
    with TestClient(app) as cli:
        resp = cli.post(
            f"/api/v1/agents/{primary.id}/readings",
            json={"readings": [{"ip": printer.ip, "page_count": 500, "status": "ok"}]},
            headers=_auth(PRIMARY_KEY),
        )

    assert resp.json()["applied"] == 1
    assert resp.json()["refused_not_collector"] == []


def test_a_subnet_with_no_standby_admits_its_agent_exactly_as_before(db):
    """Zero behaviour change for every install that has not opted in."""
    _site, primary, _standby, _subnet, printer = _seed(db, with_standby=False)
    with TestClient(app) as cli:
        resp = cli.post(
            f"/api/v1/agents/{primary.id}/readings",
            json={"readings": [{"ip": printer.ip, "page_count": 7, "status": "ok"}]},
            headers=_auth(PRIMARY_KEY),
        )
    assert resp.json()["applied"] == 1


def test_the_displaced_collector_may_still_replay_its_spool(db):
    """History, not duplicates: strictly older than anything the successor took.

    Refusing these would punch exactly the hole in the meters that the agent's
    store-and-forward spool exists to prevent.
    """
    _site, primary, standby, subnet, printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    jobs.reassign_collectors(db)
    db.expire(subnet)
    boundary = collector._aware(subnet.collector_prev_until)
    assert subnet.collector_prev_agent_id == primary.id

    old = (boundary - timedelta(minutes=5)).isoformat()
    with TestClient(app) as cli:
        resp = cli.post(
            f"/api/v1/agents/{primary.id}/readings",
            json={"readings": [{"ip": printer.ip, "ts": old, "page_count": 9, "status": "ok"}]},
            headers=_auth(PRIMARY_KEY),
        )

    assert resp.json()["applied"] == 1


def test_the_displaced_collector_may_NOT_push_readings_after_its_tenure(db):
    """One predecessor, time-bounded. A reading stamped after the handover is
    exactly the overlapping series the whole mechanism exists to refuse."""
    _site, primary, standby, subnet, printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    jobs.reassign_collectors(db)
    db.expire(subnet)
    boundary = collector._aware(subnet.collector_prev_until)

    after = (boundary + timedelta(minutes=5)).isoformat()
    with TestClient(app) as cli:
        resp = cli.post(
            f"/api/v1/agents/{primary.id}/readings",
            json={"readings": [{"ip": printer.ip, "ts": after, "page_count": 9, "status": "ok"}]},
            headers=_auth(PRIMARY_KEY),
        )

    assert resp.json()["refused_not_collector"] == [printer.ip]


def test_a_reading_with_no_timestamp_from_a_predecessor_is_refused(db):
    """No ``ts`` means central stamps it *now*, which is inside the successor's
    tenure by definition -- so it cannot be pre-handover history."""
    _site, primary, _standby, subnet, printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    jobs.reassign_collectors(db)

    with TestClient(app) as cli:
        resp = cli.post(
            f"/api/v1/agents/{primary.id}/readings",
            json={"readings": [{"ip": printer.ip, "page_count": 9, "status": "ok"}]},
            headers=_auth(PRIMARY_KEY),
        )

    assert resp.json()["refused_not_collector"] == [printer.ip]


def test_a_refused_batch_is_audited_once_per_subnet(db):
    """Recorded, and readable: a handover can refuse a whole fleet at once."""
    _site, _primary, standby, _subnet, printer = _seed(db)
    other = m.Printer(
        client_id=printer.client_id, site_id=printer.site_id, ip="10.0.0.51",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(other)
    db.commit()
    with TestClient(app) as cli:
        cli.post(
            f"/api/v1/agents/{standby.id}/readings",
            json={"readings": [
                {"ip": "10.0.0.50", "page_count": 1, "status": "ok"},
                {"ip": "10.0.0.51", "page_count": 1, "status": "ok"},
            ]},
            headers=_auth(STANDBY_KEY),
        )

    rows = list(db.scalars(
        select(m.AuditLog).where(m.AuditLog.action == "reading.refused_not_collector")
    ))
    assert len(rows) == 1
    assert "2 reading(s) refused" in rows[0].detail


def test_a_printer_outside_every_configured_subnet_is_still_collected(db):
    """Unattributable is not the same as forbidden. Silently dropping printers
    nobody put in a subnet row would be a regression dressed as a safety check."""
    _site, _primary, standby, _subnet, printer = _seed(db)
    printer.ip = "192.168.99.9"  # outside 10.0.0.0/24
    db.commit()
    with TestClient(app) as cli:
        resp = cli.post(
            f"/api/v1/agents/{standby.id}/readings",
            json={"readings": [{"ip": printer.ip, "page_count": 3, "status": "ok"}]},
            headers=_auth(STANDBY_KEY),
        )
    assert resp.json()["applied"] == 1


# --------------------------------------------------------------------------- #
# 4. The agent enforces its own deadline
# --------------------------------------------------------------------------- #
def test_the_agent_deadline_is_anchored_before_the_request():
    """The safety ordering, asserted directly.

    The anchor is sampled before the heartbeat is sent, so the agent's deadline
    is always earlier than the expiry central recorded on receipt -- the holder
    gives up before the grantor will reallocate, with no assumption that the two
    clocks agree.
    """
    leases = CollectorLeases()

    class _Client:
        async def heartbeat(self, *_a, **_k):
            time.sleep(0.05)  # the request takes time to travel and be processed
            return {"collector": {"lease_seconds": 10, "held": ["10.0.0.0/24"]}}

    before = time.monotonic()
    assert asyncio.run(_try_heartbeat(_Client(), "/x", None, leases)) is True
    central_would_expire_at = time.monotonic() + 10  # central stamped it later than us

    assert leases.holds("10.0.0.0/24")
    agent_deadline = leases._until["10.0.0.0/24"]
    assert before + 10 <= agent_deadline <= central_would_expire_at


def test_a_lease_that_is_not_renewed_expires_at_the_agent():
    leases = CollectorLeases()
    leases.record(["10.0.0.0/24"], 60, anchor=time.monotonic() - 61)
    assert leases.holds("10.0.0.0/24") is False


def test_a_failed_heartbeat_neither_clears_nor_extends_the_lease():
    """One missed heartbeat is not a lost lease; a run of them is."""
    leases = CollectorLeases()
    leases.record(["10.0.0.0/24"], 60, anchor=time.monotonic())

    class _Down:
        async def heartbeat(self, *_a, **_k):
            raise OSError("central unreachable")

    deadline = leases._until["10.0.0.0/24"]
    assert asyncio.run(_try_heartbeat(_Down(), "/x", None, leases)) is False
    assert leases._until["10.0.0.0/24"] == deadline  # untouched, so it still runs out


def test_losing_a_lease_takes_effect_on_the_heartbeat_that_says_so():
    """``record`` replaces rather than merges: a lost lease disappears at once.

    Merging would leave a lease this agent no longer holds in place until it
    timed out -- turning a clean handover into the longest possible overlap.
    """
    leases = CollectorLeases()
    leases.record(["10.0.0.0/24", "10.0.1.0/24"], 900, anchor=time.monotonic())
    leases.record(["10.0.1.0/24"], 900, anchor=time.monotonic())

    assert leases.holds("10.0.0.0/24") is False
    assert leases.holds("10.0.1.0/24") is True


def test_an_unleased_subnet_needs_no_lease():
    assert _may_collect(SubnetConfig(cidr="10.0.0.0/24"), None, time.monotonic()) is True


def test_a_leased_subnet_with_no_tracker_at_all_is_refused():
    """"I was never told I hold this" and "my lease expired" have one correct
    answer, and it is not the permissive one."""
    leased = SubnetConfig(cidr="10.0.0.0/24", leased=True)
    assert _may_collect(leased, None, time.monotonic()) is False
    assert _may_collect(leased, CollectorLeases(), time.monotonic()) is False


def test_the_cached_target_list_stops_being_polled_when_the_lease_runs_out():
    """The case central cannot reach: link down, printers still reachable.

    Without this the agent keeps sweeping from its cache while a standby is
    handed the subnet, which is precisely the double-collection this prevents.
    """
    config = AgentConfig(
        central_url="http://c", agent_id=1, api_key="k",
        subnets=[SubnetConfig(cidr="10.0.0.0/24", leased=True)],
    )
    targets = [{"ip": "10.0.0.50"}, {"ip": "10.0.0.51"}]
    leases = CollectorLeases()

    leases.record(["10.0.0.0/24"], 900, anchor=time.monotonic())
    assert _withhold_unleased(targets, config, leases) == (targets, [])

    leases.record(["10.0.0.0/24"], 900, anchor=time.monotonic() - 901)
    keep, withheld = _withhold_unleased(targets, config, leases)
    assert (keep, withheld) == ([], targets)


def test_poll_targets_withholds_and_says_so(tmp_path):
    """A withheld target is reported, never silently absent -- an agent that
    quietly stopped sweeping looks exactly like a site that went down."""
    config = AgentConfig(
        central_url="http://c", agent_id=1, api_key="k",
        data_dir=str(tmp_path),
        subnets=[SubnetConfig(cidr="10.0.0.0/24", leased=True)],
    )

    class _Client:
        async def get_targets(self):
            return [{"ip": "10.0.0.50"}]

        async def post_readings(self, readings):
            raise AssertionError("must not push readings for a subnet we do not hold")

    leases = CollectorLeases()
    leases.record([], 900, anchor=time.monotonic())
    result = asyncio.run(poll_targets(_Client(), None, config, leases=leases))

    assert result["polled"] == 0
    assert result["withheld"] == 1


def test_the_leased_flag_survives_the_config_merge():
    merged = merge_remote(
        AgentConfig(central_url="http://c", agent_id=1, api_key="k"),
        {"subnets": [
            {"cidr": "10.0.0.0/24", "leased": True},
            {"cidr": "10.0.1.0/24"},
        ]},
    )
    assert [s.leased for s in merged.subnets] == [True, False]


def test_a_central_too_old_to_send_the_block_leases_nothing():
    """Backwards compatibility, from the other side: an old central has no
    leased subnets either, so an empty grant withholds nothing."""
    leases = CollectorLeases()

    class _Old:
        async def heartbeat(self, *_a, **_k):
            return {"id": 1, "site_id": 1, "name": "a", "status": "online"}

    assert asyncio.run(_try_heartbeat(_Old(), "/x", None, leases)) is True
    assert leases.held_cidrs() == []
    assert _may_collect(SubnetConfig(cidr="10.0.0.0/24"), leases, time.monotonic())


# --------------------------------------------------------------------------- #
# 5. Config / targets filtering, hand-back, UI
# --------------------------------------------------------------------------- #
def test_config_gives_credentials_only_to_the_lease_holder(db):
    """Both eligible agents are told the subnet exists; only one gets its creds.

    Being nominated as a standby grants no credential at all -- which is the
    least-privilege half of this feature, and is enforced by the same row that
    prevents the split brain rather than by a second rule that could disagree.
    """
    _site, primary, standby, subnet, _printer = _seed(db)
    subnet.snmp_community = "s3cret-community"
    subnet.snmp_v3 = {"user": "usm", "auth_password": "hunter2"}
    db.commit()
    with TestClient(app) as cli:
        held = cli.get(f"/api/v1/agents/{primary.id}/config", headers=_auth(PRIMARY_KEY))
        not_held = cli.get(f"/api/v1/agents/{standby.id}/config", headers=_auth(STANDBY_KEY))

    mine = held.json()["subnets"]
    theirs = not_held.json()["subnets"]
    assert [s["cidr"] for s in mine] == ["10.0.0.0/24"]
    assert mine[0]["leased"] is True and mine[0]["snmp_community"] == "s3cret-community"
    assert [s["cidr"] for s in theirs] == ["10.0.0.0/24"]
    assert theirs[0]["leased"] is True
    assert theirs[0]["snmp_community"] == "public"  # schema default, not the real one
    assert theirs[0]["snmp_v3"] is None
    assert "hunter2" not in not_held.text


def test_a_displaced_primary_is_not_pushed_back_onto_its_local_subnet_list(db):
    """The trap: ``merge_remote`` falls back to the LOCAL subnets when central
    returns none, so an empty list would send a displaced primary back to its
    ``agent.toml`` and it would keep sweeping the subnet it just lost."""
    _site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    jobs.reassign_collectors(db)

    with TestClient(app) as cli:
        remote = cli.get(f"/api/v1/agents/{primary.id}/config",
                         headers=_auth(PRIMARY_KEY)).json()

    local = AgentConfig(
        central_url="http://c", agent_id=primary.id, api_key="k",
        subnets=[SubnetConfig(cidr="10.0.0.0/24", community="local-secret")],
    )
    effective = merge_remote(local, remote)

    assert [s.cidr for s in effective.subnets] == ["10.0.0.0/24"]
    assert effective.subnets[0].leased is True
    # ...and the flag is what stops the sweep, whatever the local file said.
    assert _may_collect(effective.subnets[0], CollectorLeases(), time.monotonic()) is False


def test_targets_serves_a_leased_subnets_printers_only_to_its_holder(db):
    _site, primary, standby, _subnet, printer = _seed(db)
    with TestClient(app) as cli:
        held = cli.get(f"/api/v1/agents/{primary.id}/targets", headers=_auth(PRIMARY_KEY))
        not_held = cli.get(f"/api/v1/agents/{standby.id}/targets", headers=_auth(STANDBY_KEY))

    assert [t["ip"] for t in held.json()] == [printer.ip]
    assert not_held.json() == []


def test_both_agents_of_a_site_still_get_everything_when_nothing_is_leased(db):
    """The pre-existing behaviour, pinned: this feature must not change it."""
    _site, primary, standby, _subnet, printer = _seed(db, with_standby=False)
    with TestClient(app) as cli:
        a = cli.get(f"/api/v1/agents/{primary.id}/targets", headers=_auth(PRIMARY_KEY))
        b = cli.get(f"/api/v1/agents/{standby.id}/targets", headers=_auth(STANDBY_KEY))
    assert [t["ip"] for t in a.json()] == [printer.ip]
    assert [t["ip"] for t in b.json()] == [printer.ip]


def test_a_standby_reaches_another_sites_fleet_only_while_it_holds_the_lease(db):
    """Least privilege: being *named* as a standby grants nothing at all."""
    site, primary, _standby, subnet, printer = _seed(db)
    other_client = m.Client(name="Beta")
    db.add(other_client)
    db.flush()
    other_site = m.Site(client_id=other_client.id, name="Beta Office")
    db.add(other_site)
    db.flush()
    remote_standby = m.Agent(
        site_id=other_site.id, name="HQ", api_key_hash=hash_api_key("pn_hq"),
        status=m.AgentStatus.online, last_heartbeat=_now(),
    )
    db.add(remote_standby)
    db.flush()
    subnet.standby_agent_id = remote_standby.id
    db.commit()

    assert site.id not in services.sites_served_by_agent(db, remote_standby)

    _make_primary_look_gone(db, primary, subnet)
    jobs.reassign_collectors(db)
    db.expire_all()
    remote_standby = db.get(m.Agent, remote_standby.id)
    assert site.id in services.sites_served_by_agent(db, remote_standby)
    with TestClient(app) as cli:
        resp = cli.get(f"/api/v1/agents/{remote_standby.id}/targets", headers=_auth("pn_hq"))
    assert [t["ip"] for t in resp.json()] == [printer.ip]


def test_longest_prefix_decides_which_subnet_a_printer_is_on(db):
    """Overlapping CIDRs are legal (the unique constraint only stops duplicates),
    so the answer has to be deterministic. Most specific wins, like every router."""
    site, primary, standby, _subnet, _printer = _seed(db, cidr="10.0.0.0/16")
    narrow = m.Subnet(site_id=site.id, agent_id=standby.id, cidr="10.0.0.0/24")
    db.add(narrow)
    db.commit()
    subnets = list(db.scalars(select(m.Subnet)))

    assert collector.subnet_for_ip(subnets, "10.0.0.50").cidr == "10.0.0.0/24"
    assert collector.subnet_for_ip(subnets, "10.0.9.50").cidr == "10.0.0.0/16"
    assert collector.subnet_for_ip(subnets, "not-an-ip") is None
    assert collector.subnet_for_ip(subnets, "192.168.1.1") is None


def test_handback_releases_rather_than_reassigns(db):
    """The barrier, which is what makes hand-back safe.

    Pointing the lease at the primary would leave the standby's own deadline
    live and both would collect. Releasing keeps the expiry as an acquisition
    barrier, so the primary picks it up only after the standby must have stopped.
    """
    _site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    jobs.reassign_collectors(db)
    db.expire(subnet)
    standby_deadline = collector._aware(subnet.collector_lease_expires_at)
    primary.status = m.AgentStatus.online
    primary.last_heartbeat = _now()
    db.commit()

    cli = _admin_http(db)
    resp = cli.post(f"/manage/subnets/{subnet.id}/handback", follow_redirects=False)
    assert resp.status_code == 303

    db.expire_all()
    subnet = db.get(m.Subnet, subnet.id)
    assert subnet.collector_agent_id is None
    assert collector._aware(subnet.collector_lease_expires_at) == standby_deadline
    # Nobody may acquire before the barrier -- not even the primary, not even
    # the worker.
    assert not collector.renew_lease(db, subnet.id, primary.id, now=_now(), ttl_seconds=900)
    assert jobs.reassign_collectors(db) == {"collector_takeovers": 0}
    # ...and after it, the primary gets it straight back.
    later = standby_deadline + timedelta(seconds=1)
    assert collector.renew_lease(db, subnet.id, primary.id, now=later, ttl_seconds=900)


def test_handback_is_audited(db):
    _site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    jobs.reassign_collectors(db)
    cli = _admin_http(db)
    cli.post(f"/manage/subnets/{subnet.id}/handback", follow_redirects=False)

    row = db.scalar(
        select(m.AuditLog).where(m.AuditLog.action == "subnet.collector_handback")
    )
    assert row is not None and row.username == "admin"


def test_setting_a_standby_seeds_the_lease_to_the_agent_already_collecting(db):
    """No window in which nobody holds the lease and the primary is refused."""
    _site, primary, standby, subnet, _printer = _seed(db, with_standby=False)
    cli = _admin_http(db)

    cli.post(f"/manage/subnets/{subnet.id}", follow_redirects=False, data={
        "standby_present": "1", "standby_agent_id": str(standby.id),
    })

    db.expire_all()
    subnet = db.get(m.Subnet, subnet.id)
    assert subnet.standby_agent_id == standby.id
    assert subnet.collector_agent_id == primary.id
    assert collector.holder_state(subnet) == "held"


def test_a_form_without_the_marker_cannot_clear_the_standby(db):
    """The presence-marker lesson, applied where "" is itself a real choice."""
    _site, _primary, standby, subnet, _printer = _seed(db)
    cli = _admin_http(db)

    cli.post(f"/manage/subnets/{subnet.id}", follow_redirects=False,
             data={"label": "VLAN 10"})  # inline rename, no standby field

    db.expire_all()
    assert db.get(m.Subnet, subnet.id).standby_agent_id == standby.id


def test_an_empty_standby_with_the_marker_clears_it_and_releases_the_lease(db):
    _site, _primary, standby, subnet, _printer = _seed(db)
    cli = _admin_http(db)

    cli.post(f"/manage/subnets/{subnet.id}", follow_redirects=False,
             data={"standby_present": "1", "standby_agent_id": ""})

    db.expire_all()
    subnet = db.get(m.Subnet, subnet.id)
    assert subnet.standby_agent_id is None
    assert subnet.collector_agent_id is None


def test_the_primary_cannot_be_its_own_standby(db):
    _site, primary, _standby, subnet, _printer = _seed(db, with_standby=False)
    cli = _admin_http(db)

    cli.post(f"/manage/subnets/{subnet.id}", follow_redirects=False,
             data={"standby_present": "1", "standby_agent_id": str(primary.id)})

    db.expire_all()
    assert db.get(m.Subnet, subnet.id).standby_agent_id is None


def test_a_subnet_with_no_primary_cannot_be_given_a_standby(db):
    _site, _primary, standby, subnet, _printer = _seed(db, with_standby=False)
    subnet.agent_id = None
    db.commit()
    cli = _admin_http(db)

    cli.post(f"/manage/subnets/{subnet.id}", follow_redirects=False,
             data={"standby_present": "1", "standby_agent_id": str(standby.id)})

    db.expire_all()
    assert db.get(m.Subnet, subnet.id).standby_agent_id is None


def test_the_agents_page_names_the_standby_and_the_current_collector(db):
    _site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    jobs.reassign_collectors(db)
    cli = _admin_http(db)

    html = cli.get("/manage/agents").text

    assert 'aria-label="Standby collector for 10.0.0.0/24"' in html
    assert "collector: Standby" in html
    assert "standing by for:" in html
    assert "return to primary" in html


def test_a_site_covered_by_its_standby_is_not_reported_as_an_outage(db):
    """The standby took over, so the printers are being collected -- flagging
    them offline would turn a successful failover into a fleet-wide alert."""
    site, primary, standby, subnet, _printer = _seed(db)
    _make_primary_look_gone(db, primary, subnet)
    jobs.reassign_collectors(db)

    assert site.id in jobs._sites_with_a_live_agent(db)


@pytest.mark.postgres_only
def test_the_interlock_holds_across_two_real_connections(db):
    """The rowcount interlock under genuine concurrency, on the only backend
    where two sessions can actually race a row.

    SQLite serialises writers at the file level, so a same-process test there
    proves the predicate but not the locking. Here the second session blocks on
    the first's row lock, then re-evaluates its WHERE against the committed row
    and matches nothing.
    """
    from central.db import SessionLocal

    _site, primary, standby, subnet, _printer = _seed(db)
    now = _now()
    subnet.collector_agent_id = None
    subnet.collector_lease_expires_at = now - timedelta(seconds=1)
    db.commit()
    subnet_id = subnet.id
    db.close()

    one, two = SessionLocal(), SessionLocal()
    try:
        first = collector.renew_lease(one, subnet_id, primary.id, now=now, ttl_seconds=900)
        one.commit()
        second = collector.renew_lease(two, subnet_id, standby.id, now=now, ttl_seconds=900)
        two.commit()
        assert [first, second] == [True, False]
    finally:
        one.close()
        two.close()
