"""Pydantic v2 request/response schemas for the JSON API."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from central import models as m


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Ingest (agent -> central)
# --------------------------------------------------------------------------- #
class HeartbeatIn(BaseModel):
    version: Optional[str] = None


class SupplyIn(BaseModel):
    type: m.SupplyType = m.SupplyType.toner
    color: Optional[str] = None
    description: Optional[str] = None
    level_pct: Optional[float] = Field(default=None, ge=0, le=100)
    status_note: Optional[str] = None
    current: Optional[int] = None
    max_capacity: Optional[int] = None
    unit: Optional[str] = None


class EventIn(BaseModel):
    code: Optional[str] = None
    severity: m.EventSeverity = m.EventSeverity.info
    source: m.EventSource = m.EventSource.snmp_alert
    message: str


class ReadingIn(BaseModel):
    """A single printer's poll result, addressed by IP within the agent's site."""

    ip: str
    ts: Optional[datetime] = None
    status: m.PrinterStatus = m.PrinterStatus.unknown
    page_count: Optional[int] = None
    hostname: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    supplies: list[SupplyIn] = Field(default_factory=list)
    events: list[EventIn] = Field(default_factory=list)
    # Per-poll vendor-provider diagnostics. Free-shape dicts (one per provider
    # that ran) -- the dashboard renders them as-is so providers can evolve
    # their summary without a schema migration.
    provider_trace: Optional[list[dict]] = None


class ReadingsBatchIn(BaseModel):
    readings: list[ReadingIn]


class DiscoveredIn(BaseModel):
    ip: str
    mac: Optional[str] = None
    hostname: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    subnet_cidr: Optional[str] = None


class DiscoveredBatchIn(BaseModel):
    devices: list[DiscoveredIn]


class CommandOut(ORMModel):
    id: int
    type: m.CommandType
    payload: Optional[dict] = None
    created_at: datetime


class PollTargetOut(ORMModel):
    """An approved printer the agent should poll, with its SNMP connection params."""

    id: int
    ip: str
    snmp_version: str
    snmp_community: Optional[str] = None
    snmp_v3: Optional[dict] = None


class AgentSubnetConfig(BaseModel):
    cidr: str
    snmp_community: str = "public"
    snmp_version: str = "2c"
    # Source IP / interface the agent should bind to when scanning this subnet.
    # Lets one agent serve multiple clients with overlapping RFC 1918 CIDRs
    # (each tunnel terminates at a unique local IP).
    bind_interface: Optional[str] = None
    # SNMPv3 USM credentials (used when snmp_version == "3"). JSON pass-through
    # of the Subnet.snmp_v3 column. See central/models.py:Subnet for keys.
    snmp_v3: Optional[dict] = None


class AgentConfigOut(BaseModel):
    """Central-managed config delivered to an agent (so its local file is just URL+key)."""

    poll_interval_seconds: int
    discovery_interval_seconds: int
    heartbeat_interval_seconds: int
    snmp: dict
    subnets: list[AgentSubnetConfig]


# --------------------------------------------------------------------------- #
# Management CRUD
# --------------------------------------------------------------------------- #
class ClientIn(BaseModel):
    name: str
    notes: Optional[str] = None


class ClientOut(ORMModel):
    id: int
    name: str
    notes: Optional[str] = None
    created_at: datetime


class SiteIn(BaseModel):
    client_id: int
    name: str
    address: Optional[str] = None
    contact: Optional[str] = None


class SiteOut(ORMModel):
    id: int
    client_id: int
    name: str
    address: Optional[str] = None
    contact: Optional[str] = None


class SubnetIn(BaseModel):
    site_id: int
    cidr: str
    agent_id: Optional[int] = None
    label: Optional[str] = None


class SubnetOut(ORMModel):
    id: int
    site_id: int
    agent_id: Optional[int] = None
    cidr: str
    label: Optional[str] = None


class AgentIn(BaseModel):
    site_id: int
    name: str


class AgentOut(ORMModel):
    id: int
    site_id: int
    name: str
    status: m.AgentStatus
    version: Optional[str] = None
    last_heartbeat: Optional[datetime] = None


class AgentCreated(AgentOut):
    # The plaintext key is returned exactly once, at creation time.
    api_key: str


class PrinterIn(BaseModel):
    client_id: int
    site_id: int
    ip: str
    hostname: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    location: Optional[str] = None
    snmp_version: str = "2c"
    snmp_community: Optional[str] = "public"
    notes: Optional[str] = None
    asset_tag: Optional[str] = None
    tags: Optional[list[str]] = None


class PrinterOut(ORMModel):
    id: int
    client_id: int
    site_id: int
    ip: str
    hostname: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    location: Optional[str] = None
    status: m.PrinterStatus
    discovery_state: m.DiscoveryState
    page_count: Optional[int] = None
    last_seen: Optional[datetime] = None
    notes: Optional[str] = None
    asset_tag: Optional[str] = None
    tags: Optional[list[str]] = None


class MaintenanceRecordIn(BaseModel):
    printer_id: int
    type: m.MaintenanceType = m.MaintenanceType.scheduled
    performed_by: Optional[str] = None
    notes: Optional[str] = None
    next_due: Optional[datetime] = None


class MaintenanceRecordOut(ORMModel):
    id: int
    printer_id: int
    type: m.MaintenanceType
    performed_by: Optional[str] = None
    performed_at: datetime
    notes: Optional[str] = None
    next_due: Optional[datetime] = None


class CommandIn(BaseModel):
    agent_id: int
    type: m.CommandType
    payload: Optional[dict] = None
