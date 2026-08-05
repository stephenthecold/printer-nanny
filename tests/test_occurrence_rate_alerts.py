"""Occurrence-rate alerting: "not every jam, but ten jams a day".

Every other ``AlertConditionType`` asks whether a condition is true *now*. A
device that jams, clears, and jams again never holds any of them long enough to
be noticed -- ``error_severity`` raises one alert for the standing condition and
then goes quiet while the fault keeps recurring. ``occurrence_rate`` counts
matching ``printer_events`` inside a rolling window instead.

The tests are grouped by the thing that could quietly go wrong:

1. **The count and the window.** N in W opens; the same N spread wider does not.
   The window has to *roll* -- an occurrence that ages out has to stop counting,
   or the rule degenerates into "N since the beginning of time".
2. **Matching.** Code substring, severity floor, tenant scope. Plus the two ways
   an operator-typed match string could misbehave in SQL.
3. **The flap-damping interaction**, which is the subtle one. A rate alert is
   *about* repetition, so the mechanism that folds repeated firings into one
   alert is aimed at exactly the thing this feature measures. Both halves of the
   decision are asserted here: dedupe must not leave a stale number on screen,
   and the cooldown must not silently swallow a burst that shares no evidence
   with the previous one.
4. **Hysteresis on the way down** -- resolving the instant the count dips one
   under is how flapping was invented in this codebase the first time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from central import models as m
from central.worker import jobs

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _set(db, **kv):
    """Write runtime settings directly (``__`` stands in for ``.``)."""
    for key, value in kv.items():
        key = key.replace("__", ".")
        row = db.query(m.AppSetting).filter_by(key=key).one_or_none()
        if row is None:
            db.add(m.AppSetting(key=key, value=str(value)))
        else:
            row.value = str(value)
    db.commit()


def _printer(db, *, client_name="Acme", ip="10.0.0.5", name="Front desk"):
    client = m.Client(name=client_name)
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name=f"{client_name} HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip=ip,
        model="Brother MFC-L8900CDW", display_name=name,
        discovery_state=m.DiscoveryState.approved,
    )
    db.add(printer)
    db.commit()
    return printer


def _rule(db, *, count=10, window=1440, code="jam", floor=m.EventSeverity.warning,
          scope=m.AlertScope.global_, scope_id=None, severity=m.EventSeverity.warning):
    rule = m.AlertRule(
        name="jams",
        scope=scope,
        scope_id=scope_id,
        condition_type=m.AlertConditionType.occurrence_rate,
        threshold=float(count),
        severity=severity,
        window_minutes=window,
        match_code=code,
        match_min_severity=floor,
    )
    db.add(rule)
    db.commit()
    return rule


def _events_at(db, printer, offsets_min, *, code="jammed",
               severity=m.EventSeverity.warning, now=NOW):
    """One event per offset, each ``offset`` minutes BEFORE ``now``.

    Every occurrence is a separate row on purpose: ``services._reconcile_events``
    refreshes a *standing* snmp_alert condition in place and inserts a new row
    only once the condition has cleared, so one row per occurrence is exactly
    what a printer that jams, is cleared, and jams again actually produces.
    """
    for offset in offsets_min:
        ts = now - timedelta(minutes=offset)
        db.add(m.PrinterEvent(
            printer_id=printer.id,
            ts=ts,
            code=code,
            severity=severity,
            source=m.EventSource.snmp_alert,
            message="Paper jam",
            resolved_at=ts + timedelta(minutes=1),
        ))
    db.commit()


def _burst(db, printer, n, *, spacing_min=10, first_offset_min=5, **kw):
    """N events walking backwards from ``now``, ``spacing_min`` apart."""
    _events_at(
        db, printer,
        [first_offset_min + i * spacing_min for i in range(n)],
        **kw,
    )


def _open_rate_alerts(db):
    return list(db.query(m.Alert).filter(
        m.Alert.type == m.AlertConditionType.occurrence_rate,
        m.Alert.state.in_((m.AlertState.open, m.AlertState.acknowledged)),
    ).all())


# --------------------------------------------------------------------------- #
# 1. The count and the window
# --------------------------------------------------------------------------- #
def test_ten_jams_in_a_day_opens_one_alert_carrying_the_real_count(db):
    printer = _printer(db)
    _rule(db, count=10, window=1440)
    _burst(db, printer, 12, spacing_min=60)  # 12 jams over the last 12 hours

    res = jobs.evaluate_alerts(db, now=NOW)

    assert res["alerts_opened"] == 1
    alert = _open_rate_alerts(db)[0]
    assert alert.printer_id == printer.id
    assert "Front desk" in alert.title
    # The number is the whole content of a rate alert; it has to be the real one.
    assert "12 matching event(s) in the last 1d" in alert.detail
    assert "threshold 10" in alert.detail
    # ...and the alert has to say what it counted, because the email carries no
    # link back to the rule.
    assert "code contains 'jam'" in alert.detail
    assert "warning or above" in alert.detail


def test_the_same_burst_spread_outside_the_window_opens_nothing(db):
    """12 jams, but at 4-hour spacing: only 6 land inside a 24h window."""
    printer = _printer(db)
    _rule(db, count=10, window=1440)
    _burst(db, printer, 12, spacing_min=240)

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 0
    assert _open_rate_alerts(db) == []


def test_the_window_rolls_rather_than_counting_since_the_beginning_of_time(db):
    """The identical rows stop qualifying once ``now`` moves past them.

    This is the property that separates a rolling window from a running total.
    Nothing about the data changes between the two calls -- only the clock.
    """
    printer = _printer(db)
    _rule(db, count=5, window=60)
    _burst(db, printer, 6, spacing_min=5)  # all inside the last 35 minutes

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 1

    # Two hours later those same six events are all outside a 60-minute window.
    later = NOW + timedelta(hours=2)
    res = jobs.evaluate_alerts(db, now=later)
    assert res["alerts_resolved"] == 1
    assert _open_rate_alerts(db) == []


def test_a_rule_with_no_window_is_skipped_rather_than_scanning_everything(db):
    """A window is half the operator's intent; it is never defaulted."""
    printer = _printer(db)
    rule = _rule(db, count=1, window=1440)
    rule.window_minutes = None
    db.commit()
    _burst(db, printer, 50, spacing_min=1)

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 0


