"""Remote hands: the policy for reaching a device's own management surface.

This is the only feature in the program that makes central act on a device
rather than merely observe one, so every decision it embodies is written down
here rather than distributed across routes.

THE SHAPE, AND WHY IT IS TWO CHANNELS RATHER THAN ONE
-----------------------------------------------------
An operator wants two different things and they have wildly different blast
radii, so they are built as two separate channels and never merged:

* **Read** -- "show me what the printer's own web page says". A single HTTP
  ``GET`` to the device's embedded web server, executed by the site agent (the
  only thing with a route to it), captured, and rendered back in isolation.
  This is the baseline and it works with no capability detection at all.
* **Write** -- "set this device's location" / "restart it". NOT a proxied POST
  to the device's web UI. A closed vocabulary of named operations, each one an
  SNMP SET of a specific standard OID, gated on a recorded capability probe.

Proxying arbitrary POSTs would have been less code and is the obvious reading of
"EWS proxy with writes". It is refused because it makes us a general-purpose
request forger aimed at a device on a customer LAN, with no way to state what a
given request will do, no way to audit it meaningfully ("POST /form.cgi" tells a
reviewer nothing), and no way to detect capability -- you cannot probe for "can
I POST" without performing the very write you were trying to test. A closed
vocabulary answers all three: every write is a named operation with a known
effect, the audit row says which, and capability is provable by a write that
changes nothing.

**Therefore the proxy is read-only by construction.** A write never travels
through it. That sentence is the security property; everything below enforces it.

CAPABILITY IS PROBED, NEVER INFERRED
------------------------------------
``Printer.remote_capability`` is ``unknown`` until a probe says otherwise, and
``unknown`` behaves exactly like ``read_only``. Brand, model and firmware are
never consulted -- HP's Secure-by-Default (FutureSmart 4.5+) leaves SNMP reads
working and turns SNMP **writes** off, so on a hardened fleet the read-only
answer is the common one and a brand table would be confidently wrong on the
majority of devices.

The probe is a **write that changes nothing**: read ``sysLocation``, then SET it
to the value it already holds. A device that accepts it is writable with the
credentials we hold; a device that answers ``noAccess`` / ``notWritable`` /
``authorizationError``, or does not answer at all, is read-only. Nothing on the
device differs afterwards either way, which is what makes it safe to run on a
production printer during business hours.

Two honesty points that must survive any later edit:

* It proves **"writable with the credentials we hold"**, not "writable". A
  device with a separate write community that we were never given is genuinely
  read-only *to us*, and reporting that as read-only is correct rather than
  pessimistic.
* It proves the device **accepted** the SET, not that it **applied** it. Real
  writes therefore read back and compare (see ``WRITE_OPS``); the probe cannot,
  because the value it writes is the value that was already there.

WHAT IS DELIBERATELY NOT IN THE WRITE VOCABULARY
------------------------------------------------
``prtGeneralReset`` has four values; we offer exactly one. ``powerCycleReset(4)``
is a restart and is recoverable. ``resetToNRC(5)`` and especially
``resetToDefaults(6)`` wipe a device's configuration -- including its network
settings, which is how a remote action strands a printer that then has to be
walked to. There is no operator flag for those: they are simply not expressible.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone
from typing import Dict, List, NamedTuple, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from central import models as m
from central.events.destinations import classify, normalise_address

#: Hard cap on a captured response body, applied by the agent to the raw bytes
#: as they arrive and re-applied by central when the result is posted. An EWS
#: status page is a few tens of KB; anything approaching this is a device
#: behaving oddly or a deliberate attempt to fill the database.
MAX_BODY_BYTES = 256 * 1024

#: Longest device path an operator may request. Long enough for any real EWS
#: query string, short enough that the audit row stays readable.
MAX_PATH_LEN = 512

#: Ports an EWS is plausibly on. An allowlist rather than a range because the
#: point of this control is that the operator cannot aim the agent at an
#: arbitrary service -- "80, 443 and the three everyone actually uses" keeps the
#: feature usable without reopening that.
ALLOWED_PORTS = (80, 443, 8000, 8080, 8443)

#: RFC 3986 unreserved + sub-delims + pchar extras, plus the query separator.
#: Deliberately excludes '#' (a fragment is never transmitted, so accepting one
#: would only invite an operator to believe it means something), backslash, and
#: every control character and space.
_PATH_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "-._~:/?[]@!$&'()*+,;=%"
)

#: sysLocation, then sysContact. Both are RFC 1213 read-write scalars present on
#: essentially every SNMP-capable device, so the probe does not depend on the
#: Printer MIB being implemented well.
PROBE_OIDS = ("1.3.6.1.2.1.1.6.0", "1.3.6.1.2.1.1.4.0")

#: How long a queued request may sit before it is called expired rather than
#: pending. An agent heartbeats every few minutes; a request still unclaimed
#: after this either has no agent behind it or the agent is gone, and either way
#: an operator staring at "pending" forever is being told something untrue.
REQUEST_TTL_SECONDS = 900


class WriteOp(NamedTuple):
    """One named write. ``value_kind`` decides what the operator may supply."""

    key: str
    label: str
    oid: str
    snmp_type: str          # "string" | "int"
    #: "text" -- operator supplies a string, bounded by ``max_len``.
    #: "fixed" -- operator supplies nothing; ``fixed_value`` is sent.
    value_kind: str
    max_len: int = 0
    fixed_value: Optional[str] = None
    #: True when the written value can be read straight back and compared. A
    #: restart cannot be: the device is rebooting, so there is nothing to read
    #: and claiming verification would be a lie the UI then repeats.
    verifiable: bool = True
    help: str = ""


WRITE_OPS: Dict[str, WriteOp] = {
    op.key: op
    for op in (
        WriteOp(
            key="set_location",
            label="Set location",
            oid="1.3.6.1.2.1.1.6.0",
            snmp_type="string",
            value_kind="text",
            max_len=200,
            help="Writes sysLocation. Shows on the device's own panel and web page.",
        ),
        WriteOp(
            key="set_contact",
            label="Set contact",
            oid="1.3.6.1.2.1.1.4.0",
            snmp_type="string",
            value_kind="text",
            max_len=200,
            help="Writes sysContact -- who to call about this device.",
        ),
        WriteOp(
            key="set_name",
            label="Set device name",
            oid="1.3.6.1.2.1.1.5.0",
            snmp_type="string",
            value_kind="text",
            max_len=200,
            help="Writes sysName. Some devices derive their hostname from this.",
        ),
        WriteOp(
            key="restart",
            label="Restart printer",
            # prtGeneralReset.1 -- powerCycleReset(4). See the module docstring
            # for why 5 and 6 are not offered and cannot be reached from here.
            oid="1.3.6.1.2.1.43.5.1.1.3.1",
            snmp_type="int",
            value_kind="fixed",
            fixed_value="4",
            verifiable=False,
            help="Power-cycles the device (prtGeneralReset). Queued jobs are lost.",
        ),
    )
}


class RemoteError(ValueError):
    """A remote-hands request this server refuses to make.

    Its own type so a route renders it as a form error and the agent-facing
    result path records it as a stated reason, without either string-matching a
    bare ValueError.
    """


# --------------------------------------------------------------------------- #
# Address + path validation (the SSRF boundary)
# --------------------------------------------------------------------------- #
def validate_device_address(ip: str) -> str:
    """Refuse any address the agent must not be pointed at; return it normalised.

    Composed from ``events.destinations.classify`` rather than re-derived, so
    the never-override set (unspecified, link-local -- i.e. cloud instance
    metadata -- multicast, IETF-reserved) has exactly one definition in this
    codebase. Two differences from an event destination, both deliberate:

    * ``allow_private=True``. A printer is *expected* to be on RFC 1918 space;
      refusing private addresses would refuse the entire feature.
    * Loopback is refused **here** even though ``classify`` permits it. For an
      event subscription loopback is a legitimate operator choice; for a printer
      it is not an address a printer can have, and it names the agent's own
      host -- which is the one machine a remote-hands request must never be
      able to aim at. That extra question is asked of ``normalise_address``'s
      output rather than of the raw literal, and this is not cosmetic:
      ``::ffff:127.0.0.1`` has ``is_loopback`` False as an ``IPv6Address``, so
      an un-normalised check reads a loopback address as ordinary -- caught by
      a test, having shipped past the first reading of this function.

    The value never comes from a request: it is read from ``printers.ip``, which
    the discovery path wrote. This function is the second gate, not the first.
    """
    raw = (ip or "").strip()
    if not raw:
        raise RemoteError("this printer has no address recorded")
    try:
        addr = normalise_address(ipaddress.ip_address(raw))
    except ValueError:
        # Deliberately no name resolution anywhere on this path: a hostname
        # would reintroduce the resolve-then-connect gap that destinations.py
        # documents, and a printer row always carries a literal.
        raise RemoteError("%s is not an IP address; remote hands never resolves names" % raw)
    if addr.is_loopback:
        raise RemoteError("%s is a loopback address -- that is the agent host, not a printer" % raw)
    reason = classify(str(addr), allow_private=True)
    if reason:
        raise RemoteError("refusing to reach %s" % reason)
    return str(addr)


def validate_path(path: str) -> str:
    """Normalise and bound the device path. Returns a path, never a URL."""
    raw = (path or "").strip()
    if not raw:
        return "/"
    if not raw.startswith("/"):
        raise RemoteError("the path must start with '/' -- a full URL is not accepted")
    if raw.startswith("//"):
        # "//evil.example" is a protocol-relative URL: joined to a base it
        # changes the HOST, which is the one thing the operator must not control.
        raise RemoteError("a path starting '//' would change the host; refused")
    if len(raw) > MAX_PATH_LEN:
        raise RemoteError("the path is longer than %d characters" % MAX_PATH_LEN)
    bad = sorted({c for c in raw if c not in _PATH_CHARS})
    if bad:
        raise RemoteError(
            "the path contains characters that are not allowed here: %s"
            % " ".join(repr(c) for c in bad)
        )
    if any(seg == ".." for seg in raw.split("/")):
        raise RemoteError("'..' is not allowed in the path")
    return raw


def validate_port(port) -> int:
    try:
        value = int(port)
    except (TypeError, ValueError):
        raise RemoteError("port must be a number")
    if value not in ALLOWED_PORTS:
        raise RemoteError(
            "port %d is not one of the embedded-web-server ports this proxy will "
            "reach (%s)" % (value, ", ".join(str(p) for p in ALLOWED_PORTS))
        )
    return value


def validate_scheme(scheme: str) -> str:
    value = (scheme or "").strip().lower()
    if value not in ("http", "https"):
        raise RemoteError("scheme must be http or https")
    return value


# --------------------------------------------------------------------------- #
# Write vocabulary
# --------------------------------------------------------------------------- #
def validate_write(op_key: str, value: Optional[str]) -> Tuple[WriteOp, str]:
    """Resolve a named write and the exact value that will go on the wire.

    A ``fixed`` operation ignores whatever the caller sent -- the value is a
    server constant, the same reasoning as ``update_agent`` resolving its
    ``pip_source`` from settings rather than from the request.
    """
    op = WRITE_OPS.get((op_key or "").strip())
    if op is None:
        raise RemoteError("unknown write operation")
    if op.value_kind == "fixed":
        return op, str(op.fixed_value)
    text = (value or "").strip()
    if not text:
        raise RemoteError("%s needs a value" % op.label)
    if len(text) > op.max_len:
        raise RemoteError("value is longer than %d characters" % op.max_len)
    # Control characters are refused rather than stripped. This string is sent
    # to a device, read back off it on the next poll, and rendered in the
    # dashboard and in alerts -- a value we would have to sanitise on the way
    # back is a value we should not have written on the way out.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise RemoteError("value contains control characters")
    return op, text


# --------------------------------------------------------------------------- #
# Rate limiting + lifecycle
# --------------------------------------------------------------------------- #
def expire_stale(db: Session, printer_id: int, *, now: Optional[datetime] = None) -> int:
    """Mark this printer's un-answered requests expired. Bounded to one printer.

    Deliberately not a worker job. The sweep only has to happen where the answer
    is read (the panel) or where staleness changes a decision (the rate limit),
    both of which are already per-printer, so a whole extra scheduled job over
    the whole table would buy nothing.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=REQUEST_TTL_SECONDS)
    stale = list(db.scalars(
        select(m.RemoteRequest).where(
            m.RemoteRequest.printer_id == printer_id,
            m.RemoteRequest.status.in_(
                (m.RemoteRequestStatus.pending, m.RemoteRequestStatus.sent)
            ),
            m.RemoteRequest.created_at < cutoff,
        )
    ))
    for row in stale:
        row.status = m.RemoteRequestStatus.expired
        row.error = "the agent did not pick this up within %d seconds" % REQUEST_TTL_SECONDS
        row.completed_at = now
    return len(stale)


