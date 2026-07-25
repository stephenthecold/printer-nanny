"""Alerting maturity: stop the evaluator shouting, and stop it lying.

Five independent defects from ``docs/audit-2026-07.md``, all in the path between
"a condition is true" and "an operator is told". Grouped because they share one
theme: the alert pipeline reported things that were not so.

1. **No hysteresis or cooldown.** Opened at ``<= threshold`` and resolved the
   instant the reading wasn't, so a cartridge oscillating around the threshold
   raised a fresh alert -- and a fresh notification -- every cycle. ~720
   notifications/day for one cartridge at a 60s poll.
2. **Only the newest error code was tracked.** A newer ``DOOR_OPEN`` stopped
   refreshing the ``PAPER_JAM`` key, so the resolve pass closed the jam alert
   while the printer was still jammed.
3. **Acknowledged reorder alerts stuck open forever**, and because dedupe
   suppresses while an alert is live, that one stuck row silently blocked every
   future reorder alert for the same cartridge.
4. **Escalation orphaned the FreeScout ticket**: ``external_ref`` was assigned
   unconditionally from a dispatch that only returns an id when it CREATES a
   ticket, so escalating nulled it and the next round opened a duplicate.
5. **Never-sent notifications were recorded as delivered.** A severity gate or
   an unconfigured channel returns ``ok=True`` having transmitted nothing; that
   was stored as ``delivered`` and rendered as a green tick -- a durable audit
   trail asserting a send that never happened.

Every test here asserts the fixed behaviour on real evaluator/dispatch runs, not
on a mock of the fix.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from central import models as m
from central.channels.base import ChannelResult, Notification, NotificationChannel
from central.channels.delivery import record_dispatch, retry_due
from central.runtime import load_settings
from central.worker import jobs


class _Recorder(NotificationChannel):
    """Records what it was asked to send. No network."""

    type = "recorder"

    def __init__(self, name="Email"):
        super().__init__(name, config={}, runtime={})
        self.sent: list = []

    def send(self, note: Notification) -> ChannelResult:
        self.sent.append(note)
        return ChannelResult(ok=True, detail="sent")


class _SilentChannel(NotificationChannel):
    """Reports success without transmitting -- the dry-run / severity-gate shape."""

    type = "silent"

    def __init__(self, name="Slack"):
        super().__init__(name, config={}, runtime={})
        self.calls = 0

    def send(self, note: Notification) -> ChannelResult:
        self.calls += 1
        return ChannelResult(ok=True, detail="dry-run (nothing configured)", sent=False)


def _fleet(db, *, level=5, threshold=10):
    """One approved printer with a black toner supply + a supply_below rule."""
    client = m.Client(name="Acme")
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
    rule = m.AlertRule(
        name="low", condition_type=m.AlertConditionType.supply_below,
        threshold=threshold, severity=m.EventSeverity.warning,
    )
    db.add(rule)
    db.commit()
    return printer, supply, rule


def _set(db, **kv):
    """Write runtime settings directly -- no form/section semantics needed here."""
    for key, value in kv.items():
        key = key.replace("__", ".")
        row = db.query(m.AppSetting).filter_by(key=key).one_or_none()
        if row is None:
            db.add(m.AppSetting(key=key, value=str(value)))
        else:
            row.value = str(value)
    db.commit()


# --------------------------------------------------------------------------- #
# 1. Hysteresis -- the deadband
# --------------------------------------------------------------------------- #
def test_supply_oscillating_around_the_threshold_alerts_once_not_every_cycle(db):
    """9 -> 11 -> 9 -> 11 around a threshold of 10 is ONE alert, ONE notification."""
    _printer, supply, _rule = _fleet(db, level=9, threshold=10)
    _set(db, alerts__supply_deadband_pct=3, alerts__renotify_cooldown_min=0)

    assert jobs.evaluate_alerts(db)["alerts_opened"] == 1

    # 11% is back over the threshold but still inside the 3-point margin, so the
    # alert HOLDS rather than resolving. This is the cycle that used to resolve.
    supply.level_pct = 11
    db.commit()
    res = jobs.evaluate_alerts(db)
    assert res["alerts_resolved"] == 0, "in the deadband: must not resolve"
    assert res["alerts_opened"] == 0

    # Back down, and back up again -- still one alert the whole way through.
    for level in (9, 11, 9, 11):
        supply.level_pct = level
        db.commit()
        res = jobs.evaluate_alerts(db)
        assert res["alerts_opened"] == 0
        assert res["alerts_resolved"] == 0

    assert db.query(m.Alert).count() == 1
    assert db.query(m.Alert).one().state == m.AlertState.open

    # A genuine recovery past threshold + margin DOES resolve it.
    supply.level_pct = 14
    db.commit()
    assert jobs.evaluate_alerts(db)["alerts_resolved"] == 1
    assert db.query(m.Alert).one().state == m.AlertState.resolved


def test_deadband_does_not_hold_an_alert_that_was_never_opened(db):
    """The hold only applies to a LIVE alert -- it must not invent one."""
    _printer, supply, _rule = _fleet(db, level=11, threshold=10)
    _set(db, alerts__supply_deadband_pct=3)

    # 11% is inside the deadband but no alert is live, so nothing should happen.
    assert jobs.evaluate_alerts(db)["alerts_opened"] == 0
    assert db.query(m.Alert).count() == 0


def test_zero_deadband_keeps_the_old_immediate_resolve(db):
    """The margin is operator-tunable and 0 must restore the un-damped behaviour."""
    _printer, supply, _rule = _fleet(db, level=9, threshold=10)
    _set(db, alerts__supply_deadband_pct=0, alerts__renotify_cooldown_min=0)

    assert jobs.evaluate_alerts(db)["alerts_opened"] == 1
    supply.level_pct = 11
    db.commit()
    assert jobs.evaluate_alerts(db)["alerts_resolved"] == 1


# --------------------------------------------------------------------------- #
# 1b. Hysteresis -- the flap cooldown, which covers what a deadband can't
# --------------------------------------------------------------------------- #
def test_condition_refiring_inside_the_cooldown_refolds_instead_of_renotifying(db, monkeypatch):
    """A re-fire within the window re-opens the SAME alert and stays quiet."""
    _printer, supply, _rule = _fleet(db, level=9, threshold=10)
    # Deadband off so the resolve genuinely happens; cooldown on to catch the re-fire.
    _set(db, alerts__supply_deadband_pct=0, alerts__renotify_cooldown_min=30)
    ch = _Recorder()
    monkeypatch.setattr("central.worker.jobs.routable_channels", lambda db_, rt: [])
    monkeypatch.setattr("central.channels.delivery.dispatch", lambda note, chans: [(ch.name, ch.send(note))])

    assert jobs.evaluate_alerts(db)["alerts_opened"] == 1
    first_id = db.query(m.Alert).one().id

    supply.level_pct = 20          # recovers -> resolves
    db.commit()
    assert jobs.evaluate_alerts(db)["alerts_resolved"] == 1

    supply.level_pct = 9           # re-fires immediately -> flap, not a new alert
    db.commit()
    res = jobs.evaluate_alerts(db)
    assert res["alerts_opened"] == 0, "a flap must not open a new alert"
    assert res["alerts_flapped"] == 1

    alert = db.query(m.Alert).one()          # still exactly one row
    assert alert.id == first_id
    assert alert.state == m.AlertState.open
    assert alert.resolved_at is None
    assert alert.flap_count == 1
    assert "flapped 1" in (alert.detail or "")


def test_refire_after_the_cooldown_expires_is_a_genuinely_new_alert(db):
    """Damping is time-bounded: a recurrence next week is real news."""
    _printer, supply, _rule = _fleet(db, level=9, threshold=10)
    _set(db, alerts__supply_deadband_pct=0, alerts__renotify_cooldown_min=30)

    assert jobs.evaluate_alerts(db)["alerts_opened"] == 1
    supply.level_pct = 20
    db.commit()
    assert jobs.evaluate_alerts(db)["alerts_resolved"] == 1

    # Backdate the resolve well outside the 30-minute window.
    resolved = db.query(m.Alert).one()
    resolved.resolved_at = datetime.now(timezone.utc) - timedelta(hours=6)
    db.commit()

    supply.level_pct = 9
    db.commit()
    res = jobs.evaluate_alerts(db)
    assert res["alerts_opened"] == 1
    assert res["alerts_flapped"] == 0
    assert db.query(m.Alert).count() == 2


def test_cooldown_of_zero_disables_flap_damping(db):
    _printer, supply, _rule = _fleet(db, level=9, threshold=10)
    _set(db, alerts__supply_deadband_pct=0, alerts__renotify_cooldown_min=0)

    assert jobs.evaluate_alerts(db)["alerts_opened"] == 1
    supply.level_pct = 20
    db.commit()
    jobs.evaluate_alerts(db)
    supply.level_pct = 9
    db.commit()
    assert jobs.evaluate_alerts(db)["alerts_opened"] == 1
    assert db.query(m.Alert).count() == 2


# --------------------------------------------------------------------------- #
# 2. One alert per error code
# --------------------------------------------------------------------------- #
def _error_fleet(db):
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.6",
        model="HP M404", display_name="Copy room",
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.flush()
    db.add(m.AlertRule(
        name="errors", condition_type=m.AlertConditionType.error_severity,
        threshold=None, severity=m.EventSeverity.warning,
    ))
    db.commit()
    return printer


def _event(db, printer, code, message, *, minutes_ago=0, severity=m.EventSeverity.warning):
    db.add(m.PrinterEvent(
        printer_id=printer.id, code=code, message=message, severity=severity,
        ts=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    ))
    db.commit()


def test_a_newer_error_code_does_not_resolve_an_older_unresolved_one(db):
    """The headline bug: DOOR_OPEN must not mark a still-live PAPER_JAM fixed."""
    printer = _error_fleet(db)
    _set(db, alerts__renotify_cooldown_min=0)
    _event(db, printer, "PAPER_JAM", "Paper jam in tray 2", minutes_ago=10)

    assert jobs.evaluate_alerts(db)["alerts_opened"] == 1
    jam = db.query(m.Alert).one()
    assert "PAPER_JAM" in jam.dedupe_key

    # A newer, different fault arrives while the jam is STILL unresolved.
    _event(db, printer, "DOOR_OPEN", "Front door open", minutes_ago=0)
    res = jobs.evaluate_alerts(db)

    assert res["alerts_opened"] == 1, "the new code gets its own alert"
    assert res["alerts_resolved"] == 0, "the jam is still jammed -- must not resolve"
    db.refresh(jam)
    assert jam.state == m.AlertState.open

    keys = {a.dedupe_key.rsplit(":", 1)[-1] for a in db.query(m.Alert).all()}
    assert keys == {"PAPER_JAM", "DOOR_OPEN"}


def test_clearing_one_error_code_resolves_only_its_own_alert(db):
    printer = _error_fleet(db)
    _set(db, alerts__renotify_cooldown_min=0)
    _event(db, printer, "PAPER_JAM", "Paper jam", minutes_ago=10)
    _event(db, printer, "DOOR_OPEN", "Door open", minutes_ago=5)
    jobs.evaluate_alerts(db)
    assert db.query(m.Alert).count() == 2

    # The door gets shut; the jam persists.
    door = db.query(m.PrinterEvent).filter_by(code="DOOR_OPEN").one()
    door.resolved_at = datetime.now(timezone.utc)
    db.commit()

    assert jobs.evaluate_alerts(db)["alerts_resolved"] == 1
    live = db.query(m.Alert).filter(m.Alert.state == m.AlertState.open).all()
    assert len(live) == 1
    assert "PAPER_JAM" in live[0].dedupe_key


def test_repeats_of_the_same_code_stay_one_alert(db):
    """Per-code, not per-event: five jams in a row is still one jam alert."""
    printer = _error_fleet(db)
    _set(db, alerts__renotify_cooldown_min=0)
    for i in range(5):
        _event(db, printer, "PAPER_JAM", f"Jam #{i}", minutes_ago=5 - i)

    assert jobs.evaluate_alerts(db)["alerts_opened"] == 1
    assert db.query(m.Alert).count() == 1


def test_error_alerts_are_capped_and_the_cap_is_disclosed(db):
    """A device spewing codes must not open dozens -- and must say it was capped."""
    printer = _error_fleet(db)
    _set(db, alerts__max_error_alerts_per_printer=3, alerts__renotify_cooldown_min=0)
    for i in range(7):
        _event(db, printer, f"CODE_{i}", f"Fault {i}", minutes_ago=7 - i)

    assert jobs.evaluate_alerts(db)["alerts_opened"] == 3
    alerts = db.query(m.Alert).all()
    assert len(alerts) == 3
    # Newest kept: CODE_6/5/4 (minutes_ago 1/2/3).
    assert {a.dedupe_key.rsplit(":", 1)[-1] for a in alerts} == {"CODE_6", "CODE_5", "CODE_4"}
    # The 4 dropped codes are counted, not silently discarded.
    assert all("+4 more distinct error code" in (a.detail or "") for a in alerts)


# --------------------------------------------------------------------------- #
# 3. Acknowledged reorder alerts must still auto-resolve
# --------------------------------------------------------------------------- #
def test_acknowledged_reorder_alert_resolves_and_unblocks_future_alerts(db):
    """An ack'd predicted-depletion alert must not become a permanent mute."""
    printer, _supply, _rule = _fleet(db)
    alert = m.Alert(
        printer_id=printer.id,
        type=m.AlertConditionType.predicted_depletion,
        severity=m.EventSeverity.warning,
        state=m.AlertState.acknowledged,        # the operator saw it and ordered
        title="Reorder black",
        detail="projected empty in 3d",
        dedupe_key="forecast:printer:%d:supply:black" % printer.id,
    )
    db.add(alert)
    db.commit()

    # The cartridge is swapped, so the key is no longer active.
    # resolved == 1 is the discriminating assertion: with the old `state == open`
    # filter an acknowledged alert never matched, so this returned 0.
    resolved = jobs._resolve_stale_forecasts(db, set(), datetime.now(timezone.utc))
    assert resolved == 1
    db.commit()          # the job doesn't commit; refresh would revert without this
    db.refresh(alert)
    assert alert.state == m.AlertState.resolved
    assert alert.resolved_at is not None