def test_a_zero_count_rule_is_skipped_rather_than_firing_on_every_printer(db):
    _printer(db)
    rule = _rule(db, count=10)
    rule.threshold = 0
    db.commit()

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 0


def test_an_absurd_window_is_clamped_to_the_documented_maximum(db):
    """A hand-edited row cannot turn this into an unbounded table scan."""
    rule = _rule(db, window=1440)
    rule.window_minutes = 10 ** 9
    db.commit()

    assert jobs._occurrence_window_minutes(rule) == jobs.OCCURRENCE_MAX_WINDOW_MINUTES


# --------------------------------------------------------------------------- #
# 2. Matching
# --------------------------------------------------------------------------- #
def test_the_code_match_is_a_case_insensitive_substring(db):
    """"jam" has to catch the agent's "jammed" without an operator knowing RFC 3805."""
    printer = _printer(db)
    _rule(db, count=3, window=1440, code="JAM")
    _burst(db, printer, 4, spacing_min=30, code="jammed")

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 1


def test_events_with_a_different_code_do_not_count(db):
    printer = _printer(db)
    _rule(db, count=3, window=1440, code="jam")
    _burst(db, printer, 10, spacing_min=30, code="door-open")

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 0


def test_a_blank_code_counts_every_event(db):
    printer = _printer(db)
    _rule(db, count=3, window=1440, code=None)
    _burst(db, printer, 2, spacing_min=30, code="jammed")
    _burst(db, printer, 2, spacing_min=30, first_offset_min=7, code="door-open")

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 1


def test_the_severity_floor_excludes_info_noise(db):
    """Low paper and power-save offline are recorded at info; a jam rule ignores them."""
    printer = _printer(db)
    _rule(db, count=3, window=1440, code=None, floor=m.EventSeverity.warning)
    _burst(db, printer, 10, spacing_min=30, severity=m.EventSeverity.info)

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 0

    # ...and the same rule with no floor counts them.
    rule = db.query(m.AlertRule).one()
    rule.match_min_severity = None
    db.commit()
    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 1


