"""SQLAlchemy ORM models for the Printer Nanny central server.

Hierarchy: Client -> Site -> (Subnet, Agent, Printer). Printers carry Supplies and
time-series Readings; PrinterEvents capture errors/status; Maintenance and Alert
tables track service and notifications. Enums are stored as VARCHAR
(native_enum=False) so the same models work on SQLite and Postgres.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from central.db import Base
from central.money import Money, Rate


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _enum(py_enum: type[enum.Enum]) -> Enum:
    """Store an enum as a portable VARCHAR rather than a native DB enum type."""
    return Enum(py_enum, native_enum=False, validate_strings=True, length=32)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class UserRole(str, enum.Enum):
    admin = "admin"
    tech = "tech"
    client_readonly = "client_readonly"


class AgentStatus(str, enum.Enum):
    online = "online"
    offline = "offline"
    never_seen = "never_seen"


class DiscoveryState(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    ignored = "ignored"


class PrinterStatus(str, enum.Enum):
    ok = "ok"
    warning = "warning"
    error = "error"
    offline = "offline"
    unknown = "unknown"


class DriverTier(str, enum.Enum):
    """How a workstation queue for this printer has to be created.

    Driver installation is what strands most workstation setups, and since
    KB5005652 (Aug 2021) the reason is privilege rather than packaging:
    ``RestrictDriverInstallationToAdministrators`` defaults to 1, so Point and
    Print demands local admin. The workstation client sidesteps that entirely by
    running as LocalSystem -- but it still has to know, per device, whether a
    driver is needed at all.

    Five values rather than two, because they call for different remediation and
    the difference is invisible in a single "failed" state:

    ``driverless``       the Windows inbox IPP class driver drives it; nothing
                         to install. Preferred -- as of 2026-07-01 Windows ranks
                         that driver ahead of third-party ones by default.
    ``driver_required``  answers IPP but below the class driver's bar, so the
                         privileged service must stage a vendor driver.
    ``ipp_disabled``     the device refused port 631. Many ship with IPP off;
                         that is a checkbox in the printer's web UI, NOT a
                         driver problem. Conflating the two sends a technician
                         to entirely the wrong place, which is why it is its own
                         value and not folded into ``driver_required``.
    ``unreachable``      nothing answered (offline, firewalled, wrong address).
    ``error``            answered, but with nothing we could decode.
    """

    driverless = "driverless"
    driver_required = "driver_required"
    ipp_disabled = "ipp_disabled"
    unreachable = "unreachable"
    error = "error"


#: The only tiers an operator may pin. The other three describe a failure to
#: reach or decode the device -- states you fix on the device or the network,
#: not opinions to override.
OVERRIDABLE_DRIVER_TIERS = (DriverTier.driverless, DriverTier.driver_required)


class SupplyType(str, enum.Enum):
    toner = "toner"
    ink = "ink"
    drum = "drum"
    fuser = "fuser"
    waste = "waste"
    staples = "staples"
    developer = "developer"
    other = "other"


class EventSeverity(str, enum.Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class EventSource(str, enum.Enum):
    snmp_alert = "snmp_alert"
    status = "status"
    agent = "agent"


class MaintenanceType(str, enum.Enum):
    scheduled = "scheduled"
    repair = "repair"
    supply_replace = "supply_replace"


class AlertScope(str, enum.Enum):
    global_ = "global"
    client = "client"
    site = "site"
    printer = "printer"


class SuppressionKind(str, enum.Enum):
    """What shape of window this is.

    ``quiet_hours`` -- recurring weekly, expressed in the client's LOCAL
                       wall-clock time (a weekday mask plus start/end minutes).
                       "Don't wake anyone between 18:00 and 07:00."
    ``maintenance`` -- a single dated UTC range. "We're swapping the fleet
                       Saturday 08:00-14:00."
    """

    quiet_hours = "quiet_hours"
    maintenance = "maintenance"


class SuppressionAction(str, enum.Enum):
    """What happens to a notification that lands inside the window.

    ``defer``    -- hold it and deliver when the window ends, batched into a
                    digest. Nothing is lost; the operator just isn't woken.
                    The sensible default for quiet hours.
    ``suppress`` -- don't notify at all. The alert still opens and stays visible
                    on the dashboard; only the notification is dropped, and the
                    delivery row records ``suppressed`` so the drop is on the
                    books rather than silent. The sensible default for planned
                    maintenance, where a digest of 40 expected "printer offline"
                    alerts is pure noise and defeats the point of the window.
    """

    defer = "defer"
    suppress = "suppress"


class AlertConditionType(str, enum.Enum):
    supply_below = "supply_below"          # threshold = percent
    error_severity = "error_severity"      # threshold mapped to EventSeverity rank
    offline_minutes = "offline_minutes"    # threshold = minutes an *agent* is offline
    maintenance_due = "maintenance_due"    # no threshold
    # A single printer has stopped answering. Distinct from offline_minutes,
    # which watches agent heartbeats: an unreachable printer produces no reading
    # at all (the agent drops it from the batch), so staleness of
    # ``Printer.last_seen`` is the only signal central ever gets.
    printer_offline = "printer_offline"    # threshold = minutes since last reading
    # Forecast-driven: a supply is projected to hit empty within the configured
    # reorder lead-time (alerts.reorder_lead_days). Raised by the worker's
    # forecast pass, not by an AlertRule, so it has its own open/resolve
    # lifecycle (auto-resolves when the cartridge is swapped/refilled).
    predicted_depletion = "predicted_depletion"
    # Rate, not state: N matching printer_events inside a rolling window of
    # AlertRule.window_minutes. Every other condition here asks "is this true
    # right now"; this one asks "has this happened too often lately", which is
    # the difference between "the printer is jammed" (already covered by
    # error_severity) and "the printer jams ten times a day" (nothing covered
    # it). threshold = the count N; see AlertRule for the matching columns.
    occurrence_rate = "occurrence_rate"


class AlertState(str, enum.Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"


class ChannelType(str, enum.Enum):
    email = "email"
    freescout = "freescout"
    teams = "teams"
    webhook = "webhook"
    slack = "slack"


class DeliveryStatus(str, enum.Enum):
    """Lifecycle of a single per-channel notification send attempt.

    ``pending``  -- queued / awaiting (re)try at ``next_attempt_at``.
    ``delivered`` -- the channel reported success AND transmitted; terminal.
    ``failed``   -- last attempt failed but more retries remain (still due at
                    ``next_attempt_at``); functionally a retryable ``pending``.
    ``dead``     -- exhausted the max-attempts cap; terminal, dead-lettered.
    ``skipped``  -- the channel succeeded without transmitting anything: a
                    severity gate excluded the notification, or the channel is
                    enabled but unconfigured (dry-run). Terminal and NOT a
                    delivery. Previously these were stored as ``delivered``,
                    so the durable log asserted a send that never happened.
                    Terminal because a severity skip is deterministic, and
                    because replaying every historical skip the moment a URL
                    is finally pasted would flood the new channel with
                    backlog. The alert stays open and visible either way, and
                    the alerts page renders the channel with a distinct
                    "not sent" badge carrying the reason.
    ``deferred`` -- held by a quiet-hours window. NOT terminal: it carries
                    ``next_attempt_at`` = the instant the window ends, so the
                    existing retry sweeper is the wake mechanism and the
                    notification is delivered (batched into a digest) the moment
                    the window closes. Distinct from ``failed`` because nothing
                    went wrong and no attempt was burned -- an operator reading
                    the log must be able to tell "we chose to wait" from
                    "the channel broke".
    ``suppressed`` -- dropped by a suppression window (planned maintenance).
                    Terminal, and deliberately recorded rather than simply not
                    written: a notification an operator asked to silence is
                    still a notification that did not arrive, and the log should
                    say so.

    Stored as VARCHAR (see ``_enum``), so adding a member needs no DDL.
    """

    pending = "pending"
    delivered = "delivered"
    failed = "failed"
    dead = "dead"
    skipped = "skipped"
    deferred = "deferred"
    suppressed = "suppressed"


class CommandType(str, enum.Enum):
    rescan = "rescan"
    poll_now = "poll_now"
    poll_printer = "poll_printer"  # payload: {"ip": "..."} or {"printer_id": N}
    update_config = "update_config"
    # Agent self-update: pip install --force-reinstall --no-deps <pip_source>,
    # then exit so the service manager (systemd / NSSM) restarts the process
    # against the freshly-installed code. payload: {"pip_source": "git+..."}.
    update_agent = "update_agent"
    # --- Remote hands (see central/remote.py) ---------------------------------
    # These three carry a ``request_id`` and are the only command types whose
    # result travels BACK to central. They are never enqueueable through the
    # JSON command API: each one has to pass a capability gate, a rate limit and
    # a tenancy check that only the remote routes perform, so ``CommandIn``
    # refuses them outright rather than letting a caller construct one directly.
    remote_fetch = "remote_fetch"    # payload: {request_id, ip, scheme, port, path}
    remote_probe = "remote_probe"    # payload: {request_id, ip}
    remote_write = "remote_write"    # payload: {request_id, ip, op, oid, snmp_type, value}


class CommandStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    done = "done"


class RemoteCapability(str, enum.Enum):
    """Whether this device accepts writes FROM US, as proven by a probe.

    ``unknown`` is the shipped state of every printer and behaves exactly like
    ``read_only`` -- a device that has not been proven writable is read-only.
    The two are kept distinct only so the UI can say "not checked yet" instead
    of asserting a result nobody measured.
    """

    unknown = "unknown"
    read_only = "read_only"
    writable = "writable"


class RemoteRequestKind(str, enum.Enum):
    fetch = "fetch"    # read-only HTTP GET against the device's web server
    probe = "probe"    # no-op SNMP SET to establish capability
    write = "write"    # a named operation from remote.WRITE_OPS


class RemoteRequestStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    succeeded = "succeeded"
    failed = "failed"
    expired = "expired"


# --------------------------------------------------------------------------- #
# Tenancy: Client -> Site
# --------------------------------------------------------------------------- #
class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, default=None)
    # IANA zone name (e.g. "America/New_York"). NULL means "use the global
    # alerts.default_timezone". Quiet hours are wall-clock-local by nature --
    # "don't call after 18:00" means the CLIENT's 18:00, and an MSP's clients do
    # not all share a zone. Validated against zoneinfo.available_timezones() on
    # save, and resolved defensively at read time (see central.suppression), so
    # a stale or hand-edited value degrades to UTC instead of killing the worker.
    timezone: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    # --- Per-client white-label branding (customer portal only) ---------------
    # NULL/blank on any of these means "inherit the global app.* setting", and
    # the fallback is per-field: a client that sets only a colour keeps the
    # MSP's name and logo. See central/branding.py for where this is applied
    # (and, just as deliberately, where it is not: alert email stays global).
    #
    # brand_primary_color is validated to #rgb/#rrggbb on the way in AND
    # re-checked on the way out, because it is interpolated into a CSS
    # declaration. brand_logo_url is either an external https URL or the
    # internal /branding/clients/<id>/logo path an upload points it at; the
    # bytes themselves live in app_assets under client_logo_asset_name(), so
    # there is one blob store and one uploader rather than two.
    brand_name: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    brand_logo_url: Mapped[Optional[str]] = mapped_column(String(1000), default=None)
    brand_primary_color: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sites: Mapped[list[Site]] = relationship(back_populates="client", cascade="all, delete-orphan")
    printers: Mapped[list[Printer]] = relationship(back_populates="client")


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[Optional[str]] = mapped_column(String(400), default=None)
    contact: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    client: Mapped[Client] = relationship(back_populates="sites")
    agents: Mapped[list[Agent]] = relationship(back_populates="site", cascade="all, delete-orphan")
    subnets: Mapped[list[Subnet]] = relationship(back_populates="site", cascade="all, delete-orphan")
    printers: Mapped[list[Printer]] = relationship(back_populates="site")

    __table_args__ = (UniqueConstraint("client_id", "name", name="uq_site_client_name"),)


# --------------------------------------------------------------------------- #
# Collection: Agent -> Subnet
# --------------------------------------------------------------------------- #
class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    api_key_hash: Mapped[str] = mapped_column(String(128), index=True)
    # ``version`` now embeds an install-time marker (``0.1.0+YYYYMMDD-HHMMSS``)
    # so the operator can SEE whether a self-update actually replaced the
    # package files just by comparing the suffix before and after Update.
    version: Mapped[Optional[str]] = mapped_column(String(80), default=None)
    status: Mapped[AgentStatus] = mapped_column(_enum(AgentStatus), default=AgentStatus.never_seen)
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    # Filesystem location the agent is running from. Pulled from the agent's
    # __file__ on every heartbeat. Useful for "is pip installing to user
    # site-packages vs the venv?" diagnostics.
    install_path: Mapped[Optional[str]] = mapped_column(String(400), default=None)
    # Outcome of the most recent self-update attempt (set after the agent
    # restarts post-pip-install, or on the same process if pip failed).
    # JSON keys: status ("ok" | "pip_failed" | ...), detail, ts.
    last_update_result: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    site: Mapped[Site] = relationship(back_populates="agents")
    # ``Subnet`` now carries several FKs to ``agents`` (primary / standby /
    # current lease holder), so this one has to say which it means. Without
    # ``foreign_keys`` SQLAlchemy cannot choose and raises at mapper
    # configuration time. It means the PRIMARY assignment, unchanged: a card on
    # the Agents page still lists the subnets this agent owns, and the subnets
    # it is merely standing by for are listed separately.
    subnets: Mapped[list[Subnet]] = relationship(
        back_populates="agent", foreign_keys="Subnet.agent_id"
    )
    standby_subnets: Mapped[list[Subnet]] = relationship(
        foreign_keys="Subnet.standby_agent_id", viewonly=True
    )
    commands: Mapped[list[Command]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )


class AgentClaimToken(Base):
    """A short-lived, single-use code that lets an agent enroll itself.

    The existing path mints the agent first and makes the operator carry its id
    and API key to the site by hand. That key is long-lived and full-privilege
    from the moment it is displayed, so every copy of it -- the clipboard, the
    chat message it was pasted into, the ticket it was attached to -- is a
    standing credential. A claim code inverts that: it is worthless after one
    redemption and after its TTL, so the thing that travels is the thing that
    expires, and the long-lived key is minted at the destination and never
    leaves it.

    What the redeeming agent may NOT choose is the important part. ``site_id``
    is fixed by the operator at mint time, because the code is a bearer
    credential -- anyone holding it could otherwise self-declare a tenant and
    land inside another client's fleet. The agent supplies only a hostname,
    which is device-supplied input and is therefore treated as a display label
    and nothing more.

    ``token_hash`` stores SHA-256 of the code, never the code, mirroring
    ``Agent.api_key_hash``: a database dump must not yield a working
    enrollment. Single use is enforced as a conditional UPDATE on ``used_at``
    rather than a read-then-write, so two agents racing the same code cannot
    both win.
    """

    __tablename__ = "agent_claim_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True
    )
    # Name for the agent created on redemption. Operator-supplied, so it is the
    # one piece of naming that is trustworthy; a hostname reported by the
    # machine only ever appends to it.
    agent_name: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    used_by_agent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), default=None
    )

    site: Mapped[Site] = relationship()

    # Redemption looks up by hash and then filters on "still valid", and the
    # expiry sweep scans by expires_at; both are covered here rather than only
    # in the migration, because revision 0001 is ``create_all()`` -- an index
    # declared only in a migration is silently absent on every fresh install.
    __table_args__ = (
        Index("ix_agent_claim_token_expires", "expires_at"),
    )


class Subnet(Base):
    __tablename__ = "subnets"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), default=None, index=True
    )
    cidr: Mapped[str] = mapped_column(String(64))
    label: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    # SNMP creds for this subnet -- pushed to the owning agent for discovery.
    snmp_community: Mapped[str] = mapped_column(String(120), default="public")
    snmp_version: Mapped[str] = mapped_column(String(8), default="2c")
    # SNMPv3 credentials, used when snmp_version == "3". JSON blob mirroring
    # Printer.snmp_v3 so per-subnet v3 config matches per-printer override
    # patterns. Keys:
    #   user                -- USM security name
    #   security_level      -- noAuthNoPriv | authNoPriv | authPriv
    #   auth_protocol       -- MD5 | SHA | SHA224 | SHA256 | SHA384 | SHA512
    #   auth_password       -- shared secret (treat at-rest encryption as a
    #                          design-doc follow-up; today this is plaintext)
    #   priv_protocol       -- DES | 3DES | AES128 | AES192 | AES256
    #   priv_password       -- shared secret
    #   context_name        -- optional engine context (default "")
    # All keys are optional except `user`; defaults map to noAuthNoPriv.
    snmp_v3: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    # Optional source IP / interface name the agent should bind SNMP packets to
    # when sweeping this subnet. Lets one agent serve multiple clients whose
    # internal RFC 1918 CIDRs overlap (each tunnel terminates at a different
    # local IP / interface; bind-per-subnet routes packets to the right one).
    bind_interface: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    # Auto-approve discoveries found here instead of queueing them for review.
    #
    # This is the ONLY thing that can move a device into a tenant's fleet without
    # a human, so it is deliberately a property of the subnet rather than a
    # global switch: an operator had to type this CIDR and its SNMP credentials
    # by hand to create the row at all, and marking it trusted re-uses that
    # existing deliberate act rather than inventing new trust. A device on a
    # CIDR nobody enrolled still queues, and so does one whose subnet_cidr the
    # agent doesn't report -- "unknown provenance" must never mean "approved".
    #
    # Defaults False so an upgrade changes nobody's behaviour, and so a subnet
    # created in a hurry is not silently more permissive than one created
    # carefully.
    trusted: Mapped[bool] = mapped_column(Boolean, default=False)
    # ------------------------------------------------------------------ #
    # Collector redundancy (see central/collector.py for the whole rule set)
    # ------------------------------------------------------------------ #
    # The second agent allowed to collect this subnet when the primary stops.
    # NULL -- the overwhelmingly common case, and the shipped default -- means
    # this subnet has no redundancy and is NOT leased at all: ``agent_id``
    # collects it, exactly as before, with no lease check on any path. Every
    # behaviour below switches on "is standby_agent_id set", so an upgrade
    # changes nobody's collection.
    #
    # Exactly one standby, not a list: picking between two eligible standbys is
    # a coin flip, and the whole takeover path exists to refuse coin flips
    # (services.adopt_by_name, "exactly one candidate or none").
    standby_agent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), default=None, index=True
    )
    # Who is collecting RIGHT NOW, and until when. This is a lease, not a
    # setting: it is granted by a conditional UPDATE (never read-then-write) and
    # it expires. ``collector_lease_expires_at`` doubles as an acquisition
    # BARRIER -- after a lease is revoked the holder is cleared but the expiry
    # is left in place, so no successor may acquire before the instant the old
    # holder must already have stopped collecting. That is what makes a
    # revocation overlap-free without a second column.
    collector_agent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), default=None, index=True
    )
    collector_lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # When the current holder took the lease -- "last takeover" in the UI.
    collector_since: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # The predecessor and the instant its tenure ended. Readings are pushed in
    # batches and spooled across a central outage, so a displaced collector will
    # legitimately replay readings it took while it DID hold the lease. Those
    # are history, not duplicates -- they are strictly older than the successor's
    # first reading -- so ingest admits them from this one agent, bounded by
    # this timestamp, and refuses everything else.
    collector_prev_agent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), default=None
    )
    collector_prev_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # Discovery status (updated by the ingest endpoint on each /discovered batch).
    last_discovery_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_discovery_found_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    last_discovery_new_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    site: Mapped[Site] = relationship(back_populates="subnets")
    agent: Mapped[Optional[Agent]] = relationship(
        back_populates="subnets", foreign_keys=[agent_id]
    )
    standby_agent: Mapped[Optional[Agent]] = relationship(foreign_keys=[standby_agent_id])
    collector_agent: Mapped[Optional[Agent]] = relationship(foreign_keys=[collector_agent_id])

    __table_args__ = (
        UniqueConstraint("site_id", "cidr", name="uq_subnet_site_cidr"),
        # The worker's takeover sweep looks for leased subnets whose lease has
        # lapsed. Declared here as well as in migration 0040 because revision
        # 0001 is ``create_all()`` -- an index declared only in a migration is
        # silently absent on every fresh install.
        Index("ix_subnet_collector_lease", "collector_lease_expires_at"),
    )


# --------------------------------------------------------------------------- #
# Devices: Printer -> Supply, Reading, Event
# --------------------------------------------------------------------------- #
class Printer(Base):
    __tablename__ = "printers"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    discovered_by_agent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), default=None
    )

    ip: Mapped[str] = mapped_column(String(64), index=True)
    mac: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    hostname: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    # Operator-chosen friendly name ("Front Desk", "Lab Copier"). Preferred
    # over model/hostname everywhere a printer is named -- dashboards, alert
    # titles, notification emails -- so alerts read "Front Desk toner low"
    # instead of a bare model number and IP.
    display_name: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    brand: Mapped[Optional[str]] = mapped_column(String(100), default=None)
    model: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    # Lookups are always site-scoped, so the composite index in __table_args__
    # covers this column -- see services.find_printer_in_sites.
    serial: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    location: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    # Firmware / version string, best-effort from sysDescr (or a vendor field)
    # during polling. Used by the device security-posture report so a regulated
    # buyer can answer "what firmware is this endpoint running?". Honestly None
    # when the device exposes nothing parseable -- the posture view shows
    # "unknown" rather than inventing a value.
    firmware: Mapped[Optional[str]] = mapped_column(String(200), default=None)

    # SNMP connection details (community for v1/v2c; v3 creds stored in snmp_v3 jsonb).
    snmp_version: Mapped[str] = mapped_column(String(8), default="2c")
    snmp_community: Mapped[Optional[str]] = mapped_column(String(120), default="public")
    snmp_v3: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    status: Mapped[PrinterStatus] = mapped_column(_enum(PrinterStatus), default=PrinterStatus.unknown)
    discovery_state: Mapped[DiscoveryState] = mapped_column(
        _enum(DiscoveryState), default=DiscoveryState.pending, index=True
    )
    page_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    # Latest mono/color impression meters (billing-grade). Total lives in
    # page_count; these split it. None when the device/provider doesn't report a
    # split (we never invent it). This is a display cache of the most recent
    # reading; billing diffs the append-only readings series, not this value.
    mono_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    color_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    # Operator-managed metadata: free-text notes, an asset/lease/inventory tag,
    # and a list of short labels (e.g. "lease", "vip", "color").
    notes: Mapped[Optional[str]] = mapped_column(Text, default=None)
    asset_tag: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    tags: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # Last poll's vendor-provider diagnostics -- which providers ran, whether
    # each one succeeded, and a short summary of what data it contributed.
    # Used by the printer detail page so an operator can see at a glance why
    # (for example) a Brother is still showing "buckets only" -- maybe PJL
    # was unreachable on port 9100, or EWS scraping fell off a layout
    # pattern. The shape is a list of dicts, one per provider that ran:
    #   {"name": "brother_pjl", "ok": false, "error": "connect refused",
    #    "fields": [], "summary": "PJL port 9100 unreachable"}
    last_provider_trace: Mapped[Optional[list]] = mapped_column(JSON, default=None)

    # --- workstation driver tier -------------------------------------------
    # What the agent's IPP probe observed, and what an operator decided if they
    # disagreed. Kept in two columns on purpose: a re-probe must be free to
    # update what it saw without silently discarding a human's decision, and an
    # operator must be able to see both ("we detect driver_required, you pinned
    # driverless") rather than a single value with no provenance.
    driver_tier: Mapped[Optional[DriverTier]] = mapped_column(
        _enum(DriverTier), default=None, index=True
    )
    # Why the probe reached that conclusion, in words an operator can act on
    # ("advertises only IPP 1.1 -- the inbox class driver needs 2.0 or later").
    # Rendered verbatim; the probe writes no value it would not want read aloud
    # to a technician.
    driver_tier_reason: Mapped[Optional[str]] = mapped_column(String(400), default=None)
    driver_tier_override: Mapped[Optional[DriverTier]] = mapped_column(
        _enum(DriverTier), default=None
    )
    driver_probed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # The IPP URI that actually answered. Devices vary on path (/ipp/print,
    # /ipp/printer, ...), so the working one is recorded rather than re-derived.
    ipp_endpoint: Mapped[Optional[str]] = mapped_column(String(300), default=None)
    # Free-shape capability detail from the probe (versions, document formats,
    # finishings). Diagnostics only -- nothing keys off its contents, so the
    # probe can add fields without a migration.
    ipp_capabilities: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    # --- remote hands ------------------------------------------------------
    # What a capability probe OBSERVED, and separately what an operator
    # DECIDED -- the same two-column shape as driver_tier above, for the same
    # reason: a re-probe must refresh what we saw without discarding a human's
    # decision. The asymmetry is the security property, and it is why the
    # operator column is a boolean rather than a mirror of the enum: an
    # operator may pin a device read-only, and may NOT pin one writable. A
    # "writable" override would be an inference standing in for evidence, which
    # is the one thing this feature must never do (central/remote.py).
    remote_capability: Mapped[RemoteCapability] = mapped_column(
        _enum(RemoteCapability), default=RemoteCapability.unknown
    )
    remote_capability_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # What the probe (or a real write) actually observed, in words an operator
    # can act on: "the device refused the SET (noAccess) -- SNMP writes are
    # disabled, or the community we hold is read-only".
    remote_capability_detail: Mapped[Optional[str]] = mapped_column(String(400), default=None)
    remote_write_disabled: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def effective_driver_tier(self) -> Optional[DriverTier]:
        """The tier the workstation client should act on: override beats probe."""
        return self.driver_tier_override or self.driver_tier

    @property
    def driver_tier_is_overridden(self) -> bool:
        """True when an operator pinned a tier that differs from what we saw."""
        return (
            self.driver_tier_override is not None
            and self.driver_tier_override != self.driver_tier
        )

    client: Mapped[Client] = relationship(back_populates="printers")
    site: Mapped[Site] = relationship(back_populates="printers")
    supplies: Mapped[list[Supply]] = relationship(
        back_populates="printer", cascade="all, delete-orphan"
    )
    readings: Mapped[list[Reading]] = relationship(
        back_populates="printer", cascade="all, delete-orphan"
    )
    events: Mapped[list[PrinterEvent]] = relationship(
        back_populates="printer", cascade="all, delete-orphan"
    )
    maintenance_records: Mapped[list[MaintenanceRecord]] = relationship(
        back_populates="printer", cascade="all, delete-orphan"
    )

    # Identity is the serial, not the address. `(site_id, ip)` used to be UNIQUE,
    # which encoded "an IP identifies a printer at a site" -- the assumption that
    # makes DHCP churn corrupt data. It also made the correct behaviour
    # impossible to express: once a replaced device is recorded separately from
    # the one it replaced, both legitimately reference the same address (one
    # retired, one live), so a hard uniqueness rule could only be satisfied by
    # merging their histories, which is the very thing that breaks billing.
    #
    # Uniqueness now sits where identity actually lives. The index is partial
    # because plenty of devices report no serial over SNMP; those rows fall back
    # to IP matching and must not all collide on NULL. Both Postgres and SQLite
    # support partial indexes.
    __table_args__ = (
        Index(
            "uq_printer_site_serial",
            "site_id",
            "serial",
            unique=True,
            postgresql_where=text("serial IS NOT NULL"),
            sqlite_where=text("serial IS NOT NULL"),
        ),
        Index("ix_printer_site_ip", "site_id", "ip"),
    )


class Supply(Base):
    __tablename__ = "supplies"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[SupplyType] = mapped_column(_enum(SupplyType), default=SupplyType.toner)
    # prtMarkerSuppliesClass, verbatim from the device: "consumed",
    # "receptacle", "other", or NULL when the device did not report the column
    # (also every row written before this existed).
    #
    # It is stored rather than derived because it changes what ``level_pct``
    # MEANS -- remaining for a cartridge, how-full for a waste box -- and a
    # value that reverses the reading of another column has to travel with it.
    # NULL is honestly "not reported", never "consumed": the fallback lives in
    # ``central.supplies.is_receptacle`` where it can be stated once, not
    # frozen into the row by whichever agent build happened to write it.
    supply_class: Mapped[Optional[str]] = mapped_column(String(20), default=None)
    color: Mapped[Optional[str]] = mapped_column(String(40), default=None)  # black/cyan/magenta/yellow
    description: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    # For a CONSUMED supply this is how much remains; for a RECEPTACLE it is how
    # full the container is. Never read it without asking ``central.supplies``
    # which one you have -- 5% is a nearly-dead cartridge and a nearly-empty
    # waste box, and treating those alike is what this column exists to stop.
    level_pct: Mapped[Optional[float]] = mapped_column(Float, default=None)  # None == unknown
    # Coarse state when no numeric level is reported (e.g. "some remaining").
    status_note: Mapped[Optional[str]] = mapped_column(String(60), default=None)
    current: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    max_capacity: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    unit: Mapped[Optional[str]] = mapped_column(String(40), default=None)
    # Persisted supply-depletion forecast, written by the worker's forecast pass
    # (regression fit over the recent depleting segment) so dashboards, the
    # customer portal, and reports can read a days-to-empty estimate without
    # re-fitting the reading history on every render. ``None`` means "not yet
    # trustworthy / nothing depleting"; ``forecast_at`` stamps when it was last
    # computed so a stale estimate can be aged out or shown with a timestamp.
    days_to_empty: Mapped[Optional[float]] = mapped_column(Float, default=None)
    # Companion to days_to_empty on the PAGES axis: level fitted against the
    # printer's page meter rather than against time. Written by the same worker
    # pass off the same rows, so it costs no extra query.
    #
    # It is a separate measurement rather than days x pages-per-day because the
    # two say different things. Days-remaining is volatile -- a quiet week
    # inflates it -- while pages-remaining is a property of the cartridge, is
    # directly comparable against the page yield a cartridge is sold by, and does
    # not move when the customer simply stops printing. The reorder
    # recommendations (central.reorder) trigger on either, so a supply whose
    # days estimate is too noisy to trust can still be recommended on pages.
    # ``None`` means "no trustworthy estimate", never "plenty left".
    pages_to_empty: Mapped[Optional[float]] = mapped_column(Float, default=None)
    forecast_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    printer: Mapped[Printer] = relationship(back_populates="supplies")

    __table_args__ = (
        UniqueConstraint("printer_id", "type", "color", name="uq_supply_printer_type_color"),
    )

    @property
    def is_receptacle(self) -> bool:
        """True when ``level_pct`` means "how full", not "how much is left".

        A property rather than a template global because the dashboard builds
        **four** separate Jinja environments (routes / manage / settings /
        backup): a global registered on one is missing from the other three, and
        the failure is a template rendering a waste box the wrong way round with
        nothing in the log. Reached from Jinja as ``sup.is_receptacle``, so no
        route has to remember to pass it. The rule itself lives once, in
        ``central.supplies``; this only forwards.
        """
        from central.supplies import is_receptacle  # lazy: supplies imports models

        return is_receptacle(self)


class Reading(Base):
    """Append-only per-poll time-series: the raw material for meters and forecasts.

    NOT partitioned. This docstring used to claim Postgres range-partitioned it
    monthly "see migration"; migration 0002 explicitly *deferred* partitioning
    ("can be layered on later if retention volume demands it") and shipped a BRIN
    index on ``ts`` instead, so the claim described a table that has never
    existed. Correcting it matters because it was load-bearing in the wrong
    direction -- it read as "growth is handled", which is how a table with no
    retention at all reached ~52M rows/year at 500 printers unnoticed.

    Volume is bounded by ``central.retention`` instead: readings older than
    ``retention.raw_days`` (90) collapse into one ``ReadingRollup`` per printer
    per UTC day, kept forever, and the raw rows are removed only where an
    operator has explicitly enabled deletion.
    """

    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    page_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    # Per-reading mono/color impression meters (billing diffs across the period
    # read these). None when no split is reported. ``meter_snapshot`` holds the
    # richer, vendor-shaped per-function breakdown (e.g. print/copy/fax/total)
    # without needing a column per function -- same pattern as supply_snapshot.
    mono_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    color_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    meter_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    status: Mapped[PrinterStatus] = mapped_column(_enum(PrinterStatus), default=PrinterStatus.unknown)
    supply_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    printer: Mapped[Printer] = relationship(back_populates="readings")

    # Every hot query on this table is "one printer, a time range": the forecast
    # pass (printer_id = X AND ts >= now-30d), supply_runway, page-count trends,
    # and the retention pass below. Only two single-column indexes existed, so
    # Postgres scanned every reading a printer had ever produced and filtered by
    # ts -- on the largest table in the schema, on every worker cycle. Declared
    # here rather than only in migration 0034 because revision 0001 is
    # ``create_all()``: an index that lives only in a migration is silently
    # absent on every fresh install.
    __table_args__ = (
        Index("ix_readings_printer_ts", "printer_id", "ts"),
    )


class ReadingRollup(Base):
    """One row per printer per UTC day, collapsed from the raw ``readings`` series.

    This is what makes retention possible without losing history: raw readings
    are kept ``retention.raw_days`` (90) days and then reduce to one of these,
    which is kept forever. See ``central.retention`` for the pass that writes
    them and the rules that govern deletion.

    **Meters are stored twice on purpose.** ``page_count``/``mono_count``/
    ``color_count`` are the day's LAST reported (cumulative, lifetime) values and
    ``*_start`` the day's FIRST. A cumulative meter alone answers "how many pages
    has this device ever printed"; billing asks "how many during this period",
    which is a difference. Holding both ends means one row answers it for its own
    day, so a month's volume is a sum over 30 self-contained rows rather than a
    chain that silently reads zero wherever a day is missing.

    **Per-supply levels are in here, and that was a decision.** They are not
    needed by anything today -- ``FORECAST_HISTORY_WINDOW_DAYS`` and
    ``RUNWAY_HISTORY_WINDOW_DAYS`` are both 30, which the 90-day raw window
    clears by 3x, so the forecast reads raw readings exclusively and must keep
    doing so. They are here because the alternative fails silently later:
    without them, widening the forecast window past 90 days would return exactly
    the same answer as 90 days, forever, with no error and nothing to grep --
    the shape of failure this codebase keeps paying for. A daily sample is
    sufficient for that future because the fit is percent-per-DAY against a
    3-day confidence floor: toner does not move meaningfully inside one day, so
    sub-daily samples carry noise, not signal. ``supply_snapshot`` is the
    end-of-day snapshot in byte-identical shape to ``Reading.supply_snapshot``,
    so widening the window is a change to the query and not to the parser.

    ``raw_pruned`` says whether the raw readings behind this row are gone. It is
    not decoration: it selects the write rule. False means the raw rows are still
    there, so a re-run RECOMPUTES from them (idempotent). True means they were
    deleted, so a later straggler for the same day is MERGED in (recomputing
    would silently discard the whole day and keep only the straggler).
    """

    __tablename__ = "reading_rollups"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE")
    )
    # UTC calendar day. Deliberately a DATE and deliberately UTC: readings are
    # stored in UTC, and a rollup bucketed in a local zone would shift whenever
    # an operator changed a client's timezone, re-bucketing history that has
    # already been billed.
    day: Mapped[date] = mapped_column(Date)

    readings_count: Mapped[int] = mapped_column(Integer, default=0)
    # The real span of the readings behind this row, not the day's boundaries --
    # a printer that was offline until 18:00 must not read as a full day.
    first_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    page_count_start: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    page_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    mono_count_start: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    mono_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    color_count_start: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    color_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)

    supply_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    raw_pruned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    printer: Mapped[Printer] = relationship()

    __table_args__ = (
        # The uniqueness IS the idempotency: "roll up (printer, day)" has to be
        # safe to re-run after a crash, and a second row for the same day would
        # double-count every period that summed it.
        UniqueConstraint("printer_id", "day", name="uq_reading_rollup_printer_day"),
        # Fleet-wide date ranges ("every printer, last month") lead with the day;
        # the unique constraint's btree already serves the per-printer direction.
        Index("ix_reading_rollups_day", "day"),
    )


class SupplyCycle(Base):
    """One cartridge's life in one slot: fitted here, replaced there, N pages between.

    **This is the measurement half of yield-gap detection** (``central.supply_yield``);
    the verdict is computed on read and stored nowhere, exactly as the reorder
    recommendation is. What is persisted is what cannot be recomputed cheaply: a
    cartridge can last a year, and the raw readings behind it are only kept
    ``retention.raw_days`` (90). Rolling this up as it happens is what lets a
    twelve-month drum be measured at all.

    A "cycle" is bounded by two replacements, detected by
    ``supplies.refill_boundaries`` -- a level that RISES by more than the
    tolerance, which is the only cartridge-change signal a printer gives us. It
    follows that the first cycle we ever see for a slot is **left-truncated**: we
    joined partway through some cartridge's life, so its pages are not its yield.
    That is why ``start_level_pct`` is recorded rather than assumed to be 100,
    and why the yield calculation normalises by the level actually consumed and
    refuses a cycle that consumed too little to say anything.

    **Both ends of every measure are stored, and the direction of error matters.**
    ``pages`` accumulates POSITIVE deltas only (a meter reset contributes 0, per
    ``queries.positive_delta``), so a replaced formatter board cannot manufacture
    a huge yield. ``min_level_pct`` rather than ``end_level_pct`` is what the
    consumed fraction is measured against, because a cartridge that read 2% and
    then blipped back to 4% before it was swapped consumed 98 points, not 96.
    Understating consumption OVERSTATES yield, which fails towards "no finding" --
    the safe direction when the finding is an accusation.
    """

    __tablename__ = "supply_cycles"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), index=True
    )
    # The slot, spelled as the device reports it in ``supply_snapshot``. Stored
    # as plain strings rather than a FK to ``supplies.id`` because the slot
    # outlives the row: a Supply row is (printer, type, color) and is rewritten
    # by ingest, while "the black toner slot on printer 7" is what a cartridge
    # history is about. ``color`` is "" and never NULL so the lookup is a plain
    # equality on every dialect -- NULLs compare distinct in SQL, which would
    # silently open a second cycle for the same slot on a device that reports no
    # colour.
    supply_type: Mapped[str] = mapped_column(String(40))
    color: Mapped[str] = mapped_column(String(40), default="")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The newest observation folded into this cycle. Doubles as the scan
    # watermark: the next pass reads strictly after it, so a poll is never
    # counted twice and the history is never re-walked.
    last_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # When the replacement that ENDED this cycle was observed. NULL == still the
    # cartridge in the machine.
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )

    start_level_pct: Mapped[float] = mapped_column(Float)
    end_level_pct: Mapped[float] = mapped_column(Float)
    min_level_pct: Mapped[float] = mapped_column(Float)

    start_page_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    end_page_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    #: Positive meter deltas summed across the cycle. Not
    #: ``end_page_count - start_page_count``: that reads a meter reset as a
    #: negative cartridge.
    pages: Mapped[int] = mapped_column(Integer, default=0)
    readings_count: Mapped[int] = mapped_column(Integer, default=1)
    #: True once a replacement closed it. An open cycle is a measurement in
    #: progress and is never yield evidence -- a cartridge half used is not a
    #: cartridge that under-delivered.
    complete: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    printer: Mapped[Printer] = relationship()

    __table_args__ = (
        # Every read is "this printer's cycles for this slot" -- the scan looks
        # up the open one, the report aggregates the complete ones.
        Index("ix_supply_cycles_printer_slot", "printer_id", "supply_type", "color"),
        # ...except the replacement log, which is "the fleet's, newest first".
        Index("ix_supply_cycles_ended", "ended_at"),
    )


class SupplyYieldExpectation(Base):
    """What a cartridge for this model is SUPPOSED to yield, entered by an operator.

    Deliberately global (no ``client_id``): a cartridge's rated yield is a
    property of the hardware, not of the customer who owns it, and scoping it per
    tenant would mean re-typing the same datasheet number for every client with
    the same printer.

    ``model_tag`` is a case-insensitive SUBSTRING of ``printers.model``, matched
    by the same rule as a driver package: at least 3 characters, longest tag
    wins, and an exact tie is REFUSED rather than guessed. SNMP model strings
    vary between firmware revisions ("Brother MFC-L8900CDW series"), so a
    substring is the only workable match -- and a coin flip between two
    equally-specific numbers would produce a yield gap that is an artefact of row
    order.

    ``color`` is "" for "any colour of this supply type", which is the common
    case (one rated yield for a colour set). A row naming a specific colour is
    more specific and wins.
    """

    __tablename__ = "supply_yield_expectations"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_tag: Mapped[str] = mapped_column(String(200))
    supply_type: Mapped[str] = mapped_column(String(40))
    color: Mapped[str] = mapped_column(String(40), default="")
    #: Rated pages per cartridge, e.g. 3000 for a standard-yield black toner.
    expected_pages: Mapped[int] = mapped_column(Integer)
    note: Mapped[Optional[str]] = mapped_column(String(300), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "model_tag", "supply_type", "color", name="uq_supply_yield_expectation"
        ),
    )


class PrinterEvent(Base):
    __tablename__ = "printer_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    code: Mapped[Optional[str]] = mapped_column(String(80), default=None)
    severity: Mapped[EventSeverity] = mapped_column(_enum(EventSeverity), default=EventSeverity.info)
    source: Mapped[EventSource] = mapped_column(_enum(EventSource), default=EventSource.status)
    message: Mapped[str] = mapped_column(Text)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)

    printer: Mapped[Printer] = relationship(back_populates="events")

    # Occurrence-rate rules ask "how many matching events for this printer since
    # T", once per rule per cycle, forever. The pre-existing single-column
    # indexes make that a scan of everything the printer has ever emitted
    # followed by a filter -- this table is append-only for anything that isn't
    # a standing snmp_alert condition, so that cost grows without bound as an
    # install ages. Leading with printer_id and ranging on ts turns it into one
    # index range per printer.
    #
    # Declared here and NOT only in migration 0037: revision 0001 is a
    # Base.metadata.create_all(), so the ORM metadata is what builds a fresh
    # database. An index that lives only in the migration is silently absent on
    # every new install -- exactly the trap ix_suppression_enabled_scope
    # documents next door.
    __table_args__ = (Index("ix_printer_events_printer_ts", "printer_id", "ts"),)


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #
class MaintenanceSchedule(Base):
    __tablename__ = "maintenance_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Either a per-printer schedule or a model-level rule (printer_id NULL).
    printer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), default=None, index=True
    )
    model: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    name: Mapped[str] = mapped_column(String(200))
    interval_days: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    page_threshold: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    # Meter reading at the last logged service. This is what lets a page-driven
    # schedule RECUR: the effective target is ``last_serviced_page_count +
    # page_threshold`` (see ``page_target``), so a serviced kit is next due one
    # kit-life further on rather than staying permanently due.
    #
    # NULL means "never serviced" -> base 0 -> the target is the configured
    # threshold, which is exactly what this schedule did before the column
    # existed. So every existing row keeps firing where it always did, and no
    # data migration is needed. Rolling ``page_threshold`` itself instead was
    # rejected: the step would then be read back out of the value it had just
    # been added to, doubling on every service.
    last_serviced_page_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    # Component-life trigger: when set, the worker opens a maintenance-due alert
    # once the matching component-life Supply row (belt / fuser / laser / drum /
    # PF kit — populated by the Brother provider's maintenance blob) drops to
    # ``life_threshold`` percent or below. Independent of interval_days /
    # page_threshold; a schedule may use any combination. ``component_type`` is
    # one of the slugs in MaintenanceSchedule.COMPONENT_TYPES.
    component_type: Mapped[Optional[str]] = mapped_column(String(32), default=None)
    life_threshold: Mapped[Optional[float]] = mapped_column(Float, default=None)
    next_due: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Operator-selectable component types -> how they map onto the Supply rows
    # the Brother provider writes (see agent brother_maintenance._EXTRA_PART_ROWS):
    #   fuser  -> Supply(type=fuser)
    #   drum   -> Supply(type=drum)               (any color)
    #   belt   -> Supply(type=other, color=belt)
    #   laser  -> Supply(type=other, color=laser)
    #   pf_kit -> Supply(type=other, color in {pf-kit-mp, pf-kit-1})
    COMPONENT_TYPES = ("fuser", "drum", "belt", "laser", "pf_kit")

    def page_target(self) -> Optional[int]:
        """The meter reading at which this schedule is next due on pages.

        One source of truth for the worker's due-check, the schedules table and
        the "marked serviced" message -- three places that must agree about
        when the next service lands, and would otherwise each re-derive it.
        None when the schedule has no page trigger at all.
        """
        if self.page_threshold is None:
            return None
        return (self.last_serviced_page_count or 0) + self.page_threshold


class MaintenanceRecord(Base):
    __tablename__ = "maintenance_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[MaintenanceType] = mapped_column(
        _enum(MaintenanceType), default=MaintenanceType.scheduled
    )
    performed_by: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    performed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notes: Mapped[Optional[str]] = mapped_column(Text, default=None)
    next_due: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)

    printer: Mapped[Printer] = relationship(back_populates="maintenance_records")


# --------------------------------------------------------------------------- #
# Alerting + notifications
# --------------------------------------------------------------------------- #
class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    scope: Mapped[AlertScope] = mapped_column(_enum(AlertScope), default=AlertScope.global_)
    scope_id: Mapped[Optional[int]] = mapped_column(Integer, default=None)  # client/site/printer id
    condition_type: Mapped[AlertConditionType] = mapped_column(_enum(AlertConditionType))
    threshold: Mapped[Optional[float]] = mapped_column(Float, default=None)
    severity: Mapped[EventSeverity] = mapped_column(_enum(EventSeverity), default=EventSeverity.warning)
    channel_ids: Mapped[Optional[list]] = mapped_column(JSON, default=None)  # [notification_channel.id]
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # -- occurrence_rate only ------------------------------------------------ #
    # The rolling window W, in minutes, over which ``threshold`` occurrences are
    # counted. NULL on every other condition type, and a rule that reaches the
    # evaluator without one is skipped rather than defaulted: guessing a window
    # invents the operator's intent, and an unbounded one is the append-only
    # full-table scan the readings forecast already had to be walked back from.
    window_minutes: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    # Which events count. Matched case-insensitively as a SUBSTRING of
    # PrinterEvent.code -- "jam" catches the agent's "jammed" without an
    # operator having to know the RFC 3805 spelling. NULL/empty counts every
    # event in the window. Deliberately NOT matched against PrinterEvent.message:
    # that text is device-controlled free-form prose, so matching on it would
    # make a rule's behaviour depend on a string a printer on a customer LAN
    # chooses, and it cannot be indexed.
    match_code: Mapped[Optional[str]] = mapped_column(String(80), default=None)
    # Optional severity floor for what counts. NULL means "any severity",
    # including info -- several normal conditions (low paper, power-save
    # offline) are recorded at info, so a rate rule that wants only real faults
    # sets this to warning. Kept separate from ``severity`` (which is the
    # severity of the alert this rule RAISES) so an operator can raise a
    # critical alert about a flood of warnings.
    match_min_severity: Mapped[Optional[EventSeverity]] = mapped_column(
        _enum(EventSeverity), default=None
    )


class SuppressionWindow(Base):
    """When NOT to notify -- recurring quiet hours and one-off maintenance windows.

    One table for both shapes because they share everything that matters: the
    scope they apply to, the break-through floor, and what happens to a
    notification that lands inside them. Only the time expression differs, and
    ``kind`` selects which pair of columns is authoritative:

    - ``quiet_hours`` uses ``weekdays`` + ``start_minute``/``end_minute``, which
      are **local wall-clock** minutes from midnight in the scoped client's
      timezone. ``end_minute <= start_minute`` means the window wraps midnight
      (18:00->07:00), which is the common case and therefore must not be an
      error. Storing minutes-as-int rather than a TIME column keeps it portable
      across SQLite/Postgres and makes the wrap comparison explicit.
    - ``maintenance`` uses ``starts_at``/``ends_at``, absolute UTC instants. A
      planned outage happens once at a known moment; there is no recurrence and
      no wall-clock ambiguity to resolve.

    ``scope``/``scope_id`` reuse the AlertRule and NotificationChannel
    convention, so an operator already knows what global/client/site/printer
    means here. Overlap is resolved FLAT rather than most-specific-first (see
    ``central.suppression.evaluate``): every covering window the severity does
    not break through is considered, any ``suppress`` wins, and otherwise the
    notification defers until the latest end among them. Scope ranking was
    rejected on purpose -- it implies exclusions ("this printer is exempt from
    the client policy") that this model cannot express, and a precedence rule
    that silently *reduces* suppression is the dangerous direction to be wrong
    in.

    ``min_severity_breakthrough`` is the safety valve: alerts at or above it
    ignore the window entirely. It defaults to ``critical`` because silencing a
    site-down overnight is a liability an MSP cannot carry by accident.

    ``allow_breakthrough=False`` is total silence -- nothing escapes, critical
    included. It is a separate flag rather than "leave the floor NULL" because
    ``critical`` is the top of ``EventSeverity``, so a floor alone can only ever
    *loosen* a window, never make it fully quiet; and because SQLAlchemy cannot
    distinguish an unset column from one explicitly set to None once the column
    carries a Python-side default, which would have made NULL mean "critical"
    exactly when an operator asked for silence. Defaulting the flag to True keeps
    the safe behaviour for any window created without thinking about it.
    """

    __tablename__ = "suppression_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[SuppressionKind] = mapped_column(
        _enum(SuppressionKind), default=SuppressionKind.quiet_hours
    )
    action: Mapped[SuppressionAction] = mapped_column(
        _enum(SuppressionAction), default=SuppressionAction.defer
    )
    scope: Mapped[AlertScope] = mapped_column(_enum(AlertScope), default=AlertScope.global_)
    scope_id: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    # Alerts at or above this severity ignore the window...
    min_severity_breakthrough: Mapped[EventSeverity] = mapped_column(
        _enum(EventSeverity), default=EventSeverity.critical
    )
    # ...unless this is False, in which case nothing breaks through at all.
    # ``true()`` renders per dialect (1 on SQLite, TRUE on Postgres). A literal
    # ``text("1")`` here made ``alembic upgrade head`` fail outright on a fresh
    # Postgres -- "column is of type boolean but default expression is of type
    # integer" -- so the documented compose bootstrap could not create its
    # schema at all. SQLite accepts 1 for a boolean, which is exactly why the
    # whole suite passed over it; see tests/test_postgres_bootstrap.py.
    allow_breakthrough: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    # -- recurring (quiet_hours) --------------------------------------------- #
    # Minutes from LOCAL midnight, 0..1439. end <= start wraps past midnight.
    start_minute: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    end_minute: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    # ISO weekdays the window applies to, Monday=0 .. Sunday=6. NULL/empty means
    # every day -- the overwhelmingly common "every night" case needs no setup.
    weekdays: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # -- one-off (maintenance) ---------------------------------------------- #
    starts_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    ends_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Declared here, not only in migration 0025: revision 0001 is a
    # Base.metadata.create_all(), so the ORM metadata -- not the migration
    # chain -- is what builds a fresh database. An index that exists only in the
    # migration is silently absent on every new install, because 0025's
    # "does the table already exist?" guard correctly short-circuits.
    # The evaluator reads enabled+scope on every dispatch, so this is the hot path.
    __table_args__ = (
        Index("ix_suppression_enabled_scope", "enabled", "scope", "scope_id"),
    )


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("alert_rules.id", ondelete="SET NULL"), default=None
    )
    printer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), default=None, index=True
    )
    agent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), default=None, index=True
    )
    type: Mapped[AlertConditionType] = mapped_column(_enum(AlertConditionType))
    severity: Mapped[EventSeverity] = mapped_column(_enum(EventSeverity), default=EventSeverity.warning)
    state: Mapped[AlertState] = mapped_column(_enum(AlertState), default=AlertState.open, index=True)
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[Optional[str]] = mapped_column(Text, default=None)
    # De-dupe key so the worker doesn't re-open the same condition every cycle.
    dedupe_key: Mapped[str] = mapped_column(String(200), index=True)
    notified_channels: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    # External tracker reference captured at open time (today: the FreeScout
    # conversation/ticket id). Persisted so the closed-loop resolver can post a
    # "resolved" note + close that exact ticket when the alert auto-resolves.
    external_ref: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    # Escalation / re-notify bookkeeping. ``last_notified_at`` is stamped on
    # every dispatch (initial open + each escalation re-send); ``escalation_level``
    # starts at 0 on open and increments each time the worker re-notifies an
    # alert that has stayed unresolved past ``alerts.escalate_after_minutes``.
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    escalation_level: Mapped[int] = mapped_column(Integer, default=0)
    # How many times this condition has cleared and re-fired inside the flap
    # cooldown window (alerts.renotify_cooldown_min). A flapping condition
    # re-opens THIS alert instead of raising a fresh one, so the operator sees a
    # single item with a flap count rather than a notification per oscillation.
    # 0 means it has never flapped.
    flap_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[ChannelType] = mapped_column(_enum(ChannelType))
    config: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    scope: Mapped[AlertScope] = mapped_column(_enum(AlertScope), default=AlertScope.global_)
    scope_id: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationDelivery(Base):
    """Durable per-channel send attempt for an alert -- the retry/dead-letter log.

    Alert dedupe suppresses re-notification while an alert stays open, so a
    failed channel send used to be recorded on ``Alert.notified_channels`` and
    then dropped forever -- a transient SMTP/Slack/webhook outage silently lost
    the alert. Each (alert, channel) send now gets a row here: on failure it
    stays ``pending``/``failed`` with an exponential-backoff ``next_attempt_at``,
    the ``retry_deliveries`` worker job re-sends it when due, marks it
    ``delivered`` on success, and dead-letters it (``dead``) once it has used up
    the configured max-attempts cap.

    ``channel_key`` is the active-channel name (e.g. "Email", "Slack") so the
    retry job can rebuild the live channel from ``active_channels`` without
    pinning to a row that may have been reconfigured. ``payload`` carries the
    rendered Notification fields so a re-send is exactly what the first send
    would have been even if the printer/client was since renamed or removed.
    """

    __tablename__ = "notification_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), default=None, index=True
    )
    # Active-channel name as returned by active_channels() (the dispatch key).
    channel_key: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[DeliveryStatus] = mapped_column(
        _enum(DeliveryStatus), default=DeliveryStatus.pending, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    # When this delivery is next eligible for a (re)send. NULL == due now.
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    # Frozen Notification fields (title/body/severity + context labels) so a
    # retry reproduces the original message regardless of later DB changes.
    payload: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Command(Base):
    """Hybrid pull queue: central enqueues, the agent fetches on heartbeat."""

    __tablename__ = "commands"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    type: Mapped[CommandType] = mapped_column(_enum(CommandType))
    payload: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    status: Mapped[CommandStatus] = mapped_column(
        _enum(CommandStatus), default=CommandStatus.pending, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)

    agent: Mapped[Agent] = relationship(back_populates="commands")


class RemoteRequest(Base):
    """One operator-initiated remote-hands action and whatever came back.

    The request row is the durable half; the ``Command`` it spawns is the
    transport. Keeping them apart is what lets the answer outlive the command
    (an operator reads a captured page minutes later), and what gives the
    agent-facing result endpoint something to authorise against: a result is
    accepted only for a request whose ``agent_id`` is the agent posting it.

    ON STORING THE BODY IN THE DATABASE
    -----------------------------------
    Driver packages deliberately live on a volume so ``pg_dump`` stays small,
    and the same question was asked here. The answer is different because the
    shapes are different: a driver archive is tens of megabytes and permanent,
    an EWS page is tens of kilobytes and disposable. It is capped at
    ``remote.MAX_BODY_BYTES`` on both sides of the wire, and a captured body is
    a transient diagnostic -- there is no correctness cost to it being pruned,
    which is exactly what makes a file store the wrong trade here.

    THE BODY IS HOSTILE INPUT AND IS NEVER RENDERED IN OUR ORIGIN
    ------------------------------------------------------------
    It is HTML written by a device on a customer LAN. See
    ``dashboard/remote.py`` for the isolation the one route that serves it
    applies, and what an evil device can and cannot do with it.
    """

    __tablename__ = "remote_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), index=True
    )
    # The agent asked to carry this out. SET NULL rather than CASCADE: deleting
    # an agent must not silently erase the record that somebody restarted a
    # customer's printer through it.
    agent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), default=None, index=True
    )
    command_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("commands.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[RemoteRequestKind] = mapped_column(_enum(RemoteRequestKind))
    status: Mapped[RemoteRequestStatus] = mapped_column(
        _enum(RemoteRequestStatus), default=RemoteRequestStatus.pending, index=True
    )

    # --- what was asked for (validated by central/remote.py before it is stored)
    scheme: Mapped[Optional[str]] = mapped_column(String(8), default=None)
    port: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    path: Mapped[Optional[str]] = mapped_column(String(600), default=None)
    #: WRITE_OPS key for a write; NULL otherwise.
    op: Mapped[Optional[str]] = mapped_column(String(40), default=None)
    #: The value sent to the device. Operator-supplied free text (a location, a
    #: contact) -- never a credential, because no write in the vocabulary takes
    #: one. Recorded so the audit trail and this row agree on what was written.
    op_value: Mapped[Optional[str]] = mapped_column(String(400), default=None)

    # --- who asked
    requested_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    requested_by: Mapped[Optional[str]] = mapped_column(String(120), default=None)

    # --- what came back
    http_status: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    #: The content type the DEVICE declared. Recorded and displayed as metadata;
    #: deliberately never echoed as a response header (see dashboard/remote.py).
    content_type: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    body: Mapped[Optional[str]] = mapped_column(Text, default=None)
    body_bytes: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    #: True when the body was cut off at the cap -- so the UI can say so rather
    #: than showing a truncated page as if it were the whole one.
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    #: A write only reports success when the read-back agrees. NULL for a
    #: restart, which has nothing readable afterwards and says so.
    verified: Mapped[Optional[bool]] = mapped_column(Boolean, default=None)
    error: Mapped[Optional[str]] = mapped_column(String(600), default=None)
    #: Free-shape detail from the agent (redirect Location, probe OID, read-back
    #: value). Diagnostics only; nothing keys off its contents.
    detail: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )

    printer: Mapped[Printer] = relationship()

    __table_args__ = (
        # The panel reads "this printer's requests, newest first" and the rate
        # limiter reads "this printer's most recent request" -- one printer, one
        # ordering. Declared here as well as in the migration because revision
        # 0001 is create_all, so an index that lives only in a migration is
        # absent on every fresh install.
        Index("ix_remote_requests_printer_created", "printer_id", "created_at"),
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    # Null for SSO-only users (no local password). Local users have a hash.
    password_hash: Mapped[Optional[str]] = mapped_column(String(256), default=None)
    email: Mapped[Optional[str]] = mapped_column(String(200), unique=True, default=None)
    auth_provider: Mapped[str] = mapped_column(String(40), default="local")  # local | oidc | scim
    role: Mapped[UserRole] = mapped_column(_enum(UserRole), default=UserRole.tech)
    # Account-active flag (SCIM lifecycle / manual deactivation). A deactivated
    # user is treated as deprovisioned: login is rejected (see the login route
    # and central.deps.current_user) but the row is kept so the audit trail and
    # any historical references survive. This is the enterprise off-boarding
    # gate -- an IdP flips ``active`` to false via SCIM PATCH on termination.
    # ``true()`` rather than "1": a string default renders as DEFAULT '1', which
    # Postgres only accepts because it coerces the literal. Keeping every boolean
    # default dialect-rendered is what makes the rule in
    # tests/test_postgres_bootstrap.py a clean one.
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # SCIM external id: the IdP's stable identifier for this user, echoed back
    # in the SCIM ``externalId`` field so the provisioning system can correlate
    # its record with ours across renames. None for locally-created users.
    scim_external_id: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    # Set when the account's password was generated FOR the person rather than
    # chosen BY them -- today only the first-run bootstrap (central/seed.py),
    # which prints the generated password to the container log where it then
    # stays forever. Until it is cleared the dashboard serves nothing but the
    # change-password screen, which is what retires that logged value. Kept as a
    # column rather than a session flag alone so the requirement survives the
    # operator closing the tab and coming back.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    # For client_readonly users: restrict visibility to this client.
    client_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AppAsset(Base):
    """Operator-uploaded blobs (today: just the dashboard logo).

    Stored in the DB rather than the filesystem so it survives container
    rebuilds without an extra mount, and so it Just Works on both SQLite
    (LargeBinary -> BLOB) and Postgres (BYTEA). Cap individual rows at a few
    hundred KB at the upload route -- this isn't meant for large media.
    """

    __tablename__ = "app_assets"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)  # e.g. "logo"
    content_type: Mapped[str] = mapped_column(String(80))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Operator-action audit trail (who did what, from where, when).

    Append-only; written by ``central.audit.record`` at security-relevant
    boundaries: logins (success + failure), settings changes, user / agent /
    subnet / printer CRUD, approvals, alert actions. ``username`` is
    denormalized so the trail survives user deletion; ``user_id`` is the
    join key while the account still exists.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    username: Mapped[Optional[str]] = mapped_column(String(120), default=None)
    ip: Mapped[Optional[str]] = mapped_column(String(64), default=None)
    # Dotted action slug, e.g. "login", "login.failed", "settings.update",
    # "user.create", "agent.update_queued", "printer.approve".
    action: Mapped[str] = mapped_column(String(80), index=True)
    # Human-readable object reference, e.g. "printer:42 10.4.1.120".
    target: Mapped[Optional[str]] = mapped_column(String(300), default=None)
    detail: Mapped[Optional[str]] = mapped_column(Text, default=None)


class LoginAttempt(Base):
    """One row per FAILED local sign-in, in two independent scopes.

    This is operational state, NOT a second audit trail -- ``audit_log`` already
    records every failure with the attempted username, and this table is pruned.
    They are kept apart deliberately: if the throttle counted audit rows, then
    trimming the audit log (a routine, legitimate act) would unlock every account
    at once, and the mechanism would be hostage to a retention policy that has
    nothing to do with it.

    It lives in the database rather than in process memory because the counter
    has to be shared and durable. api and worker are separate containers, the api
    is scalable to more than one replica, and an attacker who can restart a
    container -- or simply wait for a deploy -- would otherwise get a fresh
    budget every time.

    ``scope`` is "user" (a normalised username) or "ip" (the resolved source
    address, see central/net.py). Rows are never updated, only inserted and
    deleted, so concurrent failures cannot lose a count to a read-modify-write
    race: the count is whatever ``SELECT count(*)`` sees.
    """

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    scope: Mapped[str] = mapped_column(String(8))
    key: Mapped[str] = mapped_column(String(200))

    __table_args__ = (
        # The read path is always (scope, key, ts >= window start).
        Index("ix_login_attempts_scope_key_ts", "scope", "key", "ts"),
        # The prune path is ts alone, across every key.
        Index("ix_login_attempts_ts", "ts"),
    )


class AppSetting(Base):
    """Key/value store for operator-managed runtime settings (edited in the UI).

    Values are JSON; the settings service overlays these on top of env-derived
    defaults so only DATABASE_URL + SECRET_KEY need to live in the environment.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReportRun(Base):
    """Idempotency marker for a scheduled report already sent for a period.

    One row claims "(report_type, period_key) was sent" -- the UNIQUE constraint
    on that pair is what makes the send race-safe: the report path INSERTs this
    marker inside the same transaction as the send decision, so two worker cycles
    racing the same period both try to insert, and exactly one wins. The loser
    catches the IntegrityError and skips the send. This is independent of (and
    redundant with) the worker leader lock -- either alone prevents a double-send.
    """

    __tablename__ = "report_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "weekly" or "monthly".
    report_type: Mapped[str] = mapped_column(String(32))
    # The period this send covers, e.g. an ISO date "2026-06-08" (weekly) or
    # "2026-06" (monthly). One send per (report_type, period_key).
    period_key: Mapped[str] = mapped_column(String(40))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("report_type", "period_key", name="uq_report_run_type_period"),
    )


