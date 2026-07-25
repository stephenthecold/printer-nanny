"""Quiet hours, maintenance windows, and the digest that releases held alerts.

Before this, alert suppression did not exist at all (``docs/audit-2026-07.md``:
*"none exist (exhaustive grep). This is the loudest complaint in the field."*).
Every alert notified immediately, at any hour, with no way to say "not overnight"
or "we're replacing the fleet on Saturday".

The tests are grouped by the thing most likely to be wrong:

* **Wall-clock correctness.** A window is local to the CLIENT, not the server, and
  the server here is UTC. A test that only ever uses UTC would pass with the
  timezone handling deleted, so every recurring case below runs in a zone with a
  real offset.
* **Midnight wrap.** 18:00->07:00 is the common case and the one where the naive
  implementation breaks: at 03:00 the window belongs to *yesterday's* start day,
  so testing the weekday mask against today silently un-silences the small hours.
* **The deferral instant.** It is what the retry sweeper wakes on, so it must
  always be in the future and always bounded.
* **Honest bookkeeping.** A held notification must not look sent, must not look
  un-notified, and must not stack a new row per cycle.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from central import models as m
from central import suppression
from central.channels.base import ChannelResult, Notification, NotificationChannel
from central.channels.delivery import WITHHELD_CHANNEL_KEY, flush_deferred
from central.runtime import load_settings
from central.worker import jobs

NY = "America/New_York"


class _Recorder(NotificationChannel):
    """Records what it was asked to send. No network."""

    type = "recorder"

    def __init__(self, name="Email"):
        super().__init__(name, config={}, runtime={})
        self.sent: list = []

    def send(self, note: Notification) -> ChannelResult:
        self.sent.append(note)
        return ChannelResult(ok=True, detail="sent")


def _fleet(db, *, tz=None, level=5, threshold=10):
    """One approved printer at a low level + a supply_below rule that fires."""
    client = m.Client(name="Acme", timezone=tz)
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.5",
        model="HP M404", display_name="Front desk",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    supply = m.Supply(
        printer_id=printer.id, type=m.SupplyType.toner, color="black", level_pct=level
    )
    db.add(supply)
    db.add(m.AlertRule(
        name="low", condition_type=m.AlertConditionType.supply_below,
        threshold=threshold, severity=m.EventSeverity.warning,
    ))
    db.commit()
    return client, printer, supply


def _window(db, **kw):
    kw.setdefault("name", "Overnight")
    kw.setdefault("kind", m.SuppressionKind.quiet_hours)
    kw.setdefault("action", m.SuppressionAction.defer)
    kw.setdefault("scope", m.AlertScope.global_)
    kw.setdefault("min_severity_breakthrough", m.EventSeverity.critical)
    kw.setdefault("allow_breakthrough", True)
    window = m.SuppressionWindow(**kw)
    db.add(window)
    db.commit()
    return window


def _set(db, **kv):
    for key, value in kv.items():
        key = key.replace("__", ".")
        row = db.query(m.AppSetting).filter_by(key=key).one_or_none()
        if row is None:
            db.add(m.AppSetting(key=key, value=str(value)))
        else:
            row.value = str(value)
    db.commit()


def _hm(hour, minute=0):
    return hour * 60 + minute


# --------------------------------------------------------------------------- #
# Wall-clock correctness: the window is the CLIENT's clock, not the server's.
# --------------------------------------------------------------------------- #
def test_quiet_hours_use_the_client_timezone_not_utc(db):
    """An 18:00-07:00 New York window, probed at instants that FLIP between zones.

    Both times are chosen so the answer differs depending on which clock is used.
    An earlier version of this test used 23:00 UTC, which is inside 18:00-07:00
    whether you read it as UTC or as New York -- so it passed with the timezone
    handling replaced by ``timezone.utc`` and proved nothing.
    """
    _client, printer, _supply = _fleet(db, tz=NY)
    _window(db, start_minute=_hm(18), end_minute=_hm(7))
    rt = load_settings(db)

    # 09:00 UTC == 04:00 EST: deep inside the NY window, but OUTSIDE 18:00-07:00
    # read as UTC. Evaluating in UTC would wrongly dispatch.
    small_hours_ny = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
    assert suppression.evaluate(db, printer, "warning", small_hours_ny, runtime=rt).defer

    # 20:00 UTC == 15:00 EST: the middle of the NY working day, but INSIDE
    # 18:00-07:00 read as UTC. Evaluating in UTC would wrongly hold it.
    workday_ny = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
    assert suppression.evaluate(db, printer, "warning", workday_ny, runtime=rt).dispatch


def test_a_client_without_a_timezone_falls_back_to_the_global_default(db):
    _client, printer, _supply = _fleet(db, tz=None)
    _set(db, alerts__default_timezone=NY)
    _window(db, start_minute=_hm(18), end_minute=_hm(7))
    rt = load_settings(db)

    # Same flipping instants as above, so this proves the NY default was applied
    # rather than the server's UTC.
    at = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)     # 04:00 EST
    assert suppression.evaluate(db, printer, "warning", at, runtime=rt).defer
    workday = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)  # 15:00 EST
    assert suppression.evaluate(db, printer, "warning", workday, runtime=rt).dispatch


def test_an_unresolvable_timezone_degrades_to_utc_instead_of_raising(db):
    """A bad zone string in one client row must not take the worker down."""
    _client, printer, _supply = _fleet(db, tz="Not/AZone")
    _window(db, start_minute=_hm(18), end_minute=_hm(7))
    rt = load_settings(db)

    # 09:00 UTC is 04:00 in New York (inside the window) but 09:00 in UTC
    # (outside it). Asserting *dispatch* therefore proves two things at once: the
    # evaluator returned a Decision rather than raising, and the fallback really
    # was UTC rather than the unresolvable zone somehow still applying.
    at = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)
    decision = suppression.evaluate(db, printer, "warning", at, runtime=rt)
    assert decision.dispatch
    assert suppression.valid_timezone("Not/AZone") is False
    assert suppression.valid_timezone(NY) is True
    assert suppression.valid_timezone("") is True  # blank == inherit


def test_deferral_end_is_the_clients_local_morning_in_utc(db):
    """A 07:00 EST end is 12:00 UTC, and the instant must be in the future."""
    _client, printer, _supply = _fleet(db, tz=NY)
    _window(db, start_minute=_hm(18), end_minute=_hm(7))
    rt = load_settings(db)

    at = datetime(2026, 1, 15, 23, 0, tzinfo=timezone.utc)  # 18:00 EST Thu
    decision = suppression.evaluate(db, printer, "warning", at, runtime=rt)
    assert decision.defer
    ends = decision.deferred_until
    assert ends > at
    # 07:00 EST on the 16th == 12:00 UTC.
    assert (ends.year, ends.month, ends.day, ends.hour) == (2026, 1, 16, 12)


def test_window_end_survives_a_dst_transition(db):
    """Across US spring-forward, 07:00 local is 11:00 UTC not 12:00.

    Nothing here asserts a specific DST policy for the gap hour -- only that the
    offset actually shifts, which a naive fixed-offset implementation would miss.
    """
    _client, printer, _supply = _fleet(db, tz=NY)
    _window(db, start_minute=_hm(18), end_minute=_hm(7))
    rt = load_settings(db)

    # US DST 2026 starts Sun 8 March. Late on the 8th, EDT (UTC-4) is in effect.
    at = datetime(2026, 3, 8, 23, 0, tzinfo=timezone.utc)  # 19:00 EDT
    decision = suppression.evaluate(db, printer, "warning", at, runtime=rt)
    assert decision.defer
    assert decision.deferred_until.hour == 11, decision.deferred_until
    assert decision.deferred_until > at


# --------------------------------------------------------------------------- #
# Midnight wrap -- and the weekday mask gating the START day.
# --------------------------------------------------------------------------- #
def test_wrapped_window_is_active_on_both_sides_of_midnight(db):
    _client, printer, _supply = _fleet(db)  # UTC client
    _window(db, start_minute=_hm(18), end_minute=_hm(7))
    rt = load_settings(db)

    for hour in (18, 20, 23, 0, 3, 6):
        at = datetime(2026, 1, 15, hour, 30, tzinfo=timezone.utc)
        assert suppression.evaluate(db, printer, "warning", at, runtime=rt).defer, hour
    for hour in (7, 12, 17):
        at = datetime(2026, 1, 15, hour, 30, tzinfo=timezone.utc)
        assert suppression.evaluate(db, printer, "warning", at, runtime=rt).dispatch, hour


def test_weekday_mask_gates_the_windows_start_day_not_the_current_day(db):
    """A Fri-night window is still active at 03:00 Saturday.

    This is the wrap bug: Saturday is NOT in the mask, but 03:00 Saturday is
    Friday's window running out. Checking the mask against 'today' would end the
    window at local midnight and un-silence exactly the hours it exists for.
    """
    _client, printer, _supply = _fleet(db)
    _window(db, start_minute=_hm(18), end_minute=_hm(7), weekdays=[4])  # Friday only
    rt = load_settings(db)

    friday_night = datetime(2026, 1, 16, 22, 0, tzinfo=timezone.utc)   # Fri
    saturday_small_hours = datetime(2026, 1, 17, 3, 0, tzinfo=timezone.utc)  # Sat
    saturday_night = datetime(2026, 1, 17, 22, 0, tzinfo=timezone.utc)  # Sat

    assert friday_night.weekday() == 4 and saturday_small_hours.weekday() == 5
    assert suppression.evaluate(db, printer, "warning", friday_night, runtime=rt).defer
    assert suppression.evaluate(
        db, printer, "warning", saturday_small_hours, runtime=rt
    ).defer, "Friday's window must still cover Saturday 03:00"
    # Saturday evening is NOT Friday, so the window does not start again.
    assert suppression.evaluate(db, printer, "warning", saturday_night, runtime=rt).dispatch


def test_same_day_window_does_not_wrap(db):
    _client, printer, _supply = _fleet(db)
    _window(db, name="Working hours", start_minute=_hm(9), end_minute=_hm(17))
    rt = load_settings(db)

    inside = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    before = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)
    after = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    assert suppression.evaluate(db, printer, "warning", inside, runtime=rt).defer
    assert suppression.evaluate(db, printer, "warning", before, runtime=rt).dispatch
    assert suppression.evaluate(db, printer, "warning", after, runtime=rt).dispatch


def test_empty_weekday_mask_means_every_day(db):
    _client, printer, _supply = _fleet(db)
    _window(db, start_minute=_hm(18), end_minute=_hm(7), weekdays=None)
    rt = load_settings(db)
    for day in range(15, 22):  # a full week
        at = datetime(2026, 1, day, 22, 0, tzinfo=timezone.utc)
        assert suppression.evaluate(db, printer, "warning", at, runtime=rt).defer, day


def test_corrupt_weekday_mask_does_not_raise_or_silently_disable(db):
    """A hand-edited mask must fail loud-ish (treated as every day), not vanish."""
    _client, printer, _supply = _fleet(db)
    _window(db, start_minute=_hm(18), end_minute=_hm(7), weekdays=["nonsense"])
    rt = load_settings(db)
    at = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)
    assert suppression.evaluate(db, printer, "warning", at, runtime=rt).defer


# --------------------------------------------------------------------------- #
# Break-through, scope, and overlap resolution.
# --------------------------------------------------------------------------- #
def test_critical_breaks_through_by_default(db):
    _client, printer, _supply = _fleet(db)
    _window(db, start_minute=_hm(18), end_minute=_hm(7))
    rt = load_settings(db)
    at = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)

    assert suppression.evaluate(db, printer, "warning", at, runtime=rt).defer
    assert suppression.evaluate(db, printer, "critical", at, runtime=rt).dispatch


def test_total_silence_holds_even_a_critical(db):
    """Total silence must be expressible.

    ``critical`` is the top of EventSeverity, so a floor alone can only ever
    LOOSEN a window -- there is no value that holds a critical. The separate
    allow_breakthrough flag is what makes a deliberately fully-silent window
    possible, and it defaults to True so nothing is silent by accident.
    """
    _client, printer, _supply = _fleet(db)
    _window(db, start_minute=_hm(18), end_minute=_hm(7),
            allow_breakthrough=False)
    rt = load_settings(db)
    at = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)

    for severity in ("info", "warning", "critical"):
        assert suppression.evaluate(db, printer, severity, at, runtime=rt).defer, severity


def test_info_floor_means_the_window_never_silences_anything(db):
    """The other end of the range: a floor of info lets everything through."""
    _client, printer, _supply = _fleet(db)
    _window(db, start_minute=_hm(18), end_minute=_hm(7),
            min_severity_breakthrough=m.EventSeverity.info)
    rt = load_settings(db)
    at = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)

    for severity in ("info", "warning", "critical"):
        assert suppression.evaluate(db, printer, severity, at, runtime=rt).dispatch, severity


def test_break_through_floor_is_per_window(db):
    _client, printer, _supply = _fleet(db)
    _window(db, start_minute=_hm(18), end_minute=_hm(7),
            min_severity_breakthrough=m.EventSeverity.warning)
    rt = load_settings(db)
    at = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)

    # Floor lowered to warning: a warning now breaks through too.
    assert suppression.evaluate(db, printer, "warning", at, runtime=rt).dispatch
    assert suppression.evaluate(db, printer, "info", at, runtime=rt).defer


def test_scope_limits_which_printers_a_window_covers(db):
    client, printer, _supply = _fleet(db)
    other = m.Client(name="Other")
    db.add(other)
    db.flush()
    other_site = m.Site(client_id=other.id, name="Site B")
    db.add(other_site)
    db.flush()
    other_printer = m.Printer(
        client_id=other.id, site_id=other_site.id, ip="10.0.9.9",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(other_printer)
    db.commit()

    _window(db, start_minute=_hm(18), end_minute=_hm(7),
            scope=m.AlertScope.client, scope_id=client.id)
    rt = load_settings(db)
    at = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)

    assert suppression.evaluate(db, printer, "warning", at, runtime=rt).defer
    assert suppression.evaluate(db, other_printer, "warning", at, runtime=rt).dispatch


def test_a_scoped_window_does_not_silence_agent_level_alerts(db):
    """No printer means no client, so a client-scoped window can't apply."""
    client, _printer, _supply = _fleet(db)
    _window(db, start_minute=_hm(18), end_minute=_hm(7),
            scope=m.AlertScope.client, scope_id=client.id)
    rt = load_settings(db)
    at = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)
    assert suppression.evaluate(db, None, "warning", at, runtime=rt).dispatch


