"""How old is the data this page is showing?

WHY THIS EXISTS
---------------
Every fleet-state page in this dashboard was a snapshot with no expiry printed
on it. A NOC screen opened at 08:00 renders a green fleet, and at 13:00 it is
still rendering that same green fleet -- authoritatively, with nothing on the
page admitting that no reading has arrived since breakfast. The stalled-worker
banner (``central.health.worker_banner``) covers one cause of that; it does not
cover a dead agent, a severed site link, or a fleet nobody has polled. This
module answers the general question, and every fleet surface renders the answer.

FRESHNESS MEANS THE DATA, NEVER THE RENDER
------------------------------------------
The tempting implementation -- stamp ``datetime.now()`` into the footer at
render time -- is *worse than printing nothing*. It asserts currency the data
does not have: a page built this second from readings four hours old would say
"as of now" and be believed. So every number here is derived from when a
reading actually arrived (``printers.last_seen``), when an agent last checked in
(``agents.last_heartbeat``) and whether the worker is completing its jobs. A page
rendered this second over four-hour-old readings reports **four hours**.

The second half of that promise is client-side and lives in
``static/dashboard.js``: a *relative* age rendered once by the server ("2m ago")
becomes a lie the moment the tab is left open, so the browser re-derives it from
the absolute timestamp. If polling dies, the age keeps climbing rather than
freezing on a comforting number -- the honest failure mode.

WHY ``newest`` IS NOT THE WHOLE ANSWER
--------------------------------------
``MAX(last_seen)`` is the age of the freshest thing on the page, and on its own
it is optimistic to the point of dishonesty: one printer reporting normally
while 199 have been silent for hours would read "12s ago". So the indicator
always carries coverage alongside age -- how many devices are behind, and how
many have never reported at all. ``state`` folds both together:

  ``live``     something arrived inside the grace, and every device is inside it
  ``lagging``  recent data exists, but some devices/agents are behind
  ``stale``    even the NEWEST reading is past the grace -- the page is frozen
  ``none``     nothing has ever reported; the page reflects no device state

THE THRESHOLD IS THE OPERATOR'S OWN
-----------------------------------
"Behind" is ``alerts.printer_offline_minutes`` and ``alerts.offline_grace_seconds``
-- the same two settings ``worker.jobs.mark_offline_printers`` and
``mark_offline_agents`` use to decide the very same question. A second, private
constant here would drift from the alerting and produce a dashboard that calls a
printer current while the worker is opening an offline alert against it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from central import models as m
from central.health import STATE_NEVER_RAN, STATE_STALE, worker_health

# Freshness states, worst-last. Rendered as a tone and a sentence; also emitted
# as ``data-pn-state`` so the browser can announce a *transition* without
# announcing every poll.
STATE_LIVE = "live"
STATE_LAGGING = "lagging"
STATE_STALE_DATA = "stale"
STATE_NO_DATA = "none"

#: Tone name understood by the ``note()`` macro in _components.html.
STATE_TONE = {
    STATE_LIVE: "muted",
    STATE_LAGGING: "warning",
    STATE_STALE_DATA: "error",
    STATE_NO_DATA: "muted",
}

# Fallbacks matching the defaults of the settings they read, so a database with
# no app_settings rows behaves identically to one carrying the shipped values.
DEFAULT_PRINTER_GRACE_MINUTES = 30
DEFAULT_AGENT_GRACE_SECONDS = 300

# Browser auto-refresh cadence.
#
# The floor is the load control. This endpoint is cheap (three aggregates over
# indexed columns), but "cheap" multiplied by twenty NOC tabs at a 2s interval
# is a self-inflicted load test against the same database the worker and the
# paginated fleet queries are using. 15s is well under any human's tolerance for
# a staleness readout and two orders of magnitude off the default SNMP poll
# interval (300s), so a faster setting could not surface newer data anyway -- it
# would only re-ask the same question.
MIN_REFRESH_SECONDS = 15
MAX_REFRESH_SECONDS = 3600
DEFAULT_REFRESH_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite hands back naive datetimes; treat them as UTC (as health.py does)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def humanize_age(seconds: Optional[float]) -> str:
    """A compact age: ``42s`` / ``7m`` / ``3h 20m`` / ``2d 4h``.

    Mirrored byte-for-byte by ``pnAge()`` in static/dashboard.js, which takes
    over once the page is live. Any change here must be made there too, or the
    label will visibly jump the first time the browser re-derives it.
    """
    if seconds is None:
        return "unknown"
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"
    days, remainder = divmod(total, 86400)
    hours = remainder // 3600
    return f"{days}d" if hours == 0 else f"{days}d {hours}h"


@dataclass(frozen=True)
class Freshness:
    """The age of the fleet data behind one page, and how complete it is.

    Every field is a measurement, not a rendering decision -- the template asks
    this object questions and never recomputes an age of its own.
    """

    now: datetime
    client_id: Optional[int]
    scope_label: str
    printers_total: int
    printers_reporting: int
    printers_stale: int
    newest_reading_at: Optional[datetime]
    printer_grace_seconds: int
    agents_total: int
    agents_stale: int
    newest_heartbeat_at: Optional[datetime]
    agent_grace_seconds: int
    worker_stalled: bool
    # True only for a viewer who is never shown the stalled-worker banner (the
    # tenant-facing portal). Staff get the banner, which says the same thing
    # with the job names; repeating it here would be duplication, and putting
    # job names in front of a customer would be a leak.
    warn_worker: bool

    # -- derived ----------------------------------------------------------- #
    @property
    def printers_never(self) -> int:
        """Approved but never yet polled. Absence of data, not stale data."""
        return max(0, self.printers_total - self.printers_reporting)

    @property
    def age_seconds(self) -> Optional[float]:
        if self.newest_reading_at is None:
            return None
        return max(0.0, (self.now - self.newest_reading_at).total_seconds())

    @property
    def state(self) -> str:
        if self.printers_total == 0 or self.newest_reading_at is None:
            return STATE_NO_DATA
        age = self.age_seconds
        if age is not None and age > self.printer_grace_seconds:
            # Even the freshest reading is past the grace: nothing on the page
            # is current, whatever the per-device counts say.
            return STATE_STALE_DATA
        if self.printers_stale or self.printers_never or self.agents_stale or self.warn_worker:
            return STATE_LAGGING
        return STATE_LIVE

    @property
    def tone(self) -> str:
        return STATE_TONE.get(self.state, "muted")

    @property
    def age_text(self) -> str:
        return humanize_age(self.age_seconds)

    @property
    def iso(self) -> str:
        """Absolute instant for the browser to re-derive the relative age from."""
        return self.newest_reading_at.isoformat() if self.newest_reading_at else ""

    @property
    def absolute_text(self) -> str:
        if self.newest_reading_at is None:
            return "never"
        return self.newest_reading_at.strftime("%Y-%m-%d %H:%M UTC")

    @property
    def headline(self) -> str:
        """The one sentence. Says what is true of the DATA, not of the render.

        Deliberately free of the relative age even in the stale case: the age is
        rendered once, by the ``<time>`` element the browser keeps ticking. Two
        copies of it would disagree the moment the first tick landed.
        """
        if self.printers_total == 0:
            return "No printers in this view yet."
        if self.newest_reading_at is None:
            return "No printer has ever reported. Nothing here reflects live device state."
        return f"Data as of {self.absolute_text}"

    @property
    def warning(self) -> str:
        """The blunt sentence, only when the whole view is frozen. Else empty."""
        if self.state != STATE_STALE_DATA:
            return ""
        return (
            "Nothing on this page is current — no reading has arrived for longer "
            f"than the {humanize_age(self.printer_grace_seconds)} offline grace."
        )

    @property
    def coverage(self) -> str:
        """Who is behind. Empty when everything reported inside the grace."""
        parts = []
        grace = humanize_age(self.printer_grace_seconds)
        if self.printers_stale:
            parts.append(
                f"{self.printers_stale} of {self.printers_total} printers "
                f"silent for over {grace}"
            )
        if self.printers_never:
            parts.append(f"{self.printers_never} never reported")
        if self.agents_stale:
            parts.append(
                f"{self.agents_stale} of {self.agents_total} agents not checking in"
            )
        return "; ".join(parts)

    @property
    def announcement(self) -> str:
        """Bucketed sentence for assistive tech.

        Deliberately carries no ticking number: it is written into a live region
        only when ``state`` changes, so a screen reader hears "fleet data has
        gone stale" once rather than "four hours ago... four hours ago..." every
        minute. See static/dashboard.js.
        """
        scope = self.scope_label
        if self.state == STATE_NO_DATA:
            return f"{scope}: no printer data yet."
        if self.state == STATE_STALE_DATA:
            return f"{scope}: data is out of date, nothing on this page is current."
        if self.state == STATE_LAGGING:
            return f"{scope}: data is current but some devices are not reporting."
        return f"{scope}: data is current."

    def as_dict(self) -> Dict[str, Any]:
        """Flat view for tests and for anything that wants the numbers."""
        return {
            "state": self.state,
            "age_seconds": self.age_seconds,
            "age_text": self.age_text,
            "newest_reading_at": self.newest_reading_at,
            "printers_total": self.printers_total,
            "printers_stale": self.printers_stale,
            "printers_never": self.printers_never,
            "agents_stale": self.agents_stale,
            "worker_stalled": self.worker_stalled,
        }


def _grace_seconds(db: Session) -> "tuple":
    """(printer grace, agent grace) from the operator's alerting settings."""
    try:
        from central.runtime import load_settings

        rt = load_settings(db)
        printer_minutes = int(
            rt.get("alerts.printer_offline_minutes", DEFAULT_PRINTER_GRACE_MINUTES)
            or DEFAULT_PRINTER_GRACE_MINUTES
        )
        agent_seconds = int(
            rt.get("alerts.offline_grace_seconds", DEFAULT_AGENT_GRACE_SECONDS)
            or DEFAULT_AGENT_GRACE_SECONDS
        )
    except Exception:  # noqa: BLE001 - a settings read must never 500 a page
        printer_minutes, agent_seconds = (
            DEFAULT_PRINTER_GRACE_MINUTES,
            DEFAULT_AGENT_GRACE_SECONDS,
        )
    return max(1, printer_minutes) * 60, max(1, agent_seconds)