def test_open_reorder_alert_still_resolves(db):
    """Guard the ordinary path while widening the state filter."""
    printer, _supply, _rule = _fleet(db)
    alert = m.Alert(
        printer_id=printer.id,
        type=m.AlertConditionType.predicted_depletion,
        severity=m.EventSeverity.warning,
        state=m.AlertState.open,
        title="Reorder black",
        dedupe_key="forecast:printer:%d:supply:black" % printer.id,
    )
    db.add(alert)
    db.commit()
    assert jobs._resolve_stale_forecasts(db, set(), datetime.now(timezone.utc)) == 1


def test_a_still_projected_reorder_alert_is_left_alone(db):
    """Only a key that dropped out of the active set resolves."""
    printer, _supply, _rule = _fleet(db)
    key = "forecast:printer:%d:supply:black" % printer.id
    alert = m.Alert(
        printer_id=printer.id,
        type=m.AlertConditionType.predicted_depletion,
        severity=m.EventSeverity.warning,
        state=m.AlertState.acknowledged,
        title="Reorder black",
        dedupe_key=key,
    )
    db.add(alert)
    db.commit()
    assert jobs._resolve_stale_forecasts(db, {key}, datetime.now(timezone.utc)) == 0
    db.refresh(alert)
    assert alert.state == m.AlertState.acknowledged