def test_global_window_still_applies_to_an_agent_level_alert(db):
    _client, _printer, _supply = _fleet(db)
    _window(db, start_minute=_hm(18), end_minute=_hm(7))
    rt = load_settings(db)
    at = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)
    assert suppression.evaluate(db, None, "warning", at, runtime=rt).defer


def test_suppress_beats_defer_when_windows_overlap(db):
    _client, printer, _supply = _fleet(db)
    _window(db, name="Quiet", start_minute=_hm(18), end_minute=_hm(7),
            action=m.SuppressionAction.defer)
    _window(db, name="Planned work", kind=m.SuppressionKind.maintenance,
            action=m.SuppressionAction.suppress,
            starts_at=datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 1, 16, 2, 0, tzinfo=timezone.utc))
    rt = load_settings(db)
    at = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)

    decision = suppression.evaluate(db, printer, "warning", at, runtime=rt)
    assert decision.suppress and not decision.defer
    assert "Planned work" in decision.reason


def test_overlapping_defers_hold_until_the_latest_end(db):
    _client, printer, _supply = _fleet(db)
    _window(db, name="Short", start_minute=_hm(18), end_minute=_hm(23))
    _window(db, name="Long", start_minute=_hm(18), end_minute=_hm(7))
    rt = load_settings(db)
    at = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)

    decision = suppression.evaluate(db, printer, "warning", at, runtime=rt)
    assert decision.defer
    # 07:00 the next day, not 23:00 tonight: stay quiet until every window closed.
    assert (decision.deferred_until.day, decision.deferred_until.hour) == (16, 7)


