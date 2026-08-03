"""Pydantic v2 request/response schemas for the JSON API."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from central import models as m

# Upper bound for impression meters: the readings/printers meter columns are
# 32-bit INTEGER (INT4 on Postgres). A value at/above this would overflow the
# column (500, dropping the whole batch) -- and a negative meter is nonsense for
# billing -- so out-of-range counts from a misbehaving agent are coerced to
# "not reported" (None) at the trust boundary rather than poisoning billing.
_INT4_MAX = 2_147_483_647


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Ingest (agent -> central)
# --------------------------------------------------------------------------- #
class HeartbeatIn(BaseModel):
    version: Optional[str] = None
    # Agent-side install path so the operator can confirm WHERE the agent's
    # running from (matters when self-update lands in the wrong site-packages).
    install_path: Optional[str] = None
    # Outcome of the most recent self-update attempt (success or specific
    # failure). Carried so the dashboard can show "last update: ok at X" or
    # "FAILED: ..." without anyone reading log files on the agent host.
    last_update_result: Optional[dict] = None


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
    # Billing-grade meter split of page_count. None when the device/provider
    # reports no split (we never synthesize it). meter_snapshot is a vendor-shaped
    # per-function breakdown, e.g. {"total":N,"mono":N,"color":N,"print":N,...}.
    mono_count: Optional[int] = None
    color_count: Optional[int] = None
    meter_snapshot: Optional[dict] = None
    hostname: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    # Best-effort firmware/version parsed from sysDescr by the agent. Feeds the
    # device security-posture report; None when the device exposes nothing.
    firmware: Optional[str] = None
    supplies: list[SupplyIn] = Field(default_factory=list)
    events: list[EventIn] = Field(default_factory=list)
    # Per-poll vendor-provider diagnostics. Free-shape dicts (one per provider
    # that ran) -- the dashboard renders them as-is so providers can evolve
    # their summary without a schema migration.
    provider_trace: Optional[list[dict]] = None
    # Workstation driver tier, from the agent's IPP capability probe. Absent on
    # most readings: probing is throttled to a slow cadence (it is several HTTP
    # round-trips per device, and the answer changes only when firmware or the
    # device's IPP setting does), and legacy agents never send it. Absent means
    # "no new information" -- it must never be read as "no longer driverless"
    # and reset a printer that was probed successfully last week.
    driver_tier: Optional[m.DriverTier] = None
    driver_tier_reason: Optional[str] = None
    ipp_endpoint: Optional[str] = None
    ipp_capabilities: Optional[dict] = None

    @field_validator("page_count", "mono_count", "color_count")
    @classmethod
    def _sane_meter(cls, v: Optional[int]) -> Optional[int]:
        """Drop a negative or column-overflowing meter to None at ingest.

        Defense at the trust boundary: even an authenticated agent shouldn't be
        able to write a negative impression count (billing nonsense) or a value
        that overflows the INT4 column (which would 500 and drop the batch). A
        bad value is treated as "not reported" rather than rejecting the whole
        reading, so one glitchy field never costs the rest of the poll.
        """
        if v is None:
            return None
        return v if 0 <= v <= _INT4_MAX else None


class ReadingsBatchIn(BaseModel):
    readings: list[ReadingIn]


class DiscoveredIn(BaseModel):
    ip: str
    mac: Optional[str] = None
    hostname: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    firmware: Optional[str] = None
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
    # True when this subnet has a standby collector, i.e. the agent's right to
    # sweep it is a LEASE rather than a standing assignment. The agent must then
    # stop collecting it when its lease runs out on its own monotonic clock,
    # even (especially) while central is unreachable and it is running on the
    # cached config that carried this flag. False -- the default, and every
    # subnet with no standby -- means "collect it as you always have".
    leased: bool = False
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


class CollectorLeaseOut(BaseModel):
    """The collection leases this heartbeat granted, and how long they last.

    ``held`` is the complete set of leased subnets this agent may collect until
    the lease elapses -- a leased subnet absent from it is one this agent does
    NOT hold, whatever its cached config says. The agent anchors the deadline to
    a monotonic reading taken BEFORE it sent this request, so its own deadline is
    always earlier than the expiry central recorded (see central/collector.py):
    the holder gives up before the grantor will reallocate, with no assumption
    that the two clocks agree.
    """

    lease_seconds: int
    held: list[str] = Field(default_factory=list)


class HeartbeatOut(AgentOut):
    """AgentOut plus the collection leases, so one round trip settles ownership.

    A subclass rather than a changed shape: every field an existing agent reads
    is still here in the same place, and an agent too old to know about leases
    ignores the extra key -- which is safe because it also cannot be a standby
    (an operator has to name one) and because ingest refuses readings from an
    agent that does not hold the lease regardless of what it believes.
    """

    collector: Optional[CollectorLeaseOut] = None


class AgentRegisterIn(BaseModel):
    """What a self-registering agent sends when it redeems a claim code.

    Note what is absent: no site, no client, no name it can rely on. Those come
    from the token the operator minted, because this request is authenticated
    only by a bearer code -- letting the caller nominate a tenant would make
    holding a code equivalent to choosing whose fleet to join.
    """

    claim_code: str
    # Reported by the machine, so display-only. It is stored as a suffix on the
    # operator's chosen name rather than as the name itself.
    hostname: Optional[str] = None
    version: Optional[str] = None


class AgentRegistered(BaseModel):
    """The one and only time the minted key is transmitted."""

    agent_id: int
    api_key: str
    site_id: int
    name: str


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
    firmware: Optional[str] = None
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


# Payload keys each command type actually consumes, per the agent's dispatcher
# (``handle_commands`` in agent/printer_nanny_agent/runner.py). A command payload
# is acted on unattended inside a customer LAN by the agent's service account, so
# anything not listed here is refused rather than stored -- an unrecognised key is
# either a caller bug or an attempt to smuggle behaviour past the API.
#
# Two entries are deliberately empty. ``update_agent``'s only key, pip_source, is
# resolved server-side from the admin-only ``agent.pip_source`` setting (see
# ``enqueue_command``) because the agent pip-installs -- i.e. executes -- whatever
# source it is handed. ``update_config`` is logged by the agent and never applied
# (config is file-managed), so no key of it has any legitimate effect.
COMMAND_PAYLOAD_KEYS = {
    m.CommandType.rescan: frozenset(),
    m.CommandType.poll_now: frozenset(),
    m.CommandType.poll_printer: frozenset({"ip", "printer_id"}),
    m.CommandType.update_config: frozenset(),
    m.CommandType.update_agent: frozenset(),
}


class CommandIn(BaseModel):
    agent_id: int
    type: m.CommandType
    payload: Optional[dict] = None

    @model_validator(mode="after")
    def _check_payload(self) -> CommandIn:
        keys = set(self.payload or {})
        if "pip_source" in keys:
            # Rejected loudly rather than quietly stripped: a request naming its
            # own install source is an attack in progress, and a 4xx surfaces it
            # to the caller (and to error monitoring) instead of letting it look
            # like a perfectly ordinary enqueue.
            raise ValueError(
                "payload.pip_source is not accepted: central supplies the update "
                "source from the admin-only agent.pip_source setting"
            )
        # .get(): a CommandType added without an entry above rejects every key
        # (fail closed) instead of 500ing on a KeyError.
        unknown = sorted(keys - COMMAND_PAYLOAD_KEYS.get(self.type, frozenset()))
        if unknown:
            raise ValueError(
                f"payload keys not allowed for {self.type.value}: {', '.join(unknown)}"
            )
        return self


class MachineEnrollIn(BaseModel):
    """What a workstation sends to enroll.

    Note what is absent: any tenant selector. ``client_id`` comes from the
    enroll key, never from the caller -- a bearer credential must not let its
    holder choose whose fleet it lands in.
    """

    enroll_key: str
    machine_uid: str
    name: Optional[str] = None
    #: ``windows`` / ``macos``, recorded for the UI. Not trusted for the driver
    #: decision, which reads the platform stated on each assignments request.
    platform: Optional[str] = None


class MachineEnrolled(BaseModel):
    """The one and only time the minted machine key is transmitted."""

    machine_id: int
    api_key: str
    client_id: int
    #: False when an existing machine_uid re-enrolled and had its key rotated.
    #: Surfaced so a client can log which happened without central having to
    #: guess from timing.
    created: bool
    #: True when this enrollment took over a previous record by computer name --
    #: a re-imaged PC reclaiming its printers. Logged by the client so "where did
    #: these queues come from?" is answerable on the machine as well as centrally.
    adopted: bool = False


class MachineDriverOut(BaseModel):
    """The vendor driver a driver_required printer needs.

    ``sha256`` is not decoration: the client re-verifies the bytes it downloaded
    against it before unpacking anything, so a package altered on the volume or
    in transit never reaches pnputil.
    """

    package_id: int
    driver_name: str
    inf_relpath: str
    sha256: str
    size: int
    #: macOS only: how this package becomes a driver -- ``ppd`` (bind a PPD from
    #: the archive), ``pkg`` (install a vendor .pkg), or ``system`` (bind a PPD
    #: an MDM already installed, so there are no bytes to fetch). Null on
    #: Windows, where ``inf_relpath`` is the pointer.
    kind: Optional[str] = None
    #: macOS only: the path inside the archive for ``ppd``/``pkg``, or an
    #: absolute path on the Mac for ``system``. Named for what it is rather than
    #: reusing ``inf_relpath`` -- a PPD path in a field called "inf" is how the
    #: next person writes code that treats it like one.
    ref: Optional[str] = None


class MachinePrinterOut(BaseModel):
    """One queue the workstation should provision."""

    printer_id: int
    name: str
    ip: str
    is_default: bool
    driver_tier: Optional[str] = None
    ipp_endpoint: Optional[str] = None
    #: Present only for driver_required printers that have a usable package.
    #: Absent means the client skips the queue with a stated reason rather than
    #: binding a wrong driver.
    driver: Optional[MachineDriverOut] = None


class MachineAssignmentsOut(BaseModel):
    printers: list[MachinePrinterOut]
    machine_id: int
    #: Echoes which person this was resolved for, or null at the login screen.
    #: The client logs it, and it is what makes "why did this PC get those
    #: queues?" answerable without reproducing the resolver by hand.
    resolved_for: Optional[str] = None
    default_printer_id: Optional[int] = None
    #: Whether the client may take over Windows' own default-printer management
    #: for the signed-in user. Sent per poll rather than baked into the
    #: installer so an operator can change their mind without reinstalling.
    manage_default_printer: bool = True
    #: Whether this client's Macs may run ``installer -pkg`` for a vendor driver.
    #: Default **false**, and separate from the manager permission that allows
    #: the upload: a .pkg runs arbitrary pre/postinstall scripts as root, which
    #: is a wider grant than Windows' ``pnputil /add-driver``, so it must be
    #: opted into rather than inherited. Sent per poll so it can be withdrawn
    #: without touching every Mac.
    allow_macos_pkg_install: bool = False


class MachineCheckinIn(BaseModel):
    name: Optional[str] = None
    #: ``windows`` / ``macos``, for the UI. Optional because a client older than
    #: macOS support never sends one, and absent must read as "not yet known"
    #: rather than defaulting to a platform we then act on.
    platform: Optional[str] = None


class MachineCheckinOut(BaseModel):
    machine_id: int
    ok: bool
