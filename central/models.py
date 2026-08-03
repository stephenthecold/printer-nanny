"""SQLAlchemy ORM models for the Printer Nanny central server.

Hierarchy: Client -> Site -> (Subnet, Agent, Printer). Printers carry Supplies and
time-series Readings; PrinterEvents capture errors/status; Maintenance and Alert
tables track service and notifications. Enums are stored as VARCHAR
(native_enum=False) so the same models work on SQLite and Postgres.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
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
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from central.db import Base


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


class CommandStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    done = "done"


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
    subnets: Mapped[list[Subnet]] = relationship(back_populates="agent")
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
    # Discovery status (updated by the ingest endpoint on each /discovered batch).
    last_discovery_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_discovery_found_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    last_discovery_new_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    site: Mapped[Site] = relationship(back_populates="subnets")
    agent: Mapped[Optional[Agent]] = relationship(back_populates="subnets")

    __table_args__ = (UniqueConstraint("site_id", "cidr", name="uq_subnet_site_cidr"),)


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
    color: Mapped[Optional[str]] = mapped_column(String(40), default=None)  # black/cyan/magenta/yellow
    description: Mapped[Optional[str]] = mapped_column(String(200), default=None)
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
    forecast_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    printer: Mapped[Printer] = relationship(back_populates="supplies")

    __table_args__ = (
        UniqueConstraint("printer_id", "type", "color", name="uq_supply_printer_type_color"),
    )


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
