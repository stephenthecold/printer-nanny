"""Agent config parsing and per-subnet SNMP overrides."""

from __future__ import annotations

import pytest

from printer_nanny_agent.config import parse_config


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


def test_per_subnet_snmp_override():
    cfg = parse_config(_valid())
    default = cfg.snmp_for(cfg.subnets[0])
    overridden = cfg.snmp_for(cfg.subnets[1])
    assert default.community == "public"
    assert default.version == "2c"
    assert overridden.community == "printers-ro"
    assert overridden.version == "1"
    assert overridden.timeout == 3.0  # inherited from global snmp