class WorkerJobRun(Base):
    """Per-job liveness stamp for the background worker (one row per job name).

    Written by ``central.worker.run._run_jobs`` after every job in every cycle
    and read by ``central.health`` (``/readyz``, the worker container's
    healthcheck, the dashboard banner). Without it a dead worker is invisible:
    ``mark_offline_agents`` is itself a job, so nothing marks agents offline and
    the dashboard shows a green fleet over frozen data.

    Keyed by job rather than one global "cycle ran" timestamp because
    ``_run_jobs`` deliberately swallows a single job's exception to keep the rest
    of the cycle alive -- so a global stamp would stay fresh while one job raised
    on every pass. Per job, the wedged job names itself.

    ``expected_interval_seconds`` is the cadence the worker is running with
    (``--interval``), stamped here so the read side can derive each job's
    staleness threshold from real configuration instead of a hardcoded age.

    ``last_error_type`` holds the exception CLASS NAME only. Messages carry DSNs,
    SQL and bound parameters, and this column is rendered in the dashboard; the
    full traceback stays in the worker log.
    """

    __tablename__ = "worker_job_runs"

    job: Mapped[str] = mapped_column(String(80), primary_key=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_error_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_error_type: Mapped[Optional[str]] = mapped_column(String(80), default=None)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    expected_interval_seconds: Mapped[int] = mapped_column(
        Integer, default=60, server_default=text("60")
    )


# --------------------------------------------------------------------------- #
# End users + printer assignment (print management)
#
# This is the identity half of print management: WHO prints, and WHICH printers
# they should have. Collection answers "what is on the network"; this answers
# "who is it for".
# --------------------------------------------------------------------------- #
class DirectorySource(str, enum.Enum):
    """Where an end user or group record came from.

    ``manual`` is not a lesser case -- an operator adding a person by hand is a
    first-class path, and a synced directory must never silently delete or
    overwrite a manually-created record it does not know about.
    """

    manual = "manual"
    entra = "entra"
    google = "google"
    ad = "ad"


class EndUser(Base):
    """A customer's staff member -- the person who prints.

    Deliberately NOT a row in ``users``. That table is dashboard *operators*:
    globally-unique usernames, password hashes, roles, session auth. End users
    are a different population with incompatible rules:

    * They are **tenant-scoped**. Two customers each having a "jsmith" is the
      normal case, so the global uniqueness ``users.username`` enforces would be
      violated on the first day of the second customer.
    * They arrive in the **thousands** from a directory sync, and they do not
      log into this dashboard at all. Folding them in would bloat the table the
      login path scans with rows that can never authenticate.
    * Their lifecycle is owned by an external directory, not by an operator.

    Keeping them separate also keeps the blast radius of a sync bug away from
    the table that governs who can administer the system.

    ``directory_source`` / ``directory_id`` are populated now even though sync
    lands later, so the connectors slot in without a second migration over a
    table that by then holds real customer data.
    """

    __tablename__ = "end_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )

    # Identity as the directory knows it. All optional because the three sources
    # disagree about what is mandatory: Entra and Google are email-centric, while
    # on-prem AD may hand us a sAMAccountName and no mailbox at all. A record
    # with neither is still legitimate (hand-entered) -- it just cannot be
    # matched by a future sync, which is the honest consequence.
    email: Mapped[Optional[str]] = mapped_column(String(320), default=None)
    upn: Mapped[Optional[str]] = mapped_column(String(320), default=None)
    display_name: Mapped[Optional[str]] = mapped_column(String(200), default=None)

    directory_source: Mapped[DirectorySource] = mapped_column(
        _enum(DirectorySource), default=DirectorySource.manual
    )
    # The IdP's immutable object id. Not the email: people get married, and a
    # sync keyed on a mutable attribute renames a person into a stranger.
    directory_id: Mapped[Optional[str]] = mapped_column(String(200), default=None)

    # Deactivated rather than deleted, exactly as the operator `users` table
    # does it: assignment history stays attributable after somebody leaves, and
    # a directory that flips `active` back on restores the same record.
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    client: Mapped[Client] = relationship()
    assignments: Mapped[list[PrinterAssignment]] = relationship(
        back_populates="end_user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Tenant-scoped uniqueness, never global. NULLs do not collide in either
        # backend, so AD users with no mailbox coexist freely.
        UniqueConstraint("client_id", "email", name="uq_end_users_client_email"),
        UniqueConstraint(
            "client_id", "directory_source", "directory_id",
            name="uq_end_users_client_directory",
        ),
        Index("ix_end_users_client_active", "client_id", "active"),
    )


class EndUserGroup(Base):
    """A directory group (or a hand-made one) that printers can be assigned to.

    Group assignment is what makes this manageable at scale: "everyone in
    Accounting gets the 3rd-floor MFP" survives staff turnover, where a hundred
    per-person assignments do not.
    """

    __tablename__ = "end_user_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    directory_source: Mapped[DirectorySource] = mapped_column(
        _enum(DirectorySource), default=DirectorySource.manual
    )
    directory_id: Mapped[Optional[str]] = mapped_column(String(200), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    client: Mapped[Client] = relationship()
    assignments: Mapped[list[PrinterAssignment]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("client_id", "name", name="uq_end_user_groups_client_name"),
        UniqueConstraint(
            "client_id", "directory_source", "directory_id",
            name="uq_end_user_groups_client_directory",
        ),
    )


class EndUserGroupMember(Base):
    """Membership. A plain association table -- no surrogate key, so the
    composite PK is itself the "one row per person per group" guarantee."""

    __tablename__ = "end_user_group_members"

    group_id: Mapped[int] = mapped_column(
        ForeignKey("end_user_groups.id", ondelete="CASCADE"), primary_key=True
    )
    end_user_id: Mapped[int] = mapped_column(
        ForeignKey("end_users.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (Index("ix_end_user_group_members_user", "end_user_id"),)


class WorkstationEnrollKey(Base):
    """A long-lived, client-scoped credential that lets a workstation enroll.

    WHY THIS IS NOT A CLAIM CODE
    ----------------------------
    ``AgentClaimToken`` is single-use, which is right when one agent serves a
    site. A workstation installer is the opposite shape: one MSI runs on
    hundreds of PCs, so a single-use code cannot be baked into it, and minting
    one code per PC is the manual step the per-client MSI exists to remove.

    So this key is deliberately multi-use and long-lived, and the safety comes
    from narrowing what holding it can accomplish:

    * **It can only create.** Redeeming mints a machine and that machine's own
      key. The enroll key itself reads nothing -- no printers, no people, no
      other machines. Every call after enrollment authenticates as the machine.
    * **``client_id`` is fixed at mint time**, never supplied by the redeemer.
      Same rule as ``AgentClaimToken.site_id`` and for the same reason: a bearer
      credential must never let its holder choose a tenant.
    * **A fresh machine resolves to no printers.** Enrolling grants nothing on
      its own; an operator still has to assign something. The blast radius of a
      leaked key is junk rows in one tenant, which ``revoked_at`` stops and the
      audit log records.
    * **Revocation does not break the fleet.** Existing machines authenticate
      with their own keys, so revoking stops *new* enrollments and leaves every
      enrolled PC working -- which is what makes rotating it a routine act
      rather than an outage.

    What it does NOT defend against, stated plainly rather than implied away: a
    holder can enroll a machine and then ask what printers a named person gets,
    which discloses that client's assignments. Any design where a workstation
    asks "who is signed in, what do they print to" has this property. It is
    bounded to the one tenant, needs the key, and is revocable and audited.

    ``key_hash`` stores SHA-256, never the key: a database dump must not yield a
    working enrollment.
    """

    __tablename__ = "workstation_enroll_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    #: Operator-facing name ("Acme MSI, July 2026") so a key can be revoked
    #: without having to work out which installer it went into.
    label: Mapped[str] = mapped_column(String(120), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    #: Set rather than deleted, so the audit trail keeps the row that explains
    #: where a machine came from.
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    #: Refreshed on every successful redemption. An enroll key that has not been
    #: used in a year is a candidate for revocation, and that is only visible if
    #: it is recorded.
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )

    client: Mapped[Client] = relationship()

    @property
    def live(self) -> bool:
        return self.revoked_at is None


class DriverPackage(Base):
    """A vendor driver package an operator uploaded, for printers the Windows
    inbox class driver cannot drive.

    WHAT THIS FEATURE ACTUALLY IS
    -----------------------------
    An operator uploads an archive, and the workstation service unpacks it and
    runs ``pnputil /add-driver`` **as LocalSystem** on every machine that needs
    it. That is code execution across a client's fleet, by design -- it is what
    driver installation *is*. So the controls are not decoration:

    * Upload is admin-only and audited.
    * ``sha256`` is computed on upload and **re-verified on the workstation**
      before anything is unpacked, so a package altered on disk or in transit
      never reaches ``pnputil``.
    * The bytes live outside the database (see ``stored_at``), under a path
      built only from integers -- never from an operator-supplied filename.
    * Extraction on the client refuses entries that escape the target directory,
      because an archive is attacker-shaped input the moment it is a file
      somebody uploaded.

    Windows' own requirement that x64 drivers be signed is a real backstop here,
    but it is Microsoft's control, not ours, and an operator can disable it. It
    is not what this design leans on.

    MATCHING
    --------
    ``model`` is a case-insensitive substring matched against ``printers.model``
    -- SNMP model strings vary ("HL-L2350DW" vs "Brother HL-L2350DW series"), so
    exact equality would fail on the reading the device actually returns. A tag
    must be at least 3 characters, or a short one would match a whole fleet.
    Two packages matching one printer is refused rather than guessed, the same
    discipline as machine adoption: an ambiguous driver bind prints garbage.
    """

    __tablename__ = "driver_packages"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    #: Operator-facing label ("Brother HL-L2350DW, v1.2.3").
    name: Mapped[str] = mapped_column(String(200))
    #: The driver name EXACTLY as Windows knows it, which is what
    #: ``Add-PrinterDriver -Name`` and ``Get-PrinterDriver`` take. It comes from
    #: the INF, not from us, so it is operator-supplied and must match or the
    #: staging step succeeds and the bind then fails.
    driver_name: Mapped[str] = mapped_column(String(255))
    #: Path of the .inf INSIDE the archive, relative to its root. Windows only;
    #: blank on a macOS package, which points at its payload with ``macos_ref``.
    inf_relpath: Mapped[str] = mapped_column(String(500))
    #: Substring matched against printers.model. Blank means "any
    #: driver_required printer in this client that has no better match".
    model: Mapped[str] = mapped_column(String(200), default="")

    #: Which OS this package drives. **Load-bearing for matching, not a label.**
    #: Matching is by model substring, and a client that has both a Windows and a
    #: macOS package for one printer would otherwise produce two equally-specific
    #: matches -- which this code correctly refuses as ambiguous, silently
    #: breaking the Windows staging that used to work. So the platform scopes the
    #: candidate set before specificity is ever compared. Defaults to
    #: ``windows``, which is what every package uploaded before macOS support is.
    platform: Mapped[str] = mapped_column(String(16), default="windows")

    #: macOS only. How this package becomes a usable driver, and the three
    #: differ in what they cost and what they can reach:
    #:
    #: * ``ppd``    -- the archive holds a .ppd (or .ppd.gz); the client extracts
    #:                 it and binds it with ``lpadmin -P``. Adds NO code
    #:                 execution beyond the lpadmin we already run. Sufficient
    #:                 for PostScript devices, which is most office MFPs.
    #: * ``system`` -- no upload at all: the vendor .pkg was pushed by MDM (Jamf,
    #:                 Mosyle, Kandji) and ``macos_ref`` is the absolute path of
    #:                 the PPD it installed. Zero code execution, and the only
    #:                 option that reaches a full vendor driver *with* its
    #:                 filters. Costs an out-of-band step.
    #: * ``pkg``    -- the archive holds a vendor .pkg the client installs with
    #:                 ``installer -pkg``. Self-contained, and the widest blast
    #:                 radius by far: a .pkg runs arbitrary pre/postinstall
    #:                 scripts as root, which is broader than Windows'
    #:                 ``pnputil /add-driver``. Gated behind
    #:                 ``workstation.allow_macos_pkg_install``, default off, so
    #:                 an existing manager permission does not silently widen.
    macos_kind: Mapped[Optional[str]] = mapped_column(String(16), default=None)

    #: macOS only. For ``ppd``/``pkg`` the path INSIDE the archive; for
    #: ``system`` an absolute path on the Mac. Both are operator-typed and both
    #: are validated on the client -- an archive-relative path that escapes its
    #: directory, or a system path outside the directories PPDs legitimately
    #: live in, is refused rather than handed to a root command.
    macos_ref: Mapped[Optional[str]] = mapped_column(String(1000), default=None)

    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer, default=0)
    #: Absolute path on the central host. Derived from ids only, never from the
    #: uploaded filename -- a filename is attacker-controlled and a path built
    #: from one is a directory traversal waiting to happen.
    stored_at: Mapped[str] = mapped_column(String(1000), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    client: Mapped[Client] = relationship()

    __table_args__ = (
        Index("ix_driver_packages_client_model", "client_id", "model"),
        # Matching always filters by platform first, so the index that serves it
        # has to carry the platform -- see `platform` above for why that filter
        # is a correctness requirement and not an optimisation.
        Index(
            "ix_driver_packages_client_platform", "client_id", "platform", "model"
        ),
        CheckConstraint(
            "platform IN ('windows', 'macos')",
            name="ck_driver_packages_platform",
        ),
        # A macOS package must say HOW it becomes a driver, and a Windows one must
        # not pretend to. Shape is what a CHECK can state; whether the ref
        # resolves is the client's job.
        #
        # The explicit IS NOT NULL is load-bearing. `NULL IN ('ppd',...)` is NULL
        # under SQL's three-valued logic, not FALSE, so without it
        # `(platform='macos' AND NULL)` is NULL, the whole OR is NULL, and a CHECK
        # evaluating to NULL **passes** -- verified: a macOS row with no kind was
        # accepted. Same trap as the parity bug in the assignment CHECK.
        CheckConstraint(
            "(platform = 'windows' AND macos_kind IS NULL) OR "
            "(platform = 'macos' AND macos_kind IS NOT NULL AND "
            "macos_kind IN ('ppd', 'pkg', 'system'))",
            name="ck_driver_packages_macos_kind",
        ),
    )


class Machine(Base):
    """A workstation running the client. Tenant-scoped, like everything else.

    IDENTITY IS A GUID THE CLIENT MINTS, NOT THE COMPUTER NAME
    ----------------------------------------------------------
    ``machine_uid`` is generated once by the client and persisted under
    ProgramData. Everything else on offer breaks on something ordinary:

    * **Computer name** is reused constantly, is not unique across clients, and
      changes on rename -- so a renamed PC silently becomes a new machine while
      a recycled name silently inherits another machine's printers.
    * **Machine SID** survives a rename but not a re-image, and cloned VM images
      that skipped sysprep share one, which would merge two machines into a row.

    The GUID survives renames, IP changes and domain moves. It does *not*
    survive a re-image, and that is the deliberate part: a freshly imaged PC is
    a new machine, comes back with no assignments, and leaves its old row for an
    operator to retire. Silently inheriting a departed user's printers because a
    name matched is the worse failure. If auto-adopting a re-imaged PC by name
    is wanted later, ``name`` is already stored and the adoption would be
    tenant-scoped -- no migration needed to add it.

    ``name`` is a display label, never a key. It is what an operator recognises
    in a list, and it is refreshed on every check-in.
    """

    __tablename__ = "machines"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    #: Client-minted GUID. Unique per tenant rather than globally: two customers
    #: cannot collide in practice, but scoping it means a restored backup from
    #: one tenant can never adopt another's machine.
    machine_uid: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255), default="")

    #: SHA-256 of this machine's own API key, minted at enrollment and shown to
    #: nobody -- the workstation stores it, central stores only the hash, exactly
    #: as ``Agent.api_key_hash`` does. Nullable because an operator can create a
    #: machine row in the UI before the PC exists; it gets a key when it enrolls.
    api_key_hash: Mapped[Optional[str]] = mapped_column(String(128), default=None)

    #: "Whoever is at this machine gets its default, whatever their groups say."
    #: Off by default: the agreed precedence is direct user > machine > group,
    #: which is right for an ordinary desk. A shared floor terminal or kiosk is
    #: the exception, and it is a property of the machine rather than a global
    #: setting because one site can have both.
    default_wins: Mapped[bool] = mapped_column(Boolean, default=False)

    #: Deactivate rather than delete, exactly as end_users do -- a retired PC's
    #: assignment history is worth keeping, and a machine that resolves to
    #: nothing stops provisioning immediately.
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    #: ``windows`` / ``macos``, as reported by the client. For the UI only.
    #:
    #: The driver decision deliberately does NOT read this column: it uses the
    #: platform the requesting client states on the request itself. A stored
    #: value can be stale -- a machine re-imaged from Windows to macOS keeps its
    #: row through adoption -- and a stale platform here would hand a Mac a
    #: Windows driver package. Nullable because a client older than macOS support
    #: never reports one; absent means "not yet known", never "windows".
    platform: Mapped[Optional[str]] = mapped_column(String(16), default=None)

    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    client: Mapped[Client] = relationship()

    __table_args__ = (
        UniqueConstraint("client_id", "machine_uid", name="uq_machines_client_uid"),
        Index("ix_machines_client_active", "client_id", "active"),
    )


class PrinterAssignment(Base):
    """"This printer belongs to this person (or this group)."

    Targets exactly one of ``end_user_id`` / ``group_id``, enforced by a CHECK
    rather than by convention -- a row that targets both would have no defined
    meaning, and a row that targets neither is an orphan the UI would render as
    a blank. Both mistakes are cheap to make from a form handler and expensive
    to find later.

    **Tenancy is NOT expressible here.** The invariant that matters -- the
    printer and the target belong to the same client -- spans three tables, so a
    CHECK cannot state it. It is enforced in ``services.assign_printer`` and
    covered by tests that specifically try to cross tenants. Denormalising
    ``client_id`` onto this row would let a database constraint carry it, but it
    would then drift the moment a printer is moved between clients, trading a
    checked invariant for a silently stale one.
    """

    __tablename__ = "printer_assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), index=True
    )
    end_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("end_users.id", ondelete="CASCADE"), default=None, index=True
    )
    group_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("end_user_groups.id", ondelete="CASCADE"), default=None, index=True
    )
    #: "Every printer on this machine, whoever is signed in" -- shared floor
    #: terminals, kiosks, the PC by the warehouse door. Distinct from a user
    #: assignment because the printer belongs to the location, not the person.
    machine_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("machines.id", ondelete="CASCADE"), default=None, index=True
    )

    # "Make this the workstation's default printer." Conflicts are inevitable
    # once groups are involved, so this is a request, not a guarantee --
    # services.effective_printers_for resolves it deterministically.
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Which operator made the assignment. SET NULL rather than CASCADE: deleting
    # a departed admin must not silently delete every printer assignment they
    # ever made.
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    printer: Mapped[Printer] = relationship()
    end_user: Mapped[Optional[EndUser]] = relationship(back_populates="assignments")
    group: Mapped[Optional[EndUserGroup]] = relationship(back_populates="assignments")

    __table_args__ = (
        # Exactly one target of three. Stated as a constraint rather than a
        # convention because a row targeting two has no defined meaning and a row
        # targeting none is an orphan the UI renders as a blank -- both cheap to
        # produce from a form handler and expensive to find later. Written as a
        # count rather than the old `<>` pair because that idiom does not extend
        # past two columns.
        CheckConstraint(
            "((CASE WHEN end_user_id IS NULL THEN 0 ELSE 1 END) + "
            "(CASE WHEN group_id IS NULL THEN 0 ELSE 1 END) + "
            "(CASE WHEN machine_id IS NULL THEN 0 ELSE 1 END)) = 1",
            name="ck_printer_assignments_one_target",
        ),
        UniqueConstraint(
            "printer_id", "end_user_id", name="uq_printer_assignments_printer_user"
        ),
        UniqueConstraint(
            "printer_id", "group_id", name="uq_printer_assignments_printer_group"
        ),
        UniqueConstraint(
            "printer_id", "machine_id", name="uq_printer_assignments_printer_machine"
        ),
    )