def display_status(req: "m.RemoteRequest", *, now: Optional[datetime] = None) -> str:
    """What to SHOW for a request, without writing anything.

    The panel is a GET, and a read path that writes is both a surprise and a
    commit per page view -- the same rule ``machines.platform`` is written by
    check-in rather than by the assignments read. So staleness is derived here
    and only ever persisted on a POST, where ``expire_stale`` runs inside a
    transaction that was going to commit anyway.
    """
    if req.status not in (m.RemoteRequestStatus.pending, m.RemoteRequestStatus.sent):
        return req.status.value
    now = now or datetime.now(timezone.utc)
    created = req.created_at
    if created is None:
        return req.status.value
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if (now - created).total_seconds() > REQUEST_TTL_SECONDS:
        return m.RemoteRequestStatus.expired.value
    return req.status.value


def open_requests(db: Session, printer_id: int) -> List["m.RemoteRequest"]:
    return list(db.scalars(
        select(m.RemoteRequest).where(
            m.RemoteRequest.printer_id == printer_id,
            m.RemoteRequest.status.in_(
                (m.RemoteRequestStatus.pending, m.RemoteRequestStatus.sent)
            ),
        )
    ))


def check_rate_limit(
    db: Session,
    printer: "m.Printer",
    settings: dict,
    *,
    now: Optional[datetime] = None,
) -> None:
    """Raise ``RemoteError`` if this printer is being asked for too often.

    Two limits, because they stop different things. The per-printer interval
    stops a stuck refresh or an impatient operator turning the proxy into a
    scanner aimed at one device. The outstanding cap stops a queue being filled
    faster than an agent drains it -- commands are pulled, so an unbounded
    backlog is work the agent will eventually do, minutes after anybody wanted
    it, on a device that has since been walked to.
    """
    now = now or datetime.now(timezone.utc)
    expire_stale(db, printer.id, now=now)

    outstanding = open_requests(db, printer.id)
    max_open = int(settings.get("remote.max_open_per_printer") or 0)
    if max_open and len(outstanding) >= max_open:
        raise RemoteError(
            "there are already %d remote request(s) queued for this printer; wait "
            "for the agent to answer them" % len(outstanding)
        )

    interval = int(settings.get("remote.min_interval_seconds") or 0)
    if interval:
        last = db.scalar(
            select(m.RemoteRequest)
            .where(m.RemoteRequest.printer_id == printer.id)
            .order_by(m.RemoteRequest.created_at.desc())
            .limit(1)
        )
        if last is not None:
            created = last.created_at
            if created.tzinfo is None:      # SQLite hands back naive datetimes
                created = created.replace(tzinfo=timezone.utc)
            waited = (now - created).total_seconds()
            if waited < interval:
                raise RemoteError(
                    "remote requests for one printer are limited to one every %d "
                    "seconds; try again in %d" % (interval, int(interval - waited) + 1)
                )