def test_disabled_windows_are_ignored(db):
    _client, printer, _supply = _fleet(db)
    window = _window(db, start_minute=_hm(18), end_minute=_hm(7))
    window.enabled = False
    db.commit()
    rt = load_settings(db)
    at = datetime(2026, 1, 15, 22, 0, tzinfo=timezone.utc)
    assert suppression.evaluate(db, printer, "warning", at, runtime=rt).dispatch


# --------------------------------------------------------------------------- #
# Maintenance windows -- absolute UTC ranges.
# --------------------------------------------------------------------------- #
def test_maintenance_window_applies_only_inside_its_dated_range(db):
    _client, printer, _supply = _fleet(db)
    _window(db, name="Fleet swap", kind=m.SuppressionKind.maintenance,
            action=m.SuppressionAction.suppress,
            starts_at=datetime(2026, 2, 7, 8, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 2, 7, 14, 0, tzinfo=timezone.utc))
    rt = load_settings(db)

    during = datetime(2026, 2, 7, 10, 0, tzinfo=timezone.utc)
    before = datetime(2026, 2, 7, 7, 0, tzinfo=timezone.utc)
    after = datetime(2026, 2, 7, 15, 0, tzinfo=timezone.utc)
    next_week = datetime(2026, 2, 14, 10, 0, tzinfo=timezone.utc)

    assert suppression.evaluate(db, printer, "warning", during, runtime=rt).suppress
    assert suppression.evaluate(db, printer, "warning", before, runtime=rt).dispatch
    assert suppression.evaluate(db, printer, "warning", after, runtime=rt).dispatch
    # One-off, so it must NOT recur the following week.
    assert suppression.evaluate(db, printer, "warning", next_week, runtime=rt).dispatch