# --------------------------------------------------------------------------- #
# 4. Escalation must not orphan the ticket
# --------------------------------------------------------------------------- #
def test_escalation_keeps_the_existing_external_ref(db, monkeypatch):
    """Re-notifying must not null the FreeScout id and open a duplicate ticket."""
    printer, _supply, _rule = _fleet(db)
    alert = m.Alert(
        printer_id=printer.id,
        type=m.AlertConditionType.supply_below,
        severity=m.EventSeverity.warning,
        state=m.AlertState.open,
        title="Low black",
        dedupe_key="rule:1:printer:%d:supply:1" % printer.id,
        external_ref="convo-4242",
        last_notified_at=datetime.now(timezone.utc) - timedelta(hours=3),
    )
    db.add(alert)
    db.commit()

    # A channel that delivers but returns no external_ref -- i.e. anything that
    # isn't a FreeScout *create*. This is what used to wipe the ref.
    ch = _Recorder()
    monkeypatch.setattr(
        "central.channels.delivery.dispatch",
        lambda note, chans: [(ch.name, ch.send(note))],
    )
    escalated = jobs._escalate_alerts(
        db, {"alerts.escalate_after_minutes": 60}, [], datetime.now(timezone.utc)
    )
    assert escalated == 1
    # Commit BEFORE refreshing: _escalate_alerts doesn't commit (evaluate_alerts
    # does), and refreshing an uncommitted row re-reads the old committed value.
    # Without this the external_ref assertion passes even when the code nulls it.
    db.commit()
    db.refresh(alert)
    assert alert.external_ref == "convo-4242", "escalation orphaned the ticket"
    assert alert.escalation_level == 1


