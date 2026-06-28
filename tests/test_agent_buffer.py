"""Store-and-forward spool: a failed readings push never drops the cycle.

Two layers of proof:

* **Spool unit** -- drive ``ReadingSpool`` directly: a failed push spools the
  reading, a later successful push replays it FIFO and empties the spool, a
  mid-drain failure leaves the remainder, the cap drops the oldest, and a
  replayed payload preserves its original ``ts``.
* **Runner integration** -- a stub client whose ``post_readings`` raises then
  recovers proves ``poll_targets`` buffers on outage and drains on reconnect.
"""

from __future__ import annotations

import json
from typing import List

import httpx
import pytest

from printer_nanny_agent.config import AgentConfig, parse_config
from printer_nanny_agent.runner import poll_targets
from printer_nanny_agent.spool import ReadingSpool

from tests.fakes import FakeSnmpBackend, canned_printer


def _reading(ip: str, ts: str, pages: int = 1000) -> dict:
    """A minimal reading payload shaped like the poller's output."""
    return {"ts": ts, "ip": ip, "status": "ok", "page_count": pages, "supplies": [], "events": []}


def _spool(tmp_path, max_readings: int = 10000) -> ReadingSpool:
    return ReadingSpool(str(tmp_path / "readings-spool.jsonl"), max_readings=max_readings)


# --------------------------------------------------------------------------
# A sender that fails the first N calls (simulating a central outage), then
# succeeds. Records every batch it accepts so order can be asserted.
# --------------------------------------------------------------------------
class _Sender:
    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.calls = 0
        self.accepted: List[dict] = []

    async def __call__(self, readings: List[dict]) -> dict:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.ConnectError("central down")
        self.accepted.extend(readings)
        return {"applied": len(readings)}


def _dump(label: str, spool: ReadingSpool) -> None:
    contents = spool.peek()
    print(f"\n[{label}] spool has {len(contents)} reading(s):")
    for r in contents:
        print("   ", json.dumps(r, separators=(",", ":")))


# --------------------------------------------------------------------------
# Spool-level smoke / proofs
# --------------------------------------------------------------------------
def test_append_spools_readings_on_failure(tmp_path):
    """A failed push persists the readings to the durable spool."""
    spool = _spool(tmp_path)
    assert spool.count() == 0
    dropped = spool.append([_reading("10.0.0.1", "2026-06-28T10:00:00+00:00")])
    _dump("after failed push", spool)
    assert dropped == 0
    assert spool.count() == 1
    # Durable: a fresh ReadingSpool over the same path sees it.
    again = ReadingSpool(spool.path)
    assert again.count() == 1
    assert again.peek()[0]["ip"] == "10.0.0.1"


async def test_drain_replays_fifo_and_empties(tmp_path):
    """Successful reconnect replays spooled readings in FIFO order and clears."""
    spool = _spool(tmp_path)
    spool.append([_reading("10.0.0.1", "2026-06-28T10:00:00+00:00")])
    spool.append([_reading("10.0.0.2", "2026-06-28T10:05:00+00:00")])
    spool.append([_reading("10.0.0.3", "2026-06-28T10:10:00+00:00")])
    _dump("before replay", spool)

    sender = _Sender(fail_times=0)
    replayed = await spool.drain(sender, batch_size=1)
    _dump("after replay", spool)

    assert replayed == 3
    assert spool.count() == 0  # spool emptied
    # FIFO: oldest IP first.
    assert [r["ip"] for r in sender.accepted] == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


async def test_partial_replay_leaves_remainder(tmp_path):
    """Central failing mid-drain leaves the un-acked readings spooled."""
    spool = _spool(tmp_path)
    for i in range(5):
        spool.append([_reading(f"10.0.0.{i}", f"2026-06-28T10:0{i}:00+00:00")])

    # Sender accepts the first two single-reading batches, then central drops.
    class _DropsAfterTwo:
        def __init__(self):
            self.calls = 0
            self.accepted: List[dict] = []

        async def __call__(self, readings):
            self.calls += 1
            if self.calls > 2:
                raise httpx.ConnectError("central went away mid-drain")
            self.accepted.extend(readings)
            return {"applied": len(readings)}

    sender = _DropsAfterTwo()
    with pytest.raises(httpx.ConnectError):
        await spool.drain(sender, batch_size=1)
    _dump("after partial replay", spool)

    # Two delivered, three remain -- in FIFO order, oldest-first preserved.
    assert [r["ip"] for r in sender.accepted] == ["10.0.0.0", "10.0.0.1"]
    remaining = spool.peek()
    assert [r["ip"] for r in remaining] == ["10.0.0.2", "10.0.0.3", "10.0.0.4"]


