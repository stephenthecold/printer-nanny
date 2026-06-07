"""Agent command handlers: rescan, poll_now, and the new per-printer poll_printer."""

from __future__ import annotations

from typing import List, Optional

from printer_nanny_agent.config import parse_config
from printer_nanny_agent.runner import handle_commands, poll_one_target

from tests.fakes import FakeSnmpBackend, canned_printer


class _StubClient:
    """In-memory stand-in for CentralClient — records what got posted."""

    def __init__(self, targets: List[dict]):
        self._targets = targets
        self.posted: List[dict] = []

    async def get_targets(self) -> List[dict]:
        return list(self._targets)

    async def post_readings(self, readings: List[dict]) -> dict:
        self.posted.extend(readings)
        return {"applied": len(readings)}


def _config() -> "config_type":  # type: ignore[name-defined]
    return parse_config({
        "central_url": "https://c.example",
        "agent_id": 1,
        "api_key": "pn_x",
        "snmp": {"community": "public", "version": "2c", "timeout": 1.0},
        "subnets": [{"cidr": "10.0.0.0/30"}],
    })


async def test_poll_one_target_pushes_single_reading():
    backend = FakeSnmpBackend({"10.0.0.5": canned_printer()})
    client = _StubClient(targets=[{"id": 1, "ip": "10.0.0.5", "snmp_community": "ro", "snmp_version": "2c"}])
    result = await poll_one_target(client, backend, _config(), "10.0.0.5")
    assert result == {"polled": 1, "applied": 1, "unreachable": 0}
    assert len(client.posted) == 1
    assert client.posted[0]["ip"] == "10.0.0.5"


async def test_poll_one_target_unreachable_returns_unreachable():
    # Backend has no device → SnmpError → unreachable, no post.
    backend = FakeSnmpBackend({})
    client = _StubClient(targets=[])
    result = await poll_one_target(client, backend, _config(), "10.0.0.99")
    assert result == {"polled": 1, "applied": 0, "unreachable": 1}
    assert client.posted == []


async def test_handle_commands_dispatches_poll_printer():
    backend = FakeSnmpBackend({"10.0.0.5": canned_printer()})
    client = _StubClient(targets=[{"id": 1, "ip": "10.0.0.5"}])
    await handle_commands(client, backend, _config(), [
        {"id": 42, "type": "poll_printer", "payload": {"ip": "10.0.0.5"}},
    ])
    assert client.posted and client.posted[0]["ip"] == "10.0.0.5"


async def test_handle_commands_poll_printer_without_ip_is_noop():
    backend = FakeSnmpBackend({"10.0.0.5": canned_printer()})
    client = _StubClient(targets=[])
    # Missing payload.ip → just logs a warning and moves on (no crash, no post).
    await handle_commands(client, backend, _config(), [
        {"id": 1, "type": "poll_printer", "payload": {}},
    ])
    assert client.posted == []