def writes_allowed(printer: "m.Printer", settings: dict) -> Optional[str]:
    """Why a write is refused for this printer, or None if it may proceed.

    Three independent gates, and the order is the order an operator should read
    them in. Note the asymmetry that runs through this whole feature: an
    operator may pin a device **read-only** and may not pin one **writable**.
    Pinning read-only can only ever narrow what we do; a "yes it is writable"
    override would be exactly the inference-instead-of-evidence this design
    exists to refuse, and the device would answer the SET anyway if it were true.
    """
    if not settings.get("remote.allow_writes"):
        return (
            "remote writes are switched off for this installation "
            "(Settings -> Agents -> Remote hands)"
        )
    if printer.remote_write_disabled:
        return "writes are pinned off for this printer by an operator"
    if printer.remote_capability != m.RemoteCapability.writable:
        return (
            "this device has not been proven writable -- run the capability check "
            "first; until it passes the device is read-only"
        )
    return None


def agent_for_printer(db: Session, printer: "m.Printer") -> Optional[int]:
    """Which agent should carry this out. Same rule as an on-demand poll.

    The discoverer if we know it, otherwise any agent at the printer's site.
    Kept identical to ``printer_poll_now`` on purpose: two different answers to
    "who can reach this printer" is a bug waiting for a multi-agent site.
    """
    if printer.discovered_by_agent_id is not None:
        return printer.discovered_by_agent_id
    agent = db.scalar(select(m.Agent).where(m.Agent.site_id == printer.site_id))
    return agent.id if agent is not None else None