class DirectoryConnection(Base):
    """One configured directory sync for one client.

    Per-client, not global: an MSP's customers each have their own tenant, and a
    single global connection would either serve one customer or need a mapping
    table that is this row by another name.

    Secrets live in ``secret`` (Fernet-encrypted via central.secrets), never in
    ``config``. Splitting them is not decoration -- ``config`` is rendered in the
    UI, echoed in audit detail and dumped in diagnostics, and the only reliable
    way to keep a client secret out of all three is for it to live somewhere
    those paths never read. One secret column covers every provider: Entra's
    client secret, Google's service-account JSON, AD's bind password.
    """

    __tablename__ = "directory_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    # Reuses DirectorySource so a synced end_user's provenance and its
    # connection's provider are the same vocabulary. `manual` is excluded by
    # CHECK: it is a real provenance for a person, but not a thing you can
    # connect to.
    provider: Mapped[DirectorySource] = mapped_column(_enum(DirectorySource))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Non-secret provider settings (tenant id, domain, base DN, server, filters).
    config: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    # The single credential, encrypted at rest. Nullable so a connection can be
    # created and its secret supplied separately.
    secret: Mapped[Optional[str]] = mapped_column(Text, default=None)

    # Last-run bookkeeping. `last_error` holds a sanitised message: provider
    # errors quote request URLs and occasionally echo credentials back, and this
    # column is rendered on the settings page.
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_ok: Mapped[Optional[bool]] = mapped_column(Boolean, default=None)
    last_error: Mapped[Optional[str]] = mapped_column(String(500), default=None)
    last_result: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    client: Mapped[Client] = relationship()

    __table_args__ = (
        # One connection per provider per client. Two Entra connections for the
        # same customer would race each other, each deactivating the users the
        # other just created.
        UniqueConstraint("client_id", "provider", name="uq_directory_conn_client_provider"),
        CheckConstraint("provider <> 'manual'", name="ck_directory_conn_not_manual"),
    )