def test_the_alert_severity_is_independent_of_what_is_counted(db):
    """A critical alert about a flood of warnings has to be expressible.

    ``error_severity`` conflates the two (its ``rule.severity`` is both the match
    floor and the raised severity); this condition type deliberately does not.
    """
    printer = _printer(db)
    _rule(db, count=3, window=1440, floor=m.EventSeverity.warning,
          severity=m.EventSeverity.critical)
    _burst(db, printer, 4, spacing_min=30, severity=m.EventSeverity.warning)

    jobs.evaluate_alerts(db, now=NOW)
    assert _open_rate_alerts(db)[0].severity == m.EventSeverity.critical


def test_a_percent_in_the_match_code_is_a_literal_not_a_wildcard(db):
    """An unescaped LIKE metacharacter turns a narrow rule into "match everything".

    That fails open: the operator gets a flood of alerts about events they never
    asked to be told about, and nothing in the UI says why.
    """
    printer = _printer(db)
    _rule(db, count=1, window=1440, code="%")
    _burst(db, printer, 5, spacing_min=30, code="jammed")

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 0, (
        "'%' must match a literal percent sign in the code, not every event"
    )

    # An underscore is the other LIKE metacharacter: it must not match "jammed".
    rule = db.query(m.AlertRule).one()
    rule.match_code = "jamme_"
    db.commit()
    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 0


def test_a_rate_rule_is_tenant_scoped_like_every_other_rule(db):
    """A client-scoped rule never counts another customer's fleet."""
    ours = _printer(db, client_name="Acme", ip="10.0.0.5", name="Acme MFC")
    theirs = _printer(db, client_name="Globex", ip="10.9.9.9", name="Globex MFC")
    _rule(db, count=3, window=1440, scope=m.AlertScope.client, scope_id=ours.client_id)
    _burst(db, theirs, 20, spacing_min=10)

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 0

    _burst(db, ours, 4, spacing_min=30)
    jobs.evaluate_alerts(db, now=NOW)
    alerts = _open_rate_alerts(db)
    assert [a.printer_id for a in alerts] == [ours.id]


def test_unapproved_printers_are_not_counted(db):
    printer = _printer(db)
    printer.discovery_state = m.DiscoveryState.pending
    db.commit()
    _rule(db, count=3, window=1440)
    _burst(db, printer, 10, spacing_min=30)

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 0


def test_one_alert_per_printer_not_one_per_event(db):
    a = _printer(db, ip="10.0.0.5", name="A")
    b = _printer(db, client_name="Acme2", ip="10.0.0.6", name="B")
    _rule(db, count=3, window=1440)
    _burst(db, a, 5, spacing_min=30)
    _burst(db, b, 9, spacing_min=30)

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 2
    assert len(_open_rate_alerts(db)) == 2


# --------------------------------------------------------------------------- #
# 3. The flap-damping interaction
# --------------------------------------------------------------------------- #
def test_a_live_rate_alert_tracks_the_growing_count_instead_of_freezing_at_open(db):
    """Dedupe must not leave the alert asserting a number the printer left behind.

    This is the half of the flap interaction that is NOT about the cooldown.
    ``_find_open_alert`` short-circuits every re-fire while an alert is live,
    which is right for a level condition ("9%" vs "8%" barely differs) and wrong
    for a rate one: the count IS the alert, so an unrefreshed detail is the alert
    telling the operator something untrue, not something merely stale.
    """
    printer = _printer(db)
    _rule(db, count=10, window=1440)
    _burst(db, printer, 11, spacing_min=60)

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 1
    assert "11 matching event(s)" in _open_rate_alerts(db)[0].detail

    # Twenty more jams, same rolling window, same live alert.
    _burst(db, printer, 20, spacing_min=2, first_offset_min=1)
    res = jobs.evaluate_alerts(db, now=NOW)

    assert res["alerts_opened"] == 0, "dedupe still holds -- one incident, one alert"
    alert = _open_rate_alerts(db)[0]
    assert "31 matching event(s)" in alert.detail
    assert "11 matching" not in alert.detail


