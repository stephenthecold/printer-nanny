"""Pydantic v2 request/response schemas for the JSON API."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Literal, Optional

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
# The ingest coercion rule
#
# **No value a device reports may reject the request it arrived in.** Pydantic
# validates the whole body, so one unusable field -- a vendor MIB answering 255
# for "unknown", a negative sentinel, a 300-character cartridge name off an EWS
# scrape -- would 422 the ENTIRE batch, every other printer's reading with it.
# And a rejected batch is not merely lost: ``push_readings`` spools it and
# ``drain_spool`` replays it verbatim on every cycle, so the same value is
# refused again forever. The spool never drains, grows to ``max_readings``, and
# the cap then drops the OLDEST entries -- destroying the good readings while
# retaining the poisoned one. One bad byte from one printer silently ends
# collection for that whole agent.
#
# Two variants of the same failure are invisible here and fatal in production,
# because SQLite (dev + the whole test suite) ignores what Postgres enforces:
# an int past INT4, and a string longer than its VARCHAR column (Postgres raises
# StringDataRightTruncation). Those surface as a 500 rather than a 422 and block
# the spool identically, so they are coerced/clipped at this boundary too.
#
# Coercion is always toward LESS information (None, "other", "unknown", a
# truncated string) and never toward more, so nothing here can invent a level, a
# meter or a severity that the device did not report. Only the request *shape*
# -- a missing ip, a malformed body -- is still refused.
# --------------------------------------------------------------------------- #
def _sane_count(v: Any) -> Optional[int]:
    """A device-reported count, or None when it cannot be stored.

    Shared by the impression meters and the supply raw counts: negative is
    nonsense for both, and anything past INT4 overflows the column. Runs in
    ``mode="before"`` so it also absorbs the shapes pydantic itself would refuse
    -- a float with a fractional part, a numeric string -- since those reject
    the batch just as thoroughly as an out-of-range int does.
    """
    if v is None or isinstance(v, bool):
        # bool is an int subclass in Python; a True page count is not a count.
        return None
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        v = int(v)
    if not isinstance(v, int):
        try:
            v = int(str(v).strip())
        except (TypeError, ValueError):
            return None
    return v if 0 <= v <= _INT4_MAX else None


def _clipped(v: Any, limit: int) -> Any:
    """Truncate a device-supplied string to what its column can hold.

    Non-strings pass through untouched so a genuinely wrong type is still
    reported by normal validation rather than being hidden here.
    """
    return v[:limit] if isinstance(v, str) else v


def _as_level(v: Any) -> Optional[float]:
    """``v`` as a finite float, or None if it is not a number at all."""
    if isinstance(v, bool):
        # float(True) is 1.0 -- a fabricated 1% reading out of a JSON boolean.
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


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


# Widths of the VARCHAR columns these fields land in (central.models.Supply).
_SUPPLY_TEXT_LIMITS = {"color": 40, "description": 200, "status_note": 60, "unit": 40}


def _level_refused_note(raw: Any) -> str:
    """The ``status_note`` marker for a level we would not store.

    Deliberately short, and written FIRST in the note, so that clipping to the
    60-char column can only ever eat the device's own wording -- never the
    explanation for why the percentage is missing.
    """
    shown = str(raw)
    if len(shown) > 12:
        shown = shown[:12] + "..."
    return f"level out of range: {shown}"


class SupplyIn(BaseModel):
    type: m.SupplyType = m.SupplyType.toner
    # prtMarkerSuppliesClass as the agent read it. Constrained to the three
    # values RFC 3805 defines plus ``None`` ("device did not report it"): this
    # field decides whether ``level_pct`` is read as remaining or as fullness,
    # so an unrecognised string must not reach the database and be silently
    # treated as "consumed" by ``central.supplies``. Older agents omit it,
    # which is exactly the ``None`` case.
    supply_class: Optional[Literal["consumed", "receptacle", "other"]] = None
    color: Optional[str] = None
    description: Optional[str] = None
    #: 0-100, or None for "not reported". Deliberately NOT ``Field(ge=0, le=100)``
    #: -- a constraint here refuses the whole batch. See ``_refuse_bad_level``.
    level_pct: Optional[float] = None
    status_note: Optional[str] = None
    current: Optional[int] = None
    max_capacity: Optional[int] = None
    unit: Optional[str] = None

    @field_validator("type", mode="before")
    @classmethod
    def _known_type(cls, v: Any) -> Any:
        """An unrecognised supply type becomes ``other``, never a 422.

        ``other`` is the enum's own catch-all and the row keeps its description,
        colour and counts, so nothing is lost but the classification. The case
        that matters is an agent NEWER than central (agents self-update; central
        is updated by the operator) reporting a supply type this build has never
        heard of -- which would otherwise refuse every batch that agent sends,
        permanently.
        """
        if v is None or isinstance(v, m.SupplyType):
            return v
        try:
            return m.SupplyType(v)
        except ValueError:
            return m.SupplyType.other

    @model_validator(mode="before")
    @classmethod
    def _refuse_bad_level(cls, data: Any) -> Any:
        """Coerce an unusable level to None and SAY SO in ``status_note``.

        Coerce rather than clamp: an out-of-range level is a sentinel, not a
        measurement. Clamping 255 to 100 would report a full cartridge and
        suppress the low-supply alert; clamping -1 to 0 would raise a false
        empty. "Not reported" is the one reading that is true, and it is exactly
        what ``status_note`` exists to caption ("coarse state when no numeric
        level is reported"), so the supply row survives with its identity,
        counts and description intact.

        The trace is a per-state note rather than a log line or an event on
        purpose: a device that answers 255 answers 255 on every poll, so an
        event would be unbounded noise, while the note renders once next to the
        supply on the printer page and in the supplies CSV export.
        """
        if not isinstance(data, dict):
            return data
        raw = data.get("level_pct")
        if raw is None:
            return data
        level = _as_level(raw)
        if level is not None and 0.0 <= level <= 100.0:
            return data
        data = dict(data)  # never mutate the caller's mapping
        data["level_pct"] = None
        note = _level_refused_note(raw)
        existing = data.get("status_note")
        if isinstance(existing, str) and existing.strip():
            note = f"{note}; {existing.strip()}"
        data["status_note"] = note
        return data

    @field_validator("color", "description", "status_note", "unit", mode="before")
    @classmethod
    def _clip_text(cls, v: Any, info) -> Any:
        return _clipped(v, _SUPPLY_TEXT_LIMITS[info.field_name])

    @field_validator("current", "max_capacity", mode="before")
    @classmethod
    def _sane_raw_count(cls, v: Any) -> Optional[int]:
        """Same guard as the impression meters: these are INT4 columns too."""
        return _sane_count(v)


class EventIn(BaseModel):
    code: Optional[str] = None
    severity: m.EventSeverity = m.EventSeverity.info
    source: m.EventSource = m.EventSource.snmp_alert
    message: str

    @field_validator("code", mode="before")
    @classmethod
    def _clip_code(cls, v: Any) -> Any:
        return _clipped(v, 80)  # printer_events.code

    @field_validator("severity", mode="before")
    @classmethod
    def _known_severity(cls, v: Any) -> Any:
        """An unrecognised severity becomes ``warning``.

        Not ``info`` (which buries something we failed to understand) and not
        ``critical`` (which fabricates an emergency out of a parse failure).
        Middle of the scale is the only honest answer for "this device said
        something this build cannot grade".
        """
        if v is None or isinstance(v, m.EventSeverity):
            return v
        try:
            return m.EventSeverity(v)
        except ValueError:
            return m.EventSeverity.warning

    @field_validator("source", mode="before")
    @classmethod
    def _known_source(cls, v: Any) -> Any:
        """An unrecognised source becomes ``agent``: appended, never reconciled.

        ``_reconcile_events`` treats ``snmp_alert`` rows as standing conditions
        and resolves the ones absent from a reading. Defaulting an unknown
        source into that set would let an event we cannot interpret close real
        open conditions; ``agent`` is the append-only source and cannot.
        """
        if v is None or isinstance(v, m.EventSource):
            return v
        try:
            return m.EventSource(v)
        except ValueError:
            return m.EventSource.agent


# Widths of the VARCHAR columns these land in (central.models.Printer).
_READING_TEXT_LIMITS = {
    "ip": 64, "hostname": 200, "brand": 100, "model": 200, "serial": 120,
    "firmware": 200, "driver_tier_reason": 400, "ipp_endpoint": 300,
}


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

    @field_validator("page_count", "mono_count", "color_count", mode="before")
    @classmethod
    def _sane_meter(cls, v: Any) -> Optional[int]:
        """Drop a negative or column-overflowing meter to None at ingest.

        Defense at the trust boundary: even an authenticated agent shouldn't be
        able to write a negative impression count (billing nonsense) or a value
        that overflows the INT4 column (which would 500 and drop the batch). A
        bad value is treated as "not reported" rather than rejecting the whole
        reading, so one glitchy field never costs the rest of the poll.

        ``mode="before"`` because the shapes pydantic itself refuses -- a meter
        reported as 12.5, or as a string -- reject the batch just as thoroughly
        as an out-of-range int, and for the same non-reason.
        """
        return _sane_count(v)

    @field_validator("ip", "hostname", "brand", "model", "serial", "firmware",
                     "driver_tier_reason", "ipp_endpoint", mode="before")
    @classmethod
    def _clip_text(cls, v: Any, info) -> Any:
        return _clipped(v, _READING_TEXT_LIMITS[info.field_name])

    @field_validator("driver_tier", mode="before")
    @classmethod
    def _known_tier(cls, v: Any) -> Any:
        """An unrecognised driver tier reads as absent, not as a 422.

        Absent is already the documented "no new information" (probing is
        throttled, and most readings carry no tier at all), so a tier this build
        does not know leaves the printer's last good observation in place --
        which is the same thing a routine SNMP poll does.
        """
        if v is None or isinstance(v, m.DriverTier):
            return v
        try:
            return m.DriverTier(v)
        except ValueError:
            return None

    @field_validator("status", mode="before")
    @classmethod
    def _known_status(cls, v: Any) -> Any:
        """An unrecognised status becomes ``unknown`` -- the enum's own "we
        don't know", and the value a reading carries when nothing answered."""
        if v is None or isinstance(v, m.PrinterStatus):
            return v
        try:
            return m.PrinterStatus(v)
        except ValueError:
            return m.PrinterStatus.unknown


class ReadingsBatchIn(BaseModel):
    readings: list[ReadingIn]


# Same columns, same widths -- discovery writes the identity fields readings do.
_DISCOVERED_TEXT_LIMITS = dict(_READING_TEXT_LIMITS, mac=32)


class DiscoveredIn(BaseModel):
    ip: str
    mac: Optional[str] = None
    hostname: Optional[str] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None
    firmware: Optional[str] = None
    subnet_cidr: Optional[str] = None

    @field_validator("ip", "mac", "hostname", "brand", "model", "serial",
                     "firmware", mode="before")
    @classmethod
    def _clip_text(cls, v: Any, info) -> Any:
        # subnet_cidr is deliberately absent: it is only ever matched against
        # enrolled Subnet rows, never stored, so it has no column to overflow.
        return _clipped(v, _DISCOVERED_TEXT_LIMITS[info.field_name])


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
