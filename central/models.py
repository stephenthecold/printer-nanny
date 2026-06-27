"""SQLAlchemy ORM models for the Printer Nanny central server.

Hierarchy: Client -> Site -> (Subnet, Agent, Printer). Printers carry Supplies and
time-series Readings; PrinterEvents capture errors/status; Maintenance and Alert
tables track service and notifications. Enums are stored as VARCHAR
(native_enum=False) so the same models work on SQLite and Postgres.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
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


class AlertConditionType(str, enum.Enum):
    supply_below = "supply_below"          # threshold = percent
    error_severity = "error_severity"      # threshold mapped to EventSeverity rank
    offline_minutes = "offline_minutes"    # threshold = minutes offline
    maintenance_due = "maintenance_due"    # no threshold
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
    ``delivered`` -- the channel reported success; terminal.
    ``failed``   -- last attempt failed but more retries remain (still due at
                    ``next_attempt_at``); functionally a retryable ``pending``.
    ``dead``     -- exhausted the max-attempts cap; terminal, dead-lettered.
    """

    pending = "pending"
    delivered = "delivered"
    failed = "failed"
    dead = "dead"


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

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

    __table_args__ = (UniqueConstraint("site_id", "ip", name="uq_printer_site_ip"),)


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
    """Append-only time-series. Postgres partitions this monthly (see migration)."""

    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(
        ForeignKey("printers.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    page_count: Mapped[Optional[int]] = mapped_column(Integer, default=None)
    status: Mapped[PrinterStatus] = mapped_column(_enum(PrinterStatus), default=PrinterStatus.unknown)
    supply_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, default=None)

    printer: Mapped[Printer] = relationship(back_populates="readings")


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
    next_due: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    auth_provider: Mapped[str] = mapped_column(String(40), default="local")  # local | oidc
    role: Mapped[UserRole] = mapped_column(_enum(UserRole), default=UserRole.tech)
    # For client_readonly users: restrict visibility to this client.
    client_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
