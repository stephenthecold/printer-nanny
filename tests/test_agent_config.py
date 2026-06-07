"""Agent config parsing and per-subnet SNMP overrides."""

from __future__ import annotations

import pytest

from printer_nanny_agent.config import load_config, merge_remote, parse_config
from printer_nanny_agent.runner import _due


def test_due_runs_first_cycle_regardless_of_clock():
    # last=None → due now, even when the monotonic clock value is small.
    assert _due(None, 300, 5.0) is True
    # Not due until the interval elapses.
    assert _due(100.0, 300, 200.0) is False
    assert _due(100.0, 300, 400.0) is True


def _valid() -> dict:
    return {
        "central_url": "https://central.example.com/",
        "agent_id": 7,
        "api_key": "pn_abc",
        "poll_interval_seconds": 120,
        "snmp": {"community": "public", "version": "2c", "timeout": 3.0},
        "subnets": [
            {"cidr": "10.10.0.0/24"},
            {"cidr": "10.10.1.0/24", "community": "printers-ro", "version": "1"},
        ],
    }


def test_parse_valid_config():
    cfg = parse_config(_valid())
    assert cfg.central_url == "https://central.example.com"  # trailing slash stripped
    assert cfg.agent_id == 7
    assert cfg.poll_interval_seconds == 120
    assert cfg.snmp.timeout == 3.0
    assert len(cfg.subnets) == 2


def test_missing_keys_raise():
    with pytest.raises(ValueError) as exc:
        parse_config({"central_url": "x"})
    assert "agent_id" in str(exc.value)
    assert "api_key" in str(exc.value)


def test_merge_remote_overlays_central_config():
    local = parse_config(_valid())
    remote = {
        "poll_interval_seconds": 90,
        "discovery_interval_seconds": 1800,
        "heartbeat_interval_seconds": 45,
        "snmp": {"community": "central-ro", "version": "2c", "timeout": 4.0, "retries": 2},
        "subnets": [{"cidr": "192.168.5.0/24", "snmp_community": "vlan5", "snmp_version": "2c"}],
    }
    merged = merge_remote(local, remote)
    # Local identity is preserved; operational config comes from central.
    assert merged.central_url == local.central_url
    assert merged.api_key == local.api_key
    assert merged.poll_interval_seconds == 90
    assert merged.snmp.community == "central-ro"
    assert merged.snmp.retries == 2
    assert len(merged.subnets) == 1
    assert merged.subnets[0].cidr == "192.168.5.0/24"
    assert merged.snmp_for(merged.subnets[0]).community == "vlan5"


def test_config_from_env_only_no_file(monkeypatch):
    # No file — the installer relies on env-only config.
    monkeypatch.setenv("PRINTER_NANNY_CONFIG", "/definitely/not/here.toml")
    monkeypatch.setenv("PN_CENTRAL_URL", "https://central.test/")
    monkeypatch.setenv("PN_AGENT_ID", "42")
    monkeypatch.setenv("PN_API_KEY", "pn_envkey")
    monkeypatch.setenv("PN_VERIFY_TLS", "false")
    cfg = load_config()
    assert cfg.central_url == "https://central.test"
    assert cfg.agent_id == 42
    assert cfg.api_key == "pn_envkey"
    assert cfg.verify_tls is False


def test_cli_flags_override_env(monkeypatch):
    monkeypatch.setenv("PRINTER_NANNY_CONFIG", "/definitely/not/here.toml")
    monkeypatch.setenv("PN_CENTRAL_URL", "https://env.test")
    monkeypatch.setenv("PN_AGENT_ID", "1")
    monkeypatch.setenv("PN_API_KEY", "env")
    cfg = load_config(cli={"agent_id": 99, "api_key": "cli", "central_url": None, "verify_tls": None})
    assert cfg.agent_id == 99       # flag wins
    assert cfg.api_key == "cli"     # flag wins
    assert cfg.central_url == "https://env.test"  # fell through to env


def test_merge_remote_keeps_local_subnets_when_central_has_none():
    local = parse_config(_valid())
    merged = merge_remote(local, {"snmp": {}, "subnets": []})
    assert len(merged.subnets) == 2  # fell back to local


def test_per_subnet_snmp_override():
    cfg = parse_config(_valid())
    default = cfg.snmp_for(cfg.subnets[0])
    overridden = cfg.snmp_for(cfg.subnets[1])
    assert default.community == "public"
    assert default.version == "2c"
    assert overridden.community == "printers-ro"
    assert overridden.version == "1"
    assert overridden.timeout == 3.0  # inherited from global snmp