def test_the_flap_cooldown_still_folds_a_re_fire_inside_the_rules_own_window(db):
    """Within W the two firings count overlapping events, so it IS one incident.

    Nothing here is a regression of the cooldown: this asserts it still applies
    to rate rules in the case where its claim ("same incident") is true.
    """
    printer = _printer(db)
    _rule(db, count=3, window=240)  # 4h window
    _set(db, alerts__renotify_cooldown_min=30, alerts__occurrence_clear_margin_pct=0)
    _burst(db, printer, 3, spacing_min=30)

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 1

    # Let it resolve by moving past the events, then re-fire 10 minutes later --
    # well inside both the 30-minute cooldown and the 4-hour window.
    cleared = NOW + timedelta(hours=5)
    assert jobs.evaluate_alerts(db, now=cleared)["alerts_resolved"] == 1

    refire = cleared + timedelta(minutes=10)
    _burst(db, printer, 3, spacing_min=1, now=refire)
    res = jobs.evaluate_alerts(db, now=refire)

    assert res["alerts_opened"] == 0
    assert res["alerts_flapped"] == 1, "folded back into the original alert"
    alert = _open_rate_alerts(db)[0]
    assert alert.flap_count == 1
    assert "flapped 1×" in alert.detail
    # ...and the folded alert still carries the CURRENT count, not the old one.
    assert "3 matching event(s)" in alert.detail


def test_the_cooldown_cannot_swallow_a_burst_that_shares_no_counted_event(db):
    """The cooldown is capped at the rule's own window for rate rules.

    A 10-minute "5 jams" rule under the shipped 30-minute cooldown: the second
    burst happens 15 minutes after the first alert resolved, so not one of its
    five jams was counted by the first alert. Folding them would be the damping
    mechanism silently defeating the feature that exists to measure repetition --
    the operator is told once about a printer that jammed ten times.
    """
    printer = _printer(db)
    _rule(db, count=5, window=10)
    _set(db, alerts__renotify_cooldown_min=30, alerts__occurrence_clear_margin_pct=0)
    _burst(db, printer, 5, spacing_min=1, first_offset_min=1)

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 1
    first = _open_rate_alerts(db)[0].id

    # Everything ages out; the alert resolves.
    quiet = NOW + timedelta(minutes=20)
    assert jobs.evaluate_alerts(db, now=quiet)["alerts_resolved"] == 1

    # A brand-new burst 15 minutes later: inside the 30m cooldown, outside the
    # 10m window, sharing zero events with the first alert.
    second_burst = quiet + timedelta(minutes=15)
    _burst(db, printer, 5, spacing_min=1, first_offset_min=1, now=second_burst)
    res = jobs.evaluate_alerts(db, now=second_burst)

    assert res["alerts_flapped"] == 0, "no shared evidence -- this is not the same incident"
    assert res["alerts_opened"] == 1, "a second burst must raise (and notify) again"
    live = _open_rate_alerts(db)
    assert len(live) == 1
    assert live[0].id != first


def test_the_cooldown_cap_never_lengthens_the_cooldown(db):
    """A 24h-window rule keeps the operator's 30-minute cooldown, not a 24h one."""
    runtime = {"alerts.renotify_cooldown_min": 30}
    assert jobs._flap_cooldown_minutes(runtime, 1440) == 30
    assert jobs._flap_cooldown_minutes(runtime, 10) == 10
    assert jobs._flap_cooldown_minutes(runtime, None) == 30
    # Cooldown disabled stays disabled whatever the window says.
    assert jobs._flap_cooldown_minutes({"alerts.renotify_cooldown_min": 0}, 1440) == 0


# --------------------------------------------------------------------------- #
# 4. Hysteresis on the way down
# --------------------------------------------------------------------------- #
# Ten jams inside a 24h window, deliberately back-loaded so advancing the clock
# ages them out one at a time: at +1h nine remain, at +3h seven do. With a 25%
# margin on a threshold of 10 the clear floor is 7.5, so those two instants sit
# either side of it -- which is the whole point of the margin.
_TEN_OVER_A_DAY = [10, 100, 200, 300, 400, 500, 600, 1300, 1350, 1400]


def test_the_alert_holds_while_the_count_is_inside_the_recovery_margin(db):
    """Resolving on the first dip under is how flapping was invented here once.

    A rolling window sheds occurrences by itself, so without a margin every rate
    alert resolves the moment the oldest event ages out and re-opens on the very
    next one.
    """
    printer = _printer(db)
    _rule(db, count=10, window=1440)
    _set(db, alerts__occurrence_clear_margin_pct=25, alerts__renotify_cooldown_min=0)
    _events_at(db, printer, _TEN_OVER_A_DAY)

    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 1

    # One hour on, the oldest jam has aged out: 9 in the window, under the
    # threshold but inside the 25% margin (floor 7.5). This is the cycle that
    # would otherwise resolve.
    res = jobs.evaluate_alerts(db, now=NOW + timedelta(hours=1))
    assert res["alerts_resolved"] == 0
    alert = _open_rate_alerts(db)[0]
    assert "9 matching event(s)" in alert.detail
    assert "holding open" in alert.detail, "the hold has to be visible, not silent"