def refresh_seconds(db: Session) -> int:
    """The browser poll cadence, clamped. 0 means auto-refresh is off.

    Clamped rather than validated-and-rejected: this value reaches an
    ``hx-trigger`` on every dashboard page, and a typo in a settings box must
    degrade to a sane cadence rather than either 500 the dashboard or let one
    operator point twenty tabs at a 1-second interval.
    """
    try:
        from central.runtime import load_settings

        rt = load_settings(db)
        if not rt.get("dashboard.autorefresh_enabled", True):
            return 0
        raw = int(rt.get("dashboard.autorefresh_seconds", DEFAULT_REFRESH_SECONDS))
    except Exception:  # noqa: BLE001
        return DEFAULT_REFRESH_SECONDS
    return max(MIN_REFRESH_SECONDS, min(MAX_REFRESH_SECONDS, raw))


def fleet_freshness(
    db: Session,
    client_id: Optional[int] = None,
    scope_label: str = "Fleet",
    user: Any = None,
    now: Optional[datetime] = None,
) -> Optional[Freshness]:
    """Measure how old the fleet data in ``client_id``'s scope is.

    Three aggregates, all one row each -- this is polled by every open dashboard
    tab, so it must never grow a per-printer loop.

    Returns ``None`` if the read fails. A freshness indicator that 500s the page
    it is supposed to qualify would be a spectacular own goal; the pages simply
    render without it, exactly as they did before.
    """
    now = now or _now()
    try:
        printer_grace, agent_grace = _grace_seconds(db)
        printer_cutoff = now - timedelta(seconds=printer_grace)
        agent_cutoff = now - timedelta(seconds=agent_grace)

        # Approved devices only, matching mark_offline_printers: a pending or
        # ignored device is not part of the fleet and its silence means nothing.
        stmt = select(
            func.count(),
            func.count(m.Printer.last_seen),
            func.max(m.Printer.last_seen),
            func.sum(case((m.Printer.last_seen < printer_cutoff, 1), else_=0)),
        ).where(m.Printer.discovery_state == m.DiscoveryState.approved)
        if client_id is not None:
            stmt = stmt.where(m.Printer.client_id == client_id)
        total, reporting, newest, stale = db.execute(stmt).one()

        astmt = select(
            func.count(),
            func.max(m.Agent.last_heartbeat),
            func.sum(case((m.Agent.last_heartbeat < agent_cutoff, 1), else_=0)),
        )
        if client_id is not None:
            # An agent is scoped by the client of the site it belongs to. It can
            # legitimately collect for another client's subnets (bridged-at-HQ),
            # but its own site is what places it in a tenant's view.
            astmt = astmt.join(m.Site, m.Site.id == m.Agent.site_id).where(
                m.Site.client_id == client_id
            )
        agents_total, newest_hb, agents_stale = db.execute(astmt).one()
        # An agent that has NEVER checked in is counted as behind: it is not
        # collecting, and "never" is not better than "late".
        agents_never = 0
        if agents_total:
            nstmt = select(func.count()).where(m.Agent.last_heartbeat.is_(None))
            if client_id is not None:
                nstmt = nstmt.select_from(m.Agent).join(
                    m.Site, m.Site.id == m.Agent.site_id
                ).where(m.Site.client_id == client_id)
            else:
                nstmt = nstmt.select_from(m.Agent)
            agents_never = db.scalar(nstmt) or 0

        health = worker_health(db)
        worker_stalled = health["state"] in (STATE_STALE, STATE_NEVER_RAN)
        role = getattr(getattr(user, "role", None), "value", None)
        return Freshness(
            now=now,
            client_id=client_id,
            scope_label=scope_label,
            printers_total=int(total or 0),
            printers_reporting=int(reporting or 0),
            printers_stale=int(stale or 0),
            newest_reading_at=_aware(newest),
            printer_grace_seconds=printer_grace,
            agents_total=int(agents_total or 0),
            agents_stale=int(agents_stale or 0) + int(agents_never or 0),
            newest_heartbeat_at=_aware(newest_hb),
            agent_grace_seconds=agent_grace,
            worker_stalled=worker_stalled,
            warn_worker=worker_stalled and role == "client_readonly",
        )
    except Exception:  # noqa: BLE001 - never break the page it annotates
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