def test_a_backwards_maintenance_range_never_matches(db):
    _client, printer, _supply = _fleet(db)
    _window(db, kind=m.SuppressionKind.maintenance,
            starts_at=datetime(2026, 2, 7, 14, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 2, 7, 8, 0, tzinfo=timezone.utc))
    rt = load_settings(db)
    at = datetime(2026, 2, 7, 10, 0, tzinfo=timezone.utc)
    assert suppression.evaluate(db, printer, "warning", at, runtime=rt).dispatch


def test_deferral_is_clamped_to_a_bounded_horizon(db):
    """A defer-action maintenance window a year long must not hold that long."""
    _client, printer, _supply = _fleet(db)
    at = datetime(2026, 2, 7, 10, 0, tzinfo=timezone.utc)
    _window(db, kind=m.SuppressionKind.maintenance,
            action=m.SuppressionAction.defer,
            starts_at=at - timedelta(days=1),
            ends_at=at + timedelta(days=365))
    rt = load_settings(db)

    decision = suppression.evaluate(db, printer, "warning", at, runtime=rt)
    assert decision.defer
    assert decision.deferred_until <= at + timedelta(days=7)
    assert decision.deferred_until > at


# --------------------------------------------------------------------------- #
# End-to-end through the evaluator: hold, don't notify, then digest.
# --------------------------------------------------------------------------- #
def _quiet_now(db, action=m.SuppressionAction.defer):
    """A window guaranteed to cover 'now', whatever time the suite runs at."""
    return _window(db, start_minute=0, end_minute=24 * 60, action=action)