def test_the_alert_resolves_once_the_count_clears_the_margin(db):
    printer = _printer(db)
    _rule(db, count=10, window=1440)
    _set(db, alerts__occurrence_clear_margin_pct=25, alerts__renotify_cooldown_min=0)
    _events_at(db, printer, _TEN_OVER_A_DAY)
    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 1

    # +3h: three jams have aged out, 7 remain -- below the 7.5 floor.
    res = jobs.evaluate_alerts(db, now=NOW + timedelta(hours=3))
    assert res["alerts_resolved"] == 1
    assert _open_rate_alerts(db) == []


def test_a_zero_margin_resolves_on_the_first_dip(db):
    """0 disables the margin, matching alerts.supply_deadband_pct's contract."""
    printer = _printer(db)
    _rule(db, count=10, window=1440)
    _set(db, alerts__occurrence_clear_margin_pct=0, alerts__renotify_cooldown_min=0)
    _events_at(db, printer, _TEN_OVER_A_DAY)
    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 1

    # One aged out -> 9 -> straight to resolved, no margin to hold it.
    assert jobs.evaluate_alerts(db, now=NOW + timedelta(hours=1))["alerts_resolved"] == 1


def test_a_disabled_rule_resolves_its_open_alert(db):
    printer = _printer(db)
    rule = _rule(db, count=3, window=1440)
    _burst(db, printer, 5, spacing_min=30)
    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 1

    rule.enabled = False
    db.commit()
    assert jobs.evaluate_alerts(db, now=NOW)["alerts_resolved"] == 1


# --------------------------------------------------------------------------- #
# 5. The window is actually indexed
# --------------------------------------------------------------------------- #
def test_printer_events_carries_the_composite_index_on_a_freshly_built_schema(db):
    """Declared in __table_args__, not only in migration 0037.

    Revision 0001 is ``Base.metadata.create_all()``, so an index that lives only
    in a later migration is silently absent on every fresh install -- and this
    is the index the rolling count reads.

    Reflected through the SESSION's connection, not ``inspect(engine)``. A fresh
    checkout from the pool can be a connection another test left inside an open
    read transaction, whose snapshot predates this test's ``create_all`` -- on
    SQLite that reflects as a table with no indexes at all, i.e. this assertion
    failing while the index is present. The session's own connection is the one
    the rest of the test's data lives on, so it is also the honest thing to ask.
    """
    from sqlalchemy import inspect

    indexes = {
        i["name"]: i["column_names"]
        for i in inspect(db.connection()).get_indexes("printer_events")
    }
    assert "ix_printer_events_printer_ts" in indexes
    assert indexes["ix_printer_events_printer_ts"] == ["printer_id", "ts"]


# --------------------------------------------------------------------------- #
# 6. Formatting helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "minutes,expected",
    [(45, "45m"), (60, "1h"), (360, "6h"), (1440, "1d"), (10080, "7d"), (90, "90m")],
)
def test_window_labels_read_the_way_an_operator_says_them(minutes, expected):
    assert jobs._fmt_window(minutes) == expected


# --------------------------------------------------------------------------- #
# 7. The operator surface
#
# Rules had no UI at all before this: the only way to change one was SQL. A
# rate rule is three decisions (what counts, how many, over how long) and none
# of them has a defensible default, so the form is part of the feature rather
# than a nicety on top of it.
# --------------------------------------------------------------------------- #
def _login(db, *, role=m.UserRole.admin, username="admin"):
    from fastapi.testclient import TestClient

    from central.main import app
    from central.security import hash_password

    db.add(m.User(username=username, password_hash=hash_password("pw12345678"), role=role))
    db.commit()
    cli = TestClient(app)
    resp = cli.post("/login", data={"username": username, "password": "pw12345678"},
                    follow_redirects=False)
    assert resp.status_code == 303
    return cli


_FORM = {
    "name": "Jams — more than 10 a day",
    "condition_type": "occurrence_rate",
    "scope": "global",
    "scope_id": "",
    "severity": "critical",
    "threshold": "10",
    "window_amount": "24",
    "window_unit": "hours",
    "match_code": "jam",
    "match_min_severity": "warning",
}