# --------------------------------------------------------------------------- #
# 5. A never-sent notification is not a delivery
# --------------------------------------------------------------------------- #
def test_a_silent_channel_is_recorded_skipped_not_delivered(db):
    """ok=True without transmission must never be stored as delivered."""
    note = Notification(title="Low black", body="5%", severity="warning")
    ch = _SilentChannel()
    results = record_dispatch(db, None, note, [ch], runtime=load_settings(db))
    db.commit()

    assert results[0][1].ok is True          # nothing failed...
    assert results[0][1].sent is False       # ...and nothing was sent

    row = db.query(m.NotificationDelivery).one()
    assert row.status == m.DeliveryStatus.skipped
    assert row.status != m.DeliveryStatus.delivered
    assert row.last_error and "dry-run" in row.last_error


def test_a_skipped_delivery_is_terminal_and_never_retried(db):
    """Deterministic non-sends must not be re-attempted every cycle forever."""
    note = Notification(title="Low black", body="5%", severity="warning")
    ch = _SilentChannel()
    record_dispatch(db, None, note, [ch], runtime=load_settings(db))
    db.commit()
    assert ch.calls == 1

    res = retry_due(db, load_settings(db))
    assert res["deliveries_retried"] == 0
    assert ch.calls == 1, "a skipped row was retried"
    assert db.query(m.NotificationDelivery).one().status == m.DeliveryStatus.skipped