def test_an_alert_opening_in_quiet_hours_is_held_not_sent(db, monkeypatch):
    _client, _printer, _supply = _fleet(db)
    _quiet_now(db)
    ch = _Recorder()
    monkeypatch.setattr(
        "central.channels.delivery.dispatch",
        lambda note, chans: [(ch.name, ch.send(note))],
    )
    monkeypatch.setattr("central.worker.jobs.route_channels", lambda c, **kw: [ch])

    res = jobs.evaluate_alerts(db)
    # The alert still OPENS -- the condition is real and must stay visible.
    assert res["alerts_opened"] == 1
    alert = db.query(m.Alert).one()
    assert alert.state == m.AlertState.open
    # ...but nothing was sent.
    assert ch.sent == []

    held = db.query(m.NotificationDelivery).one()
    assert held.status == m.DeliveryStatus.deferred
    assert held.channel_key == WITHHELD_CHANNEL_KEY
    assert held.next_attempt_at is not None
    assert held.attempts == 0, "holding must not burn the retry budget"

    # The badge says held, and crucially does not claim a send.
    badge = alert.notified_channels[0]
    assert badge["sent"] is False
    assert "held" in badge["detail"]
    # last_notified_at stays unset: nothing was notified, so the escalation
    # clock must not start from this non-event.
    assert alert.last_notified_at is None


def test_holding_is_idempotent_across_cycles(db, monkeypatch):
    """Re-entering the hold path must not stack a held row per cycle.

    Escalation is deliberately switched ON here. Alert dedupe already stops
    ``_notify_alert`` being re-entered on later cycles, so with escalation off
    this test passes no matter what the idempotency guard does -- escalation is
    the only path that re-enters the hold while a window is open, and therefore
    the only way to actually exercise the guard.
    """
    _client, _printer, _supply = _fleet(db)
    _quiet_now(db)
    _set(db, alerts__escalate_after_minutes=1)
    monkeypatch.setattr("central.worker.jobs.route_channels", lambda c, **kw: [])

    jobs.evaluate_alerts(db)
    assert db.query(m.NotificationDelivery).count() == 1
    alert = db.query(m.Alert).one()
    alert.created_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db.commit()

    for _ in range(5):
        jobs.evaluate_alerts(db)
    assert db.query(m.NotificationDelivery).count() == 1, "held rows stacked per cycle"
    # The stored end is refreshed in place, so extending a window is honoured.
    held = db.query(m.NotificationDelivery).one()
    assert held.status == m.DeliveryStatus.deferred
    assert held.next_attempt_at is not None