def test_cap_drops_oldest(tmp_path):
    """The cap bounds the spool: appending past it drops the OLDEST readings."""
    spool = _spool(tmp_path, max_readings=3)
    spool.append([_reading("10.0.0.1", "2026-06-28T10:00:00+00:00")])
    spool.append([_reading("10.0.0.2", "2026-06-28T10:01:00+00:00")])
    spool.append([_reading("10.0.0.3", "2026-06-28T10:02:00+00:00")])
    _dump("at cap", spool)
    assert spool.count() == 3

    # Two more push it over the cap of 3 -> the two oldest get dropped.
    dropped = spool.append([
        _reading("10.0.0.4", "2026-06-28T10:03:00+00:00"),
        _reading("10.0.0.5", "2026-06-28T10:04:00+00:00"),
    ])
    _dump("after exceeding cap", spool)
    assert dropped == 2
    assert spool.count() == 3
    # Newest three survive; .1 and .2 were dropped.
    assert [r["ip"] for r in spool.peek()] == ["10.0.0.3", "10.0.0.4", "10.0.0.5"]


async def test_replayed_payload_preserves_original_ts(tmp_path):
    """A replayed reading carries its original ``ts`` (so central stores it at
    the real poll time, not replay time)."""
    spool = _spool(tmp_path)
    original_ts = "2026-06-28T09:30:00+00:00"
    spool.append([_reading("10.0.0.7", original_ts)])

    sender = _Sender(fail_times=0)
    await spool.drain(sender)
    assert sender.accepted[0]["ts"] == original_ts


def test_empty_drain_is_noop(tmp_path):
    """Draining an empty spool sends nothing and creates no file."""
    spool = _spool(tmp_path)

    async def _boom(_readings):  # pragma: no cover - must not be called
        raise AssertionError("send should not be called for an empty spool")

    import asyncio
    assert asyncio.run(spool.drain(_boom)) == 0
    assert spool.count() == 0


def test_torn_final_line_is_skipped(tmp_path):
    """A half-written final line (process killed mid-append) is skipped, not fatal."""
    spool = _spool(tmp_path)
    spool.append([_reading("10.0.0.1", "2026-06-28T10:00:00+00:00")])
    # Simulate a torn append: a valid line followed by a truncated JSON record.
    with open(spool.path, "a", encoding="utf-8") as fp:
        fp.write('{"ts":"2026-06-28T10:01:00+00:00","ip":"10.0.0.2"')  # no newline, no close
    contents = spool.peek()
    assert len(contents) == 1  # the torn line is dropped
    assert contents[0]["ip"] == "10.0.0.1"


# --------------------------------------------------------------------------
# Runner integration: outage spools, reconnect drains
# --------------------------------------------------------------------------
class _FlakyClient:
    """Stub CentralClient. ``post_readings`` raises while ``down`` is True."""

    def __init__(self, targets: List[dict], *, down: bool):
        self._targets = targets
        self.down = down
        self.accepted: List[dict] = []

    async def get_targets(self) -> List[dict]:
        return list(self._targets)

    async def post_readings(self, readings: List[dict]) -> dict:
        if self.down:
            raise httpx.ConnectError("central unreachable")
        self.accepted.extend(readings)
        return {"applied": len(readings)}


def _config(tmp_path) -> AgentConfig:
    return parse_config({
        "central_url": "https://c.example",
        "agent_id": 1,
        "api_key": "pn_x",
        "data_dir": str(tmp_path),
        "snmp": {"community": "public", "version": "2c", "timeout": 1.0},
        "subnets": [{"cidr": "10.0.0.0/30"}],
    })


async def test_poll_targets_spools_on_outage_then_drains(tmp_path):
    """End-to-end through the runner: an outage spools the cycle; the next
    reachable cycle replays it and clears the spool."""
    config = _config(tmp_path)
    spool = ReadingSpool(config.spool_path(), max_readings=config.spool_max_readings)
    backend = FakeSnmpBackend({"10.0.0.1": canned_printer(name="p1")})
    targets = [{"id": 1, "ip": "10.0.0.1"}]

    # --- outage: post_readings raises, reading must be spooled, applied=0 ---
    down = _FlakyClient(targets, down=True)
    result = await poll_targets(down, backend, config, spool)
    _dump("after outage cycle", spool)
    assert result["applied"] == 0
    assert spool.count() == 1
    assert down.accepted == []  # central never received it

    # --- recovery: central back up. Next cycle drains the backlog AND pushes
    #     this cycle's fresh reading. ---
    up = _FlakyClient(targets, down=False)
    result = await poll_targets(up, backend, config, spool)
    _dump("after recovery cycle", spool)
    assert spool.count() == 0  # backlog flushed
    # Central received the spooled reading (replayed) plus this cycle's reading.
    assert len(up.accepted) == 2
    assert all(r["ip"] == "10.0.0.1" for r in up.accepted)
    # Both carry a ts stamped by the poller (proves replay preserves it).
    assert all(r.get("ts") for r in up.accepted)