def test_a_real_send_is_still_recorded_delivered(db):
    """Guard the happy path while splitting delivered from skipped."""
    note = Notification(title="Low black", body="5%", severity="warning")
    ch = _Recorder()
    results = record_dispatch(db, None, note, [ch], runtime=load_settings(db))
    db.commit()

    assert results[0][1].sent is True
    row = db.query(m.NotificationDelivery).one()
    assert row.status == m.DeliveryStatus.delivered
    assert row.last_error is None


def test_alert_badges_carry_sent_so_the_ui_can_tell_the_truth(db, monkeypatch):
    """notified_channels must distinguish 'told nobody' from 'told them'."""
    _printer, _supply, _rule = _fleet(db)
    _set(db, alerts__renotify_cooldown_min=0)
    silent = _SilentChannel("Slack")
    monkeypatch.setattr(
        "central.channels.delivery.dispatch",
        lambda note, chans: [(silent.name, silent.send(note))],
    )
    monkeypatch.setattr(
        "central.worker.jobs.route_channels",
        lambda cands, **kw: [silent],
    )
    assert jobs.evaluate_alerts(db)["alerts_opened"] == 1

    badge = db.query(m.Alert).one().notified_channels[0]
    assert badge["channel"] == "Slack"
    assert badge["ok"] is True
    assert badge["sent"] is False, "the UI would render a green tick for a non-send"


def test_owed_row_survives_a_channel_that_is_enabled_but_unconfigured(db, monkeypatch):
    """An unconfigured channel is not a destination -- recovery must be preserved.

    Terminating the owed row here would undo the 0.10.0 fix: pasting a webhook
    URL later has to still deliver the backlog.
    """
    _printer, _supply, _rule = _fleet(db)
    _set(db, alerts__renotify_cooldown_min=0)
    monkeypatch.setattr("central.worker.jobs.routable_channels", lambda db_, rt: [])
    assert jobs.evaluate_alerts(db)["alerts_opened"] == 1
    owed = db.query(m.NotificationDelivery).one()
    assert owed.status == m.DeliveryStatus.pending

    # A channel exists now, but it transmits nothing (no URL configured).
    silent = _SilentChannel("Slack")
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [silent])
    res = retry_due(db, load_settings(db))
    assert res["deliveries_delivered"] == 0
    assert res["deliveries_owed"] == 1, "still owed -- nobody has been told"

    db.refresh(owed)
    assert owed.status == m.DeliveryStatus.pending
    assert owed.attempts == 0, "an unconfigured channel must not burn the retry budget"
    assert db.query(m.NotificationDelivery).count() == 1, "no duplicate sibling rows"

    # Now the URL is pasted: the backlog must actually arrive.
    real = _Recorder("Slack")
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [real])
    res = retry_due(db, load_settings(db))
    assert res["deliveries_delivered"] == 1
    assert [n.title for n in real.sent] == ["Low black on Front desk @ 10.0.0.5"]
    assert db.query(m.NotificationDelivery).one().status == m.DeliveryStatus.delivered


def test_a_genuine_failure_still_retries_rather_than_reverting_to_owed(db, monkeypatch):
    """The all-skipped revert must not swallow a retryable failure."""
    _printer, _supply, _rule = _fleet(db)
    _set(db, alerts__renotify_cooldown_min=0)
    monkeypatch.setattr("central.worker.jobs.routable_channels", lambda db_, rt: [])
    jobs.evaluate_alerts(db)
    owed = db.query(m.NotificationDelivery).one()

    class _Broken(NotificationChannel):
        type = "broken"

        def send(self, note):
            return ChannelResult(ok=False, detail="smtp exploded")

    broken = _Broken("Email", config={}, runtime={})
    monkeypatch.setattr("central.channels.delivery.active_channels", lambda rt: [broken])
    retry_due(db, load_settings(db))

    db.refresh(owed)
    assert owed.status == m.DeliveryStatus.failed, "a real failure must stay retryable"
    assert owed.attempts == 1
    assert owed.next_attempt_at is not None
    assert "smtp exploded" in (owed.last_error or "")