def test_escalation_does_not_inflate_its_level_while_held(db, monkeypatch):
    """An alert must not emerge from the night at 'escalation level 480'."""
    _client, _printer, _supply = _fleet(db)
    _quiet_now(db)
    _set(db, alerts__escalate_after_minutes=1)
    monkeypatch.setattr("central.worker.jobs.route_channels", lambda c, **kw: [])

    jobs.evaluate_alerts(db)
    alert = db.query(m.Alert).one()
    alert.created_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db.commit()

    for _ in range(4):
        jobs.evaluate_alerts(db)
    db.refresh(alert)
    assert alert.escalation_level == 0, "held notifications are not escalations"


def test_a_suppressing_window_drops_the_notification_but_records_it(db, monkeypatch):
    _client, _printer, _supply = _fleet(db)
    _quiet_now(db, action=m.SuppressionAction.suppress)
    ch = _Recorder()
    monkeypatch.setattr(
        "central.channels.delivery.dispatch",
        lambda note, chans: [(ch.name, ch.send(note))],
    )
    monkeypatch.setattr("central.worker.jobs.route_channels", lambda c, **kw: [ch])

    jobs.evaluate_alerts(db)
    assert ch.sent == []
    row = db.query(m.NotificationDelivery).one()
    assert row.status == m.DeliveryStatus.suppressed
    assert row.next_attempt_at is None, "suppression is terminal"
    assert "suppressed" in (row.last_error or "")

    # Flushing must never resurrect a suppressed notification.
    res = flush_deferred(db, load_settings(db))
    assert res["digests_sent"] == 0
    assert ch.sent == []


def test_critical_alerts_notify_immediately_even_in_quiet_hours(db, monkeypatch):
    _client, _printer, _supply = _fleet(db)
    _quiet_now(db)
    rule = db.query(m.AlertRule).one()
    rule.severity = m.EventSeverity.critical
    db.commit()
    ch = _Recorder()
    monkeypatch.setattr(
        "central.channels.delivery.dispatch",
        lambda note, chans: [(ch.name, ch.send(note))],
    )
    monkeypatch.setattr("central.worker.jobs.route_channels", lambda c, **kw: [ch])

    jobs.evaluate_alerts(db)
    assert len(ch.sent) == 1
    assert db.query(m.NotificationDelivery).one().status == m.DeliveryStatus.delivered


# --------------------------------------------------------------------------- #
# The digest.
# --------------------------------------------------------------------------- #
def _hold_two_alerts(db, monkeypatch):
    """Two held notifications for one client, ready to flush."""
    _client, printer, supply = _fleet(db)
    _quiet_now(db)
    monkeypatch.setattr("central.worker.jobs.route_channels", lambda c, **kw: [])
    jobs.evaluate_alerts(db)
    # A second condition on the same printer: an unresolved error event.
    db.add(m.AlertRule(
        name="errors", condition_type=m.AlertConditionType.error_severity,
        severity=m.EventSeverity.warning,
    ))
    db.add(m.PrinterEvent(
        printer_id=printer.id, code="PAPER_JAM", message="Jam in tray 2",
        severity=m.EventSeverity.warning,
    ))
    db.commit()
    jobs.evaluate_alerts(db)
    return printer, supply


def test_held_notifications_release_as_one_digest_per_client(db, monkeypatch):
    printer, _supply = _hold_two_alerts(db, monkeypatch)
    held = db.query(m.NotificationDelivery).all()
    assert len(held) == 2
    assert all(h.status == m.DeliveryStatus.deferred for h in held)

    ch = _Recorder()
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])
    monkeypatch.setattr(
        "central.channels.delivery.dispatch",
        lambda note, chans: [(ch.name, ch.send(note))],
    )
    # Wind the clock past the window's end.
    later = datetime.now(timezone.utc) + timedelta(days=2)
    res = flush_deferred(db, load_settings(db), later)

    assert res["digests_sent"] == 1, "one digest, not one message per alert"
    assert res["deferred_delivered"] == 2
    assert len(ch.sent) == 1
    body = ch.sent[0].body
    assert "Low black" in body and "Error on" in body
    assert "2 alerts held" in ch.sent[0].title
    assert all(
        h.status == m.DeliveryStatus.delivered
        for h in db.query(m.NotificationDelivery).all()
    )