# --------------------------------------------------------------------------- #
# Billing: cost-per-page rate cards
# --------------------------------------------------------------------------- #
class MeterClass(str, enum.Enum):
    """The two things a cost-per-page contract prices separately."""

    mono = "mono"
    color = "color"


class UnsplitPolicy(str, enum.Enum):
    """What to do with pages from a device that reports no mono/colour split.

    This is a **commercial decision an operator has to make**, which is why it
    is a stored column rather than an inference. A mono laser genuinely has no
    colour meter, so "colour = 0" is right for it and catastrophically wrong for
    an MFP whose colour meter simply failed to decode.

    ``exclude`` (the default) prices nothing it cannot classify: those pages are
    reported on the invoice as unbilled, with the reason, so the gap is visible
    rather than absorbed. ``bill_as_mono`` is the operator saying "the devices in
    this fleet that report no split are mono devices, bill them at the mono
    rate" -- an explicit, audited choice attached to the rate card.

    It applies **only** when a device reports neither meter. A device reporting
    mono but not colour is not covered by it: the pages it did not classify are
    by definition not the mono ones, so calling them mono would be a different
    and worse guess.
    """

    exclude = "exclude"
    bill_as_mono = "bill_as_mono"


class BillingRateCard(Base):
    """One client's cost-per-page contract terms.

    Per client, like everything else commercial here. The **active** card is the
    one invoices are built from, and at most one card per client may be active
    (partial unique index below) -- otherwise "which card produced this invoice"
    has no answer, and two operators reading the same invoice would each be able
    to point at a different set of rates.

    Superseded cards are kept, inactive, rather than deleted: they are the terms
    a previous period was billed under.
    """

    __tablename__ = "billing_rate_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    # ISO 4217 alpha-3, validated on save. Held per card rather than globally
    # because an MSP with customers either side of a border bills each in its
    # own currency; nothing here converts between them, and the invoice states
    # which one it is in rather than assuming the reader knows.
    currency: Mapped[str] = mapped_column(String(3), default="USD")

    # Base cost per page. Also the rate for every page above the last volume
    # band, which is what removes the need for an "unbounded tier" row and the
    # question of what happens when somebody forgets to add one.
    mono_rate: Mapped[Decimal] = mapped_column(Rate())
    color_rate: Mapped[Decimal] = mapped_column(Rate())

    # Optional monthly minimum commitment. When the metered work comes to less
    # than this, the invoice carries an explicit adjustment line -- never a
    # silently inflated total.
    minimum_charge: Mapped[Optional[Decimal]] = mapped_column(Money(), default=None)

    unsplit_policy: Mapped[UnsplitPolicy] = mapped_column(
        _enum(UnsplitPolicy), default=UnsplitPolicy.exclude
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    client: Mapped[Client] = relationship()
    tiers: Mapped[list[BillingRateTier]] = relationship(
        back_populates="rate_card",
        cascade="all, delete-orphan",
        order_by="BillingRateTier.up_to",
    )

    __table_args__ = (
        UniqueConstraint("client_id", "name", name="uq_rate_card_client_name"),
        # At most one active card per client. Partial, so any number of retired
        # cards can coexist -- the same shape as the printer serial index.
        Index(
            "uq_rate_card_client_active",
            "client_id",
            unique=True,
            postgresql_where=text("active"),
            sqlite_where=text("active"),
        ),
    )


class BillingRateTier(Base):
    """One volume band of a rate card, for one meter class.

    Bands are **graduated (marginal)**, not cliff-edged: a band covers the pages
    between the previous band's ceiling and its own, and only those. Pages above
    the highest band fall back to the card's base rate.

    Graduated rather than "whole volume at the rate its total qualifies for"
    because the latter is non-monotonic -- printing one more page can make the
    bill go *down* -- and an invoice nobody can explain is worse than one that is
    slightly less generous.
    """

    __tablename__ = "billing_rate_tiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    rate_card_id: Mapped[int] = mapped_column(
        ForeignKey("billing_rate_cards.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[MeterClass] = mapped_column(_enum(MeterClass))
    #: Inclusive ceiling of this band, in pages.
    up_to: Mapped[int] = mapped_column(Integer)
    rate: Mapped[Decimal] = mapped_column(Rate())

    rate_card: Mapped[BillingRateCard] = relationship(back_populates="tiers")

    __table_args__ = (
        UniqueConstraint("rate_card_id", "kind", "up_to", name="uq_rate_tier_card_kind_upto"),
        CheckConstraint("up_to > 0", name="ck_rate_tier_up_to_positive"),
    )
    # No CHECK on `rate`: fixed-point columns are stored as text on SQLite (see
    # central.money), where SQLite's type ordering makes `rate >= 0` true for
    # every text value. A constraint that is real on one backend and vacuous on
    # the other is worse than none -- it reads as enforcement. Non-negativity is
    # enforced at the single point that parses operator input
    # (`money.parse_rate`) and again in the type's bind path, which raises rather
    # than storing a value that would mis-sort.
# Outbound event bus
#
# The integration surface that replaces the dropped PSA work: typed, versioned,
# HMAC-signed events an MSP's own systems consume. Three tables, because the
# three things have genuinely different lifetimes -- a subscription is
# configuration, an event is a fact that happened once, and a delivery is one
# attempt to tell one subscriber about it.
# --------------------------------------------------------------------------- #
class EventSubscription(Base):
    """One outbound destination for typed events, scoped to a tenant or global.

    **Scope is the security property, not a filter.** ``client_id`` NULL means
    global (the MSP's own systems); a non-NULL ``client_id`` means this
    destination belongs to that customer and must never be handed another
    customer's events. Like ``PrinterAssignment`` the rule spans tables -- an
    event's tenancy lives on ``outbound_events.client_id`` -- so no CHECK can
    state it; it is owned by ``events.emit.scope_allows`` and re-checked at send
    time, because an operator may re-scope a subscription while deliveries for
    other tenants are still queued against it.

    ``secret`` is the HMAC signing key, Fernet-encrypted at rest exactly as
    ``directory_connections.secret`` is, and for the same reason: it is never
    rendered in the UI, never echoed in audit detail and never dumped in
    diagnostics. It is generated server-side and shown once, never typed by an
    operator and never read back -- an operator who loses it rotates it.

    ``event_types`` NULL/empty means "every type in the catalogue". Naming types
    explicitly is the normal case for a partner who only cares about supplies.
    """

    __tablename__ = "event_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    #: NULL == global. Non-NULL scopes every delivery to that one tenant.
    client_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), default=None, index=True
    )
    url: Mapped[str] = mapped_column(String(500))
    #: Fernet ciphertext (enc:v1:...). Never plaintext, never rendered.
    secret: Mapped[str] = mapped_column(Text)
    #: Subset of the catalogue this destination wants; NULL/empty == all.
    event_types: Mapped[Optional[list]] = mapped_column(JSON, default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Operational bookkeeping, rendered on the subscriptions page. `last_error`
    # is deliberately short and sanitised: transport errors quote URLs and
    # occasionally echo credentials, and this column is displayed.
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_ok: Mapped[Optional[bool]] = mapped_column(Boolean, default=None)
    last_error: Mapped[Optional[str]] = mapped_column(String(500), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    client: Mapped[Optional[Client]] = relationship()


class OutboundEvent(Base):
    """One thing that happened, frozen for delivery to every interested subscriber.

    Separate from the deliveries so a single occurrence fans out to N
    destinations without N copies of the payload drifting apart, and so a replay
    to a newly-added subscriber sends byte-identical data.

    ``idempotency_key`` is the de-duplication contract on BOTH sides. It is
    stable for one logical occurrence (``alert.opened:alert:412``), UNIQUE here
    so a re-run of a worker cycle cannot emit the same fact twice, and shipped in
    the payload and a header so a consumer can detect a replay without keeping
    state about our retries.

    ``uid`` is per-event and travels as the event id; a retry re-sends the *same*
    uid, which is what makes "have I seen this before?" answerable downstream.
    """

    __tablename__ = "outbound_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Public event id ("evt_<hex>"), stable across retries.
    uid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    #: Schema version of ``data`` for this type. Bumped only on a breaking change.
    version: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    #: NULL means the event has no single tenant (fleet-wide). Client-scoped
    #: subscriptions receive NOTHING with a NULL client_id -- an event of unknown
    #: tenancy is not "yours".
    client_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), default=None, index=True
    )
    #: The per-type ``data`` block, already normalised (device strings bounded
    #: and stripped of control characters). JSON, so quoting is json.dumps's job.
    data: Mapped[Optional[dict]] = mapped_column(JSON, default=None)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # The prune sweep and the subscriptions page both read newest-first.
        Index("ix_outbound_events_created_at", "created_at"),
    )


