"""Agent configuration loaded from a TOML file.

Resolution order for the path: explicit ``--config`` arg -> ``$PRINTER_NANNY_CONFIG``
-> ``/etc/printer-nanny/agent.toml``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import List, Optional

try:  # Python 3.11+ has tomllib in the stdlib.
    import tomllib as _toml

    def _load(fp):
        return _toml.load(fp)

except ModuleNotFoundError:  # 3.9/3.10
    import tomli as _toml

    def _load(fp):
        return _toml.load(fp)

from printer_nanny_agent.snmp import SnmpParams

DEFAULT_CONFIG_PATH = "/etc/printer-nanny/agent.toml"


@dataclass
class SubnetConfig:
    cidr: str
    community: Optional[str] = None  # overrides the global SNMP community
    version: Optional[str] = None
    # Optional source IP / interface the agent should bind to when scanning
    # this subnet. Lets one agent serve multiple clients with overlapping
    # internal CIDRs (each tunnel terminates at a unique local IP).
    bind_interface: Optional[str] = None
    # SNMPv3 (USM) credentials. None when this subnet uses v1/v2c. Keys mirror
    # the JSON shape pushed by central -- user / security_level / auth_protocol
    # / auth_password / priv_protocol / priv_password / context_name.
    snmp_v3: Optional[dict] = None


@dataclass
class AgentConfig:
    central_url: str
    agent_id: int
    api_key: str
    poll_interval_seconds: int = 300
    discovery_interval_seconds: int = 3600
    heartbeat_interval_seconds: int = 60
    verify_tls: bool = True
    snmp: SnmpParams = field(default_factory=SnmpParams)
    subnets: List[SubnetConfig] = field(default_factory=list)

    def snmp_for(self, subnet: SubnetConfig) -> SnmpParams:
        """SNMP params for a subnet, applying per-subnet overrides."""
        v3 = subnet.snmp_v3 or {}
        return SnmpParams(
            community=subnet.community or self.snmp.community,
            version=subnet.version or self.snmp.version,
            port=self.snmp.port,
            timeout=self.snmp.timeout,
            retries=self.snmp.retries,
            # subnet-level bind only -- the global SnmpParams default leaves
            # bind_interface None (use the OS default route).
            bind_interface=subnet.bind_interface,
            v3_user=v3.get("user"),
            v3_security_level=v3.get("security_level"),
            v3_auth_protocol=v3.get("auth_protocol"),
            v3_auth_password=v3.get("auth_password"),
            v3_priv_protocol=v3.get("priv_protocol"),
            v3_priv_password=v3.get("priv_password"),
            v3_context_name=v3.get("context_name"),
        )


def merge_remote(config: AgentConfig, remote: dict) -> AgentConfig:
    """Overlay central-delivered config (subnets, intervals, SNMP) onto the local file.

    The local file stays authoritative for central_url / agent_id / api_key /
    verify_tls; everything operational comes from central. Remote subnets, when
    present, replace the local list (central is the source of truth).
    """
    snmp_raw = remote.get("snmp", {})
    snmp = SnmpParams(
        community=snmp_raw.get("community", config.snmp.community),
        version=str(snmp_raw.get("version", config.snmp.version)),
        port=config.snmp.port,
        timeout=float(snmp_raw.get("timeout", config.snmp.timeout)),
        retries=int(snmp_raw.get("retries", config.snmp.retries)),
    )
    remote_subnets = [
        SubnetConfig(
            cidr=s["cidr"],
            community=s.get("snmp_community"),
            version=str(s["snmp_version"]) if s.get("snmp_version") else None,
            bind_interface=s.get("bind_interface"),
            snmp_v3=s.get("snmp_v3") or None,
        )
        for s in remote.get("subnets", [])
    ]
    return replace(
        config,
        snmp=snmp,
        subnets=remote_subnets or config.subnets,
        poll_interval_seconds=int(remote.get("poll_interval_seconds", config.poll_interval_seconds)),
        discovery_interval_seconds=int(
            remote.get("discovery_interval_seconds", config.discovery_interval_seconds)
        ),
        heartbeat_interval_seconds=int(
            remote.get("heartbeat_interval_seconds", config.heartbeat_interval_seconds)
        ),
    )


def resolve_config_path(explicit: Optional[str] = None) -> str:
    return explicit or os.environ.get("PRINTER_NANNY_CONFIG") or DEFAULT_CONFIG_PATH


# Environment variables that can supply config without any file.
_ENV_MAP = {
    "central_url": "PN_CENTRAL_URL",
    "agent_id": "PN_AGENT_ID",
    "api_key": "PN_API_KEY",
    "verify_tls": "PN_VERIFY_TLS",
}


def _env_overrides() -> dict:
    data: dict = {}
    for key, env in _ENV_MAP.items():
        val = os.environ.get(env)
        if val is None or val == "":
            continue
        data[key] = val.lower() not in ("0", "false", "no") if key == "verify_tls" else val
    return data


def load_config(
    path: Optional[str] = None, cli: Optional[dict] = None
) -> AgentConfig:
    """Build config from a TOML file (if present) overlaid with env vars and CLI flags.

    Precedence: CLI flags > env vars > file. A file is optional -- env/flags alone
    are enough, which is what the one-line installer relies on.
    """
    data: dict = {}
    config_path = resolve_config_path(path)
    if config_path and os.path.exists(config_path):
        with open(config_path, "rb") as fp:
            data = _load(fp)
    data.update(_env_overrides())
    if cli:
        data.update({k: v for k, v in cli.items() if v is not None})
    return parse_config(data)


def parse_config(data: dict) -> AgentConfig:
    """Build an AgentConfig from a parsed TOML mapping (separated for testing)."""
    missing = [k for k in ("central_url", "agent_id", "api_key") if k not in data]
    if missing:
        raise ValueError(f"config missing required keys: {', '.join(missing)}")

    snmp_raw = data.get("snmp", {})
    snmp = SnmpParams(
        community=snmp_raw.get("community", "public"),
        version=str(snmp_raw.get("version", "2c")),
        port=int(snmp_raw.get("port", 161)),
        timeout=float(snmp_raw.get("timeout", 2.0)),
        retries=int(snmp_raw.get("retries", 1)),
    )
    subnets = [
        SubnetConfig(
            cidr=s["cidr"],
            community=s.get("community"),
            version=str(s["version"]) if s.get("version") is not None else None,
            bind_interface=s.get("bind_interface"),
            snmp_v3=s.get("snmp_v3"),
        )
        for s in data.get("subnets", [])
    ]
    return AgentConfig(
        central_url=str(data["central_url"]).rstrip("/"),
        agent_id=int(data["agent_id"]),
        api_key=str(data["api_key"]),
        poll_interval_seconds=int(data.get("poll_interval_seconds", 300)),
        discovery_interval_seconds=int(data.get("discovery_interval_seconds", 3600)),
        heartbeat_interval_seconds=int(data.get("heartbeat_interval_seconds", 60)),
        verify_tls=bool(data.get("verify_tls", True)),
        snmp=snmp,
        subnets=subnets,
    )