def _post(cli, **overrides):
    data = dict(_FORM)
    data.update(overrides)
    return cli.post("/manage/alert-rules", data=data, follow_redirects=False)


def test_the_form_creates_a_rule_the_evaluator_can_actually_run(db):
    """End to end: the form's output has to be a rule that fires, not just a row."""
    cli = _login(db)
    printer = _printer(db)

    assert _post(cli).status_code == 303
    rule = db.query(m.AlertRule).filter_by(
        condition_type=m.AlertConditionType.occurrence_rate
    ).one()
    assert rule.threshold == 10
    assert rule.window_minutes == 1440  # 24 hours, stored in minutes
    assert rule.match_code == "jam"
    assert rule.match_min_severity == m.EventSeverity.warning
    assert rule.severity == m.EventSeverity.critical

    _burst(db, printer, 11, spacing_min=60)
    assert jobs.evaluate_alerts(db, now=NOW)["alerts_opened"] == 1


def test_a_rate_rule_without_a_window_is_refused_not_stored(db):
    """A rule that quietly never matches is worse than an error message."""
    cli = _login(db)
    assert _post(cli, window_amount="").status_code == 303
    assert db.query(m.AlertRule).count() == 0


def test_a_rate_rule_without_a_count_is_refused_not_stored(db):
    cli = _login(db)
    assert _post(cli, threshold="").status_code == 303
    assert _post(cli, threshold="0").status_code == 303
    assert db.query(m.AlertRule).count() == 0


def test_an_over_long_window_is_refused_rather_than_silently_narrowed(db):
    """The operator is told, instead of getting a rule measuring a period they did not pick."""
    cli = _login(db)
    assert _post(cli, window_amount="400", window_unit="days").status_code == 303
    assert db.query(m.AlertRule).count() == 0


def test_a_scoped_rule_without_a_target_is_refused(db):
    cli = _login(db)
    assert _post(cli, scope="client", scope_id="").status_code == 303
    assert db.query(m.AlertRule).count() == 0


def test_creating_a_rule_is_audited(db):
    cli = _login(db)
    _post(cli)
    entry = db.query(m.AuditLog).filter_by(action="alert_rule.create").one()
    assert "occurrence_rate" in entry.detail
    assert "window=1440min" in entry.detail
    assert "match_code=jam" in entry.detail


def test_a_client_readonly_user_cannot_reach_the_rules_surface(db):
    """Alert rules govern the whole fleet; a customer must not see or edit them."""
    cli = _login(db, role=m.UserRole.client_readonly, username="customer")
    assert cli.get("/manage/alert-rules", follow_redirects=False).status_code == 303
    assert _post(cli).status_code == 303
    assert db.query(m.AlertRule).count() == 0


def test_an_anonymous_visitor_cannot_create_a_rule(db):
    from fastapi.testclient import TestClient

    from central.main import app

    assert _post(TestClient(app)).status_code == 303
    assert db.query(m.AlertRule).count() == 0


def test_a_hostile_match_code_is_rendered_as_text_not_markup(db):
    """Operator free-text lands in the rules table and in alert details.

    Not device-controlled, but it is stored input rendered back, which is the
    same class of hole as the SNMP strings the channels had to be fixed for.
    """
    cli = _login(db)
    _post(cli, match_code="<img src=x onerror=alert(1)>")
    page = cli.get("/manage/alert-rules")
    assert page.status_code == 200
    assert "<img src=x onerror=alert(1)>" not in page.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in page.text


def test_toggling_and_deleting_a_rule(db):
    cli = _login(db)
    _post(cli)
    rule = db.query(m.AlertRule).one()

    assert cli.post(f"/manage/alert-rules/{rule.id}/toggle",
                    follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.query(m.AlertRule).one().enabled is False

    assert cli.post(f"/manage/alert-rules/{rule.id}/delete",
                    follow_redirects=False).status_code == 303
    assert db.query(m.AlertRule).count() == 0
    assert db.query(m.AuditLog).filter_by(action="alert_rule.delete").count() == 1


def test_the_rules_page_lists_a_rate_rule_in_operator_terms(db):
    cli = _login(db)
    _post(cli, window_amount="30", window_unit="minutes", threshold="5")
    page = cli.get("/manage/alert-rules")
    assert "5× in 30m" in page.text
    assert "code contains" in page.text