class EventDelivery(Base):
    """One (event, subscription) send attempt -- the retry/dead-letter log.

    Deliberately shaped like ``NotificationDelivery``: same ``DeliveryStatus``
    vocabulary, same ``attempts`` / ``last_error`` / ``next_attempt_at`` triple,
    so ``channels.delivery._apply_result`` and ``backoff_delay`` fold an outcome
    into it unchanged. There is exactly one backoff policy and one dead-letter
    rule in this codebase, and a second implementation is the one that would
    drift.
    """

    __tablename__ = "event_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("outbound_events.id", ondelete="CASCADE"), index=True
    )
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("event_subscriptions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        _enum(DeliveryStatus), default=DeliveryStatus.pending, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, default=None)
    #: NULL == due now, matching NotificationDelivery.
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    #: HTTP status of the last attempt, when there was one.
    response_status: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    event: Mapped[OutboundEvent] = relationship()
    subscription: Mapped[EventSubscription] = relationship()

    __table_args__ = (
        # One row per destination per event. Without it a re-emit (or a manual
        # replay) would double-deliver, which is precisely what the idempotency
        # key exists to make unnecessary.
        UniqueConstraint(
            "event_id", "subscription_id", name="uq_event_delivery_event_subscription"
        ),
    )


class DeviceDefinition(Base):
    """A server-pushed device/model definition: how to read one printer family's
    private MIB, expressed as data an agent interprets rather than code it runs.

    WHY THIS TABLE EXISTS
    ---------------------
    Today a printer model whose supply levels only live in a vendor-private OID
    needs a new *provider* -- Python, in the agent package, shipped by a release
    and rolled out to every site. That makes "we bought a different Brother" an
    engineering task. A definition moves the model-specific part into a row an
    operator can add, and the agent fetches it.

    WHAT IS SAFE ABOUT IT
    ---------------------
    ``spec`` holds the normalised output of
    :func:`central.device_definitions.validate_definition`, never an operator's
    raw text. That validator is the security boundary and it is deliberately
    narrow: a closed vocabulary of six decoders, numeric-only OIDs, no regular
    expressions at all, unknown keys refused, and every list/string/depth
    bounded. The agent re-runs the identical validator on receipt and on every
    load of its local cache, because a signature proves who produced bytes, not
    that they are safe.

    SCOPE
    -----
    ``client_id`` is NULL for the normal case: a definition describes *hardware*,
    which is not tenant data, and re-uploading the same Brother definition per
    customer is how an MSP stops maintaining them. A non-NULL ``client_id``
    scopes a definition to one customer, and an agent is served only the global
    set plus the clients it actually collects for -- so one tenant's custom
    definition never reaches another's site.

    PRECEDENCE (also enforced agent-side; stated here because it is the rule an
    operator is deciding about when they tick the box)
    -----------------------------------------------------------------------
    Built-in providers run FIRST and a definition runs LAST, filling only what
    is still missing. A definition cannot silently replace a value a
    hardware-proven provider produced -- that is how a working printer stops
    working. ``override_builtin`` lets an operator overrule that deliberately;
    it defaults off, it is shown in the UI, it is audited, and the agent records
    per-field in ``provider_trace`` that it happened. "Never silently" is the
    contract, not "never".
    """

    __tablename__ = "device_definitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Stable slug. This -- not the row id -- is what the agent keys on, so a
    #: definition can be exported, re-imported or restored and still be the
    #: same definition to every agent holding a cache.
    key: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(200))
    #: NULL = every client (hardware knowledge). Set = scoped to one tenant.
    client_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), default=None, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: The validated, normalised definition. Never the operator's raw text.
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Bumped on every content change. Operator-facing ("has this been edited
    #: since the incident?"); the agent's change detection uses the feed digest,
    #: which cannot drift from the content the way a hand-bumped number can.
    revision: Mapped[int] = mapped_column(Integer, default=1)
    notes: Mapped[Optional[str]] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    client: Mapped[Optional[Client]] = relationship()

    __table_args__ = (
        # One definition per key per scope. Two rows with the same key would
        # both be served, and the agent -- which dedupes by key -- would take
        # whichever arrived last: a coin flip over what a fleet reads.
        UniqueConstraint("key", "client_id", name="uq_device_definitions_key_scope"),
        # ...and that constraint does NOT cover the common case, which is the
        # trap worth writing down: SQL treats NULLs as distinct in a UNIQUE, so
        # the constraint above permits any number of *global* rows sharing a
        # key -- exactly the rows every agent receives. A partial unique index
        # is what actually enforces it, and both dialects this project runs on
        # support one.
        Index(
            "uq_device_definitions_global_key",
            "key",
            unique=True,
            sqlite_where=text("client_id IS NULL"),
            postgresql_where=text("client_id IS NULL"),
        ),
        Index("ix_device_definitions_enabled", "enabled"),
    )