def test_the_digest_replaces_the_held_badge_on_each_alert(db, monkeypatch):
    _hold_two_alerts(db, monkeypatch)
    ch = _Recorder()
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])
    monkeypatch.setattr(
        "central.channels.delivery.dispatch",
        lambda note, chans: [(ch.name, ch.send(note))],
    )
    flush_deferred(db, load_settings(db), datetime.now(timezone.utc) + timedelta(days=2))

    for alert in db.query(m.Alert).all():
        badge = alert.notified_channels[0]
        assert badge["sent"] is True, "stale 'held' badge left on a delivered alert"
        assert alert.last_notified_at is not None


def test_a_condition_that_cleared_while_held_is_counted_not_paged(db, monkeypatch):
    printer, supply = _hold_two_alerts(db, monkeypatch)
    # The toner is replaced overnight, so that alert resolves.
    supply.level_pct = 90
    db.commit()
    jobs.evaluate_alerts(db)
    resolved = [a for a in db.query(m.Alert).all() if a.state == m.AlertState.resolved]
    assert len(resolved) == 1

    ch = _Recorder()
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])
    monkeypatch.setattr(
        "central.channels.delivery.dispatch",
        lambda note, chans: [(ch.name, ch.send(note))],
    )
    res = flush_deferred(db, load_settings(db), datetime.now(timezone.utc) + timedelta(days=2))

    assert res["deferred_resolved"] == 1
    assert len(ch.sent) == 1
    body = ch.sent[0].body
    # Stated, not silently dropped -- an operator must be able to answer
    # "what happened overnight?".
    assert "cleared while held" in body
    # ...and not paged about individually.
    assert "Low black" not in body


def test_nothing_is_flushed_before_the_window_closes(db, monkeypatch):
    _hold_two_alerts(db, monkeypatch)
    ch = _Recorder()
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])
    res = flush_deferred(db, load_settings(db), datetime.now(timezone.utc))
    assert res["digests_sent"] == 0
    assert ch.sent == []
    assert all(
        h.status == m.DeliveryStatus.deferred
        for h in db.query(m.NotificationDelivery).all()
    )


def test_a_digest_with_no_channel_becomes_owed_rather_than_lost(db, monkeypatch):
    """The 0.10.0 invariant still holds through the new path."""
    from central.channels.delivery import UNROUTED_CHANNEL_KEY

    _hold_two_alerts(db, monkeypatch)
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [])
    flush_deferred(db, load_settings(db), datetime.now(timezone.utc) + timedelta(days=2))

    rows = db.query(m.NotificationDelivery).all()
    assert all(r.status == m.DeliveryStatus.pending for r in rows)
    assert all(r.channel_key == UNROUTED_CHANNEL_KEY for r in rows)


def test_digest_severity_is_the_highest_it_contains(db, monkeypatch):
    """So a channel gated at critical still receives a digest holding one."""
    _client, printer, _supply = _fleet(db)
    _quiet_now(db)
    # Total silence, so the critical is held rather than breaking through.
    window = db.query(m.SuppressionWindow).one()
    window.allow_breakthrough = False
    db.commit()
    rule = db.query(m.AlertRule).one()
    rule.severity = m.EventSeverity.critical
    db.commit()
    monkeypatch.setattr("central.worker.jobs.route_channels", lambda c, **kw: [])
    jobs.evaluate_alerts(db)

    ch = _Recorder()
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [ch])
    monkeypatch.setattr(
        "central.channels.delivery.dispatch",
        lambda note, chans: [(ch.name, ch.send(note))],
    )
    flush_deferred(db, load_settings(db), datetime.now(timezone.utc) + timedelta(days=2))
    assert ch.sent[0].severity == "critical"


def test_no_windows_configured_changes_nothing(db, monkeypatch):
    """The whole feature must be inert until an operator opts in."""
    _client, _printer, _supply = _fleet(db)
    ch = _Recorder()
    monkeypatch.setattr(
        "central.channels.delivery.dispatch",
        lambda note, chans: [(ch.name, ch.send(note))],
    )
    monkeypatch.setattr("central.worker.jobs.route_channels", lambda c, **kw: [ch])

    jobs.evaluate_alerts(db)
    assert len(ch.sent) == 1
    assert db.query(m.NotificationDelivery).one().status == m.DeliveryStatus.delivered
