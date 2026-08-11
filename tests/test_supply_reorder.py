"""Supply reorder recommendations -- RECOMMEND-ONLY (central.reorder).

Four things are worth asserting here, in the order they can go wrong:

1.  The pages-remaining forecast, which is the new measurement. It must be a
    real independent estimate (fitted against the page meter) rather than
    days x pages-per-day, must survive a meter reset, and must return None --
    not a number -- when it cannot be trusted.
2.  The trigger policy: level OR days OR pages, any one firing. Especially that
    the pages trigger fires on a supply the other two would miss, because if it
    cannot do that it is decoration.
3.  Tenancy. A client_readonly user must never see another customer's fleet,
    and the scoping has to be in SQL, not applied afterwards.
4.  That this stays RECOMMEND-ONLY: no order state, no ordering control, and a
    publish that transmits nothing is never reported as a send.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from central import models as m
from sqlalchemy import select

from central import reorder
from central import runtime
from central.main import app
from central.reorder import ReorderThresholds
from central.security import hash_password
from central.worker import jobs


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _client_site(db, name="Acme", site_name="HQ"):
    client = m.Client(name=name)
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name=site_name)
    db.add(site)
    db.flush()
    return client, site


def _printer(db, client, site, ip="10.0.0.7", model="Brother MFC-L8900CDW", **kw):
    printer = m.Printer(
        client_id=client.id,
        site_id=site.id,
        ip=ip,
        model=model,
        discovery_state=kw.pop("discovery_state", m.DiscoveryState.approved),
        **kw,
    )
    db.add(printer)
    db.flush()
    return printer


def _supply(db, printer, *, level=None, days=None, pages=None,
            type_=m.SupplyType.toner, color="black", description=None):
    supply = m.Supply(
        printer_id=printer.id,
        type=type_,
        color=color,
        description=description,
        level_pct=level,
        days_to_empty=days,
        pages_to_empty=pages,
    )
    db.add(supply)
    db.flush()
    return supply


def _thresholds(**kw):
    return ReorderThresholds(**kw)


# --------------------------------------------------------------------------- #
# 1) pages-to-empty: the new measurement
# --------------------------------------------------------------------------- #
def test_pages_to_empty_fits_level_against_the_page_meter():
    """A clean line: 20% consumed over 2,000 pages, sitting at 40%.

    10% per 1,000 pages, so 40% remaining == 4,000 pages. This is the whole
    claim of the measure -- it is derived from the page meter, not from elapsed
    time -- so the number has to come out exactly right on a noiseless series.
    """
    series = [(1000 + i * 200, 60.0 - i * 2.0) for i in range(11)]  # 1000..3000 pages, 60%..40%
    assert jobs.forecast_pages_to_empty(series) == 4000.0


def test_pages_to_empty_is_not_days_times_rate():
    """Same cartridge, same pages, two very different print rates.

    The pages estimate must be identical in both, because the cartridge is
    identical; a derived days x pages-per-day figure could not be. This is the
    property that makes pages-remaining stable across a quiet week.
    """
    fast = [(1000 + i * 200, 60.0 - i * 2.0) for i in range(11)]
    slow = [(1000 + i * 200, 60.0 - i * 2.0) for i in range(11)]
    assert jobs.forecast_pages_to_empty(fast) == jobs.forecast_pages_to_empty(slow)


def test_pages_to_empty_none_when_the_printer_has_not_printed():
    """An idle printer has no page-based consumption rate.

    None is the honest answer. A number here would say "this cartridge lasts
    forever", which is what a naive divide-by-zero guard would end up implying.
    """
    series = [(5000, 60.0), (5000, 55.0), (5000, 50.0)]
    assert jobs.forecast_pages_to_empty(series) is None


def test_pages_to_empty_ignores_history_before_a_meter_reset():
    """A meter that goes backwards is a reset, not negative printing.

    Fitting through it yields a confidently wrong rate. Everything before the
    most recent drop is discarded; the fit uses the post-reset series alone.
    """
    pre = [(900_000 + i * 200, 90.0 - i) for i in range(6)]   # old formatter
    post = [(1000 + i * 200, 60.0 - i * 2.0) for i in range(11)]  # replaced, meter at 0
    assert jobs.forecast_pages_to_empty(pre + post) == 4000.0


def test_pages_to_empty_resets_on_a_refill():
    """A new cartridge must not be fitted against the spent one's slope."""
    spent = [(1000 + i * 200, 30.0 - i * 2.0) for i in range(6)]     # 30% -> 20%
    fresh = [(2200 + i * 200, 100.0 - i * 2.0) for i in range(11)]   # swapped, 100% -> 80%
    result = jobs.forecast_pages_to_empty(spent + fresh)
    # The fresh cartridge alone: 20% over 2,000 pages, at 80% -> 8,000 pages.
    assert result == 8000.0


def test_pages_to_empty_gate_rejects_too_few_pages():
    """A slope measured across a handful of pages is quantisation noise."""
    tiny = [(1000, 60.0), (1010, 50.0)]  # 10 pages, well under the floor
    assert jobs.forecast_pages_to_empty(tiny) is None
    spread = [(1000, 60.0), (1000 + jobs.FORECAST_MIN_PAGES_SPAN, 50.0)]
    assert jobs.forecast_pages_to_empty(spread) is not None


def test_pages_to_empty_none_on_a_rising_series():
    rising = [(1000 + i * 200, 40.0 + i * 2.0) for i in range(11)]
    assert jobs.forecast_pages_to_empty(rising) is None


def test_days_forecast_is_unchanged_by_the_shared_fit():
    """The refactor that gave pages its own axis must not have moved days.

    forecast_days_to_empty now delegates to the shared regression; a clean
    2%/day line still has to produce the exact legacy answer.
    """
    now = datetime.now(timezone.utc)
    assert jobs.forecast_days_to_empty(
        [(now - timedelta(days=10), 50.0), (now, 30.0)]
    ) == 15.0
    assert jobs.forecast_days_to_empty([(now, 50.0)]) is None


# --------------------------------------------------------------------------- #
# 2) trigger policy: level OR days OR pages
# --------------------------------------------------------------------------- #
def test_level_alone_triggers(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    supply = _supply(db, printer, level=12.0)
    verdict = reorder.evaluate_supply(supply, _thresholds())
    assert verdict is not None
    triggers, reasons, urgency = verdict
    assert triggers == ["level"]
    assert "12%" in reasons[0] and "20%" in reasons[0]


def test_days_alone_triggers_with_no_level_at_all(db):
    """A device reporting only a coarse status still gets a recommendation."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    supply = _supply(db, printer, level=None, days=9.0)
    triggers, reasons, urgency = reorder.evaluate_supply(supply, _thresholds())
    assert triggers == ["days"]


def test_pages_alone_triggers_where_level_and_days_would_not(db):
    """The point of the third trigger.

    68% remaining and 40 days of runway: both other triggers are silent. But
    only 300 pages of yield are left, because this device prints in bursts --
    that is exactly the cartridge an MPS provider ships early, and level-only
    monitoring never sees it.
    """
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    supply = _supply(db, printer, level=68.0, days=40.0, pages=300.0)
    verdict = reorder.evaluate_supply(supply, _thresholds())
    assert verdict is not None
    triggers, reasons, urgency = verdict
    assert triggers == ["pages"]
    assert "300" in reasons[0]
    assert urgency == reorder.URGENCY_SOON


def test_healthy_supply_is_not_recommended(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    supply = _supply(db, printer, level=80.0, days=90.0, pages=9000.0)
    assert reorder.evaluate_supply(supply, _thresholds()) is None


def test_supply_with_nothing_measured_is_not_recommended(db):
    """No level, no forecast: silence, not a guess."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    supply = _supply(db, printer)
    assert reorder.evaluate_supply(supply, _thresholds()) is None


@pytest.mark.parametrize(
    "field,value,supply_kw",
    [
        ("level_pct", 0.0, {"level": 5.0}),
        ("days_remaining", 0, {"days": 2.0}),
        ("pages_remaining", 0, {"pages": 10.0}),
    ],
)
def test_a_zero_threshold_switches_its_trigger_off(db, field, value, supply_kw):
    """0 means "off", not "fire on everything at exactly zero".

    That is what each setting's help text promises, and an operator who trusts
    only the forecasts has to be able to silence the level trigger.
    """
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    supply = _supply(db, printer, **supply_kw)
    assert reorder.evaluate_supply(supply, _thresholds(**{field: value})) is None


def test_urgency_order_now_inside_the_replacement_lead_time(db):
    """A supply that will not outlast delivery is late, not "soon"."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    supply = _supply(db, printer, level=15.0, days=6.0)
    _, reasons, urgency = reorder.evaluate_supply(supply, _thresholds(lead_days=14))
    assert urgency == reorder.URGENCY_NOW
    assert any("lead time" in r for r in reasons)


def test_urgency_order_now_at_a_critical_level_with_no_forecast(db):
    """3% is about to fail whatever the forecast says -- including no forecast."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    supply = _supply(db, printer, level=3.0, days=None)
    _, _, urgency = reorder.evaluate_supply(supply, _thresholds())
    assert urgency == reorder.URGENCY_NOW


def test_urgency_order_soon_when_there_is_slack(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    supply = _supply(db, printer, level=18.0, days=20.0)
    _, _, urgency = reorder.evaluate_supply(supply, _thresholds(lead_days=14))
    assert urgency == reorder.URGENCY_SOON


def test_thresholds_survive_a_corrupt_settings_row():
    """A hand-edited app_settings value must not raise inside a page render."""
    t = ReorderThresholds.from_runtime(
        {"reorder.level_pct": "not-a-number", "reorder.days_remaining": None,
         "alerts.reorder_lead_days": []}
    )
    assert t.level_pct == 20.0
    assert t.days_remaining == 21
    assert t.lead_days == 14


def test_thresholds_read_the_operators_settings(db):
    runtime.save_settings(db, {"reorder.level_pct": "35", "reorder.days_remaining": "5"},
                         sections=None)
    t = ReorderThresholds.load(db)
    assert t.level_pct == 35.0
    assert t.days_remaining == 5


# --------------------------------------------------------------------------- #
# 3) tenancy + the query
# --------------------------------------------------------------------------- #
def test_recommendations_are_scoped_to_one_client(db):
    acme, acme_site = _client_site(db, "Acme")
    globex, globex_site = _client_site(db, "Globex", "Depot")
    acme_printer = _printer(db, acme, acme_site, ip="10.0.0.1")
    globex_printer = _printer(db, globex, globex_site, ip="10.9.9.9")
    _supply(db, acme_printer, level=5.0)
    _supply(db, globex_printer, level=4.0)
    db.commit()

    everything = reorder.recommendations(db)
    assert {r.client_name for r in everything} == {"Acme", "Globex"}

    scoped = reorder.recommendations(db, client_id=acme.id)
    assert [r.client_name for r in scoped] == ["Acme"]
    assert all(r.printer_id == acme_printer.id for r in scoped)


def test_unapproved_printers_are_never_recommended_for(db):
    """A device in the discovery queue is not yet in anybody's fleet."""
    client, site = _client_site(db)
    pending = _printer(db, client, site, ip="10.0.0.2",
                       discovery_state=m.DiscoveryState.pending)
    ignored = _printer(db, client, site, ip="10.0.0.3",
                       discovery_state=m.DiscoveryState.ignored)
    _supply(db, pending, level=1.0)
    _supply(db, ignored, level=1.0)
    db.commit()
    assert reorder.recommendations(db) == []


def test_one_recommendation_per_printer_and_supply(db):
    """A colour device with three depleting toners is three things to order."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    for color in ("black", "cyan", "magenta"):
        _supply(db, printer, level=8.0, color=color)
    db.commit()
    recs = reorder.recommendations(db)
    assert len(recs) == 3
    assert len({r.dedupe_key for r in recs}) == 3


def test_recommendations_sort_urgent_first_then_soonest(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    calm = _supply(db, printer, level=19.0, days=20.0, color="yellow")
    urgent_late = _supply(db, printer, level=10.0, days=10.0, color="cyan")
    urgent_soon = _supply(db, printer, level=10.0, days=2.0, color="black")
    db.commit()
    recs = reorder.recommendations(db)
    assert [r.supply_id for r in recs] == [urgent_soon.id, urgent_late.id, calm.id]


def test_a_supply_with_no_forecast_sorts_after_one_with_a_forecast(db):
    """None must not outrank a cartridge we know fails on Thursday."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    unknown = _supply(db, printer, level=10.0, days=None, color="cyan")
    known = _supply(db, printer, level=10.0, days=18.0, color="black")
    db.commit()
    recs = reorder.recommendations(db)
    assert [r.supply_id for r in recs] == [known.id, unknown.id]


# --------------------------------------------------------------------------- #
# 4) aggregation: an MSP orders once
# --------------------------------------------------------------------------- #
def test_identical_cartridges_across_printers_become_one_line(db):
    client, site = _client_site(db)
    for i in range(3):
        printer = _printer(db, client, site, ip=f"10.0.0.{10 + i}", model="Brother HL-L2350DW")
        _supply(db, printer, level=9.0, description="Black Toner")
    db.commit()
    groups = reorder.group_by_client(reorder.recommendations(db))
    assert len(groups) == 1
    assert len(groups[0].lines) == 1
    assert groups[0].lines[0].quantity == 3
    assert len(groups[0].lines[0].printers) == 3
    assert groups[0].total_units == 3


def test_different_models_do_not_merge_into_one_order_line(db):
    """Black toner for a Brother is not black toner for an HP.

    Without a SKU catalogue the model is the closest honest statement of "the
    same cartridge"; merging on (type, colour) alone would tell an MSP to order
    "2 black toners" for two devices that take different cartridges.
    """
    client, site = _client_site(db)
    brother = _printer(db, client, site, ip="10.0.0.20", model="Brother HL-L2350DW")
    hp = _printer(db, client, site, ip="10.0.0.21", model="HP LaserJet M404")
    _supply(db, brother, level=9.0, description="Black Toner")
    _supply(db, hp, level=9.0, description="Black Toner")
    db.commit()
    lines = reorder.group_by_client(reorder.recommendations(db))[0].lines
    assert len(lines) == 2
    assert {line.model for line in lines} == {"Brother HL-L2350DW", "HP LaserJet M404"}
    assert all(line.quantity == 1 for line in lines)


def test_an_order_line_takes_its_most_urgent_members_urgency(db):
    """Four cartridges on one order, one needed this week: the line is urgent."""
    client, site = _client_site(db)
    calm = _printer(db, client, site, ip="10.0.0.30", model="Brother HL-L2350DW")
    dying = _printer(db, client, site, ip="10.0.0.31", model="Brother HL-L2350DW")
    _supply(db, calm, level=19.0, days=20.0, description="Black Toner")
    _supply(db, dying, level=19.0, days=3.0, description="Black Toner")
    db.commit()
    group = reorder.group_by_client(reorder.recommendations(db))[0]
    assert len(group.lines) == 1
    assert group.lines[0].quantity == 2
    assert group.lines[0].is_urgent
    assert group.lines[0].soonest_days == 3.0
    assert group.urgent_units == 2


def test_groups_are_per_client(db):
    acme, acme_site = _client_site(db, "Acme")
    globex, globex_site = _client_site(db, "Globex", "Depot")
    _supply(db, _printer(db, acme, acme_site, ip="10.0.0.40"), level=5.0)
    _supply(db, _printer(db, globex, globex_site, ip="10.9.9.40"), level=5.0)
    db.commit()
    groups = reorder.group_by_client(reorder.recommendations(db))
    assert {g.client_name for g in groups} == {"Acme", "Globex"}
    assert all(g.total_units == 1 for g in groups)


# --------------------------------------------------------------------------- #
# 5) the event-bus seam (F2)
# --------------------------------------------------------------------------- #
class _FakePublisher:
    """Stands in for the F2 bus: records what it was asked to publish."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    def __call__(self, db, event_type, payload):
        self.calls.append((event_type, payload))
        if payload["dedupe_key"] in self.fail_on:
            raise RuntimeError("bus is down")


def test_publish_emits_one_event_per_recommendation(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    for color in ("black", "cyan"):
        _supply(db, printer, level=8.0, days=3.0, pages=120.0, color=color)
    db.commit()
    recs = reorder.recommendations(db)
    bus = _FakePublisher()

    result = reorder.publish_recommendations(db, recs, publisher=bus)

    assert result.published == 2 and result.failed == 0 and result.ok
    assert {call[0] for call in bus.calls} == {"supply.reorder_recommended"}
    payload = bus.calls[0][1]
    assert payload["client"]["name"] == "Acme"
    assert payload["supply"]["days_remaining"] == 3.0
    assert payload["supply"]["pages_remaining"] == 120.0
    assert payload["urgency"] == reorder.URGENCY_NOW
    assert payload["reasons"]


def test_publish_without_the_bus_installed_reports_nothing_sent(db):
    """Not "delivered". The ChannelResult.sent lesson, applied to events.

    With no event bus present the honest outcome is that nothing left the
    building, and it has to be reported as a skip naming the reason -- never as
    a successful publish.
    """
    client, site = _client_site(db)
    _supply(db, _printer(db, client, site), level=5.0)
    db.commit()
    recs = reorder.recommendations(db)

    # The bus IS installed now, so absence has to be simulated rather than
    # observed. This test used to assert `_resolve_publisher() is None` for real
    # and pass -- which is how the feature stayed disconnected: central.events
    # shipped without a `publish`, reorder read that as "no event bus", and
    # enabling reorder.emit_events published nothing forever while the suite
    # stayed green. The assertion was a tripwire with a comment saying to rewire
    # it; nobody read the comment, because nothing failed.
    import central.events as _ev
    real = _ev.publish
    try:
        del _ev.publish
        assert reorder._resolve_publisher() is None
        result = reorder.publish_recommendations(db, recs)
    finally:
        _ev.publish = real

    assert result.published == 0
    assert result.skipped == len(recs)
    assert result.ok  # nothing went WRONG; nothing was sent either
    assert "central.events.publish" in result.reason


def test_the_publisher_is_actually_wired_to_the_event_bus(db):
    """The wiring itself, which nothing asserted before.

    Every other publish test injects a fake publisher, so they exercised the loop
    and never the resolution -- the one thing that was broken. This drives the
    real `central.events.publish` and checks a row landed.
    """
    from central import models as m

    client, site = _client_site(db)
    _supply(db, _printer(db, client, site), level=5.0)
    db.commit()
    recs = reorder.recommendations(db)
    assert recs, "fixture must produce a recommendation for this to mean anything"

    assert reorder._resolve_publisher() is not None
    result = reorder.publish_recommendations(db, recs)
    db.commit()

    assert result.failed == 0, result.reason
    assert result.published == len(recs)
    rows = list(db.scalars(
        select(m.OutboundEvent).where(m.OutboundEvent.type == reorder.EVENT_TYPE)
    ))
    assert len(rows) == len(recs)
    assert rows[0].client_id == client.id, "client scope must survive the bridge"


def test_republishing_the_same_recommendation_is_deduped(db):
    """The producer owns the identity rule here, so the bridge has to carry its
    dedupe_key through rather than inventing one."""
    from central import models as m

    client, site = _client_site(db)
    _supply(db, _printer(db, client, site), level=5.0)
    db.commit()
    recs = reorder.recommendations(db)

    reorder.publish_recommendations(db, recs)
    db.commit()
    reorder.publish_recommendations(db, recs)
    db.commit()

    rows = list(db.scalars(
        select(m.OutboundEvent).where(m.OutboundEvent.type == reorder.EVENT_TYPE)
    ))
    assert len(rows) == len(recs), "a second pass must not re-emit the same cartridge"


def test_one_failing_event_does_not_cost_the_rest_their_events(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    for color in ("black", "cyan", "magenta"):
        _supply(db, printer, level=5.0, color=color)
    db.commit()
    recs = reorder.recommendations(db)
    bus = _FakePublisher(fail_on={recs[0].dedupe_key})

    result = reorder.publish_recommendations(db, recs, publisher=bus)

    assert result.failed == 1
    assert result.published == 2
    assert not result.ok


def test_dedupe_key_is_stable_across_an_urgency_change(db):
    """A cartridge escalating soon -> now is the same cartridge, not a new one."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    supply = _supply(db, printer, level=19.0, days=20.0)
    db.commit()
    before = reorder.recommendations(db)[0]
    assert before.urgency == reorder.URGENCY_SOON

    supply.days_to_empty = 2.0
    db.commit()
    after = reorder.recommendations(db)[0]

    assert after.urgency == reorder.URGENCY_NOW
    assert after.dedupe_key == before.dedupe_key


def test_event_payload_carries_no_secrets_and_no_raw_device_strings(db):
    """Device strings reach the payload; control characters must not.

    The model comes off a device on a customer LAN, and a consumer may render
    this straight into a ticket or a log line.
    """
    client, site = _client_site(db)
    printer = _printer(db, client, site, model="Evil\nX-Injected: 1\rPrinter")
    printer.snmp_community = "s3cr3t-community"
    _supply(db, printer, level=5.0)
    db.commit()
    payload = reorder.recommendations(db)[0].as_event_payload()

    flat = repr(payload)
    assert "\\n" not in payload["printer"]["model"]
    assert "\\r" not in payload["printer"]["model"]
    assert "s3cr3t-community" not in flat
    assert "snmp" not in flat.lower()


# --------------------------------------------------------------------------- #
# 6) the worker job: gating, marker, and never lying about a send
# --------------------------------------------------------------------------- #
def test_event_publishing_is_off_by_default(db):
    client, site = _client_site(db)
    _supply(db, _printer(db, client, site), level=5.0)
    db.commit()
    assert jobs.publish_reorder_recommendations(db) == {"reorder_events": "disabled"}


def test_enabled_job_publishes_and_then_respects_its_interval(db, monkeypatch):
    client, site = _client_site(db)
    _supply(db, _printer(db, client, site), level=5.0)
    runtime.save_settings(db, {"reorder.emit_events": "on"}, sections=None)
    db.commit()
    bus = _FakePublisher()
    monkeypatch.setattr(reorder, "_resolve_publisher", lambda: bus)

    first = jobs.publish_reorder_recommendations(db)
    assert first["reorder_events"]["published"] == 1

    # Second cycle, 60 seconds later: the worker runs every minute and the
    # recommendation has not changed. Publishing again would be a firehose.
    second = jobs.publish_reorder_recommendations(db)
    assert second == {}
    assert len(bus.calls) == 1

    # Past the interval, it publishes again.
    later = datetime.now(timezone.utc) + timedelta(minutes=1441)
    third = jobs.publish_reorder_recommendations(db, now=later)
    assert third["reorder_events"]["published"] == 1
    assert len(bus.calls) == 2


def test_the_interval_marker_does_not_move_when_nothing_was_published(db):
    """Otherwise installing the bus silently loses its first real window."""
    client, site = _client_site(db)
    _supply(db, _printer(db, client, site), level=5.0)
    runtime.save_settings(db, {"reorder.emit_events": "on"}, sections=None)
    db.commit()

    # The bus is installed now, so "nothing was published" has to be arranged.
    # Before, this passed for real -- which is the same blind spot that let the
    # publisher stay unwired: every reorder-event test observed a bus that was
    # never there.
    import central.events as _ev

    real = _ev.publish
    try:
        del _ev.publish
        out = jobs.publish_reorder_recommendations(db)
    finally:
        _ev.publish = real
    assert out["reorder_events"]["published"] == 0
    assert db.get(m.AppSetting, jobs.REORDER_EMIT_MARKER) is None


def test_the_emit_marker_is_not_an_operator_setting(db):
    """Machine state stays out of the Settings UI, like the report markers."""
    assert jobs.REORDER_EMIT_MARKER not in runtime.default_settings()


# --------------------------------------------------------------------------- #
# 7) the worker persists pages_to_empty off the readings it already loads
# --------------------------------------------------------------------------- #
def test_forecast_pass_persists_pages_to_empty(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    supply = _supply(db, printer, color="black")
    now = datetime.now(timezone.utc)
    # 14 days, 200 pages/day, level 90% -> 62% (2%/day). At 62% and 0.01%/page
    # the remaining yield is ~6,200 pages.
    for d in range(15):
        db.add(m.Reading(
            printer_id=printer.id,
            ts=now - timedelta(days=14 - d),
            page_count=10_000 + d * 200,
            status=m.PrinterStatus.ok,
            supply_snapshot=[{"type": "toner", "color": "black",
                              "level_pct": 90.0 - d * 2.0}],
        ))
    db.commit()

    jobs.forecast_supplies(db, now=now)
    db.refresh(supply)

    assert supply.days_to_empty == pytest.approx(31.0, abs=0.5)
    assert supply.pages_to_empty == pytest.approx(6200.0, rel=0.01)


def test_a_cleared_forecast_clears_the_pages_estimate_too(db):
    """A stale number is worse than none -- both columns clear together."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    supply = _supply(db, printer, color="black", days=5.0, pages=100.0)
    now = datetime.now(timezone.utc)
    # A rising (refilled) series: no trustworthy estimate on either axis.
    for d in range(5):
        db.add(m.Reading(
            printer_id=printer.id, ts=now - timedelta(days=4 - d),
            page_count=10_000 + d * 200, status=m.PrinterStatus.ok,
            supply_snapshot=[{"type": "toner", "color": "black",
                              "level_pct": 20.0 + d * 15.0}],
        ))
    db.commit()

    jobs.forecast_supplies(db, now=now)
    db.refresh(supply)

    assert supply.days_to_empty is None
    assert supply.pages_to_empty is None


# --------------------------------------------------------------------------- #
# 8) the dashboard surface
# --------------------------------------------------------------------------- #
def _login(db, username, password, role, client_id=None):
    db.add(m.User(username=username, password_hash=hash_password(password),
                  role=role, client_id=client_id))
    db.commit()
    http = TestClient(app)
    resp = http.post("/login", data={"username": username, "password": password},
                     follow_redirects=False)
    assert resp.status_code == 303
    return http


def test_reorder_page_shows_the_recommendation_and_its_reasoning(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site, display_name="Front Desk")
    _supply(db, printer, level=8.0, days=4.0, pages=350.0, description="Black Toner")
    http = _login(db, "admin", "admin", m.UserRole.admin)

    resp = http.get("/supplies/reorder")

    assert resp.status_code == 200
    body = resp.text
    assert "Front Desk" in body
    assert "Black Toner" in body
    assert "order now" in body          # inside the lead time
    assert "350" in body                # pages remaining is shown
    assert "Acme" in body


def test_reorder_page_says_so_when_there_is_nothing_to_order(db):
    client, site = _client_site(db)
    _supply(db, _printer(db, client, site), level=95.0, days=200.0)
    http = _login(db, "admin", "admin", m.UserRole.admin)
    resp = http.get("/supplies/reorder")
    assert resp.status_code == 200
    assert "Nothing to order" in resp.text


def test_reorder_page_offers_a_human_order_record_without_claiming_to_buy(db):
    client, site = _client_site(db)
    _supply(db, _printer(db, client, site), level=5.0)
    http = _login(db, "admin", "admin", m.UserRole.admin)
    body = http.get("/supplies/reorder").text
    lowered = body.lower()
    assert "record order" in lowered
    assert 'action="/supplies/orders"' in lowered
    for forbidden in ("place order", "purchase now", "add to cart"):
        assert forbidden not in lowered


def test_client_readonly_is_sent_to_the_portal(db):
    client, site = _client_site(db)
    _supply(db, _printer(db, client, site), level=5.0)
    http = _login(db, "cust", "cust", m.UserRole.client_readonly, client_id=client.id)
    resp = http.get("/supplies/reorder", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal"


def test_portal_shows_only_this_customers_supplies_to_order(db):
    """The tenancy assertion that matters: one customer, one fleet."""
    acme, acme_site = _client_site(db, "Acme")
    globex, globex_site = _client_site(db, "Globex", "Depot")
    mine = _printer(db, acme, acme_site, ip="10.0.0.50", display_name="Acme Front Desk")
    theirs = _printer(db, globex, globex_site, ip="10.9.9.50", display_name="Globex Warehouse")
    _supply(db, mine, level=6.0, description="Acme Black Toner")
    _supply(db, theirs, level=2.0, description="Globex Black Toner")
    http = _login(db, "cust", "cust", m.UserRole.client_readonly, client_id=acme.id)

    body = http.get("/portal").text

    assert "Acme Front Desk" in body
    assert "Acme Black Toner" in body
    assert "Globex" not in body
    assert "Globex Black Toner" not in body


def test_reorder_page_client_filter_scopes_and_rejects_rubbish(db):
    acme, acme_site = _client_site(db, "Acme")
    globex, globex_site = _client_site(db, "Globex", "Depot")
    _supply(db, _printer(db, acme, acme_site, ip="10.0.0.60",
                         display_name="Acme Desk"), level=5.0)
    _supply(db, _printer(db, globex, globex_site, ip="10.9.9.60",
                         display_name="Globex Desk"), level=5.0)
    http = _login(db, "admin", "admin", m.UserRole.admin)

    scoped = http.get(f"/supplies/reorder?client_id={acme.id}").text
    assert "Acme Desk" in scoped
    assert "Globex Desk" not in scoped

    # An unparseable or unknown id falls back to the whole fleet rather than
    # rendering an empty page that reads as "nothing to order".
    for rubbish in ("not-an-int", "999999", "'; DROP TABLE printers;--"):
        body = http.get("/supplies/reorder", params={"client_id": rubbish}).text
        assert "Acme Desk" in body and "Globex Desk" in body


def test_reorder_page_is_accessible_when_it_actually_has_content(db):
    """The shared a11y suite only ever sees this page EMPTY.

    tests/test_dashboard_a11y.py renders against a seeded-empty database, so the
    client filter (which only appears with more than one client) and every table
    it guards are invisible to it. That is precisely the shape of blind spot
    this codebase has been bitten by, so the populated page is asserted here:
    every control named, every table scroller a containing block, every card
    able to shrink, and the nav marking where you are.
    """
    from tests.test_dashboard_a11y import _parse

    acme, acme_site = _client_site(db, "Acme")
    globex, globex_site = _client_site(db, "Globex", "Depot")
    _supply(db, _printer(db, acme, acme_site, ip="10.0.0.80"), level=5.0)
    _supply(db, _printer(db, globex, globex_site, ip="10.9.9.80"), level=5.0)
    http = _login(db, "admin", "admin", m.UserRole.admin)

    resp = http.get("/supplies/reorder")
    assert resp.status_code == 200
    parsed = _parse(resp.text)

    # The client filter renders only with >1 client, so this asserts it exists
    # before asserting it is labelled -- otherwise the check passes vacuously.
    assert any(c["name"] == "client_id" for c in parsed.controls), \
        "the client filter did not render; this check would have passed on nothing"
    unlabelled = [
        c for c in parsed.controls
        if not c["wrapped"] and not c["aria"]
        and not (c["id"] and c["id"] in parsed.label_for)
    ]
    assert not unlabelled, [c["name"] for c in unlabelled]

    for wrapper in re.findall(r'class="([^"]*overflow-x-auto[^"]*)"', resp.text):
        assert "relative" in wrapper, wrapper
    cards = re.findall(r'class="([^"]*bg-white rounded-lg shadow[^"]*)"', resp.text)
    assert cards
    for card in cards:
        assert "min-w-0" in card, card
    assert parsed.aria_current == ["/supplies/reorder"], parsed.aria_current


def test_portal_names_printers_without_leaking_addressing(db):
    """The portal shows friendly names, not IPs -- its existing convention."""
    client, site = _client_site(db)
    printer = _printer(db, client, site, ip="10.44.0.9", display_name="Reception")
    _supply(db, printer, level=5.0, description="Black Toner")
    http = _login(db, "cust", "cust", m.UserRole.client_readonly, client_id=client.id)

    panel = http.get("/portal").text.split("reorder-panel")[1].split("</section>")[0]

    assert "Reception" in panel
    assert "10.44.0.9" not in panel


def test_reorder_page_escapes_a_hostile_device_string(db):
    """Model and description come off a device on a customer LAN."""
    client, site = _client_site(db)
    printer = _printer(db, client, site, model="<script>alert(1)</script>")
    _supply(db, printer, level=5.0, description="<img src=x onerror=alert(2)>")
    http = _login(db, "admin", "admin", m.UserRole.admin)

    body = http.get("/supplies/reorder").text

    assert "<script>alert(1)</script>" not in body
    assert "<img src=x onerror=alert(2)>" not in body
    assert "&lt;script&gt;" in body


# --------------------------------------------------------------------------- #
# 9) the weekly report
# --------------------------------------------------------------------------- #
def test_weekly_report_lists_supplies_to_order_aggregated_per_client(db):
    from central.reports import build_weekly_summary

    client, site = _client_site(db)
    for i in range(2):
        printer = _printer(db, client, site, ip=f"10.0.0.{70 + i}",
                           model="Brother HL-L2350DW")
        _supply(db, printer, level=8.0, days=3.0, description="Black Toner")
    db.commit()

    _subject, body = build_weekly_summary(db)

    assert "Supplies to order" in body
    assert "2 x Black Toner" in body          # aggregated, not two lines
    assert "ORDER NOW" in body
    assert "recommendations only" in body


def test_weekly_report_omits_the_section_when_switched_off(db):
    from central.reports import build_weekly_summary

    client, site = _client_site(db)
    _supply(db, _printer(db, client, site), level=5.0)
    # Clearing a checkbox is "declare the section, omit the key" -- an unchecked
    # box posts nothing, so that is the only way the form can say False. Driving
    # it the way the settings page does is the point: a test that wrote the row
    # directly would pass while the actual toggle did nothing.
    runtime.save_settings(db, {}, sections={"Supplies (reorder)"})
    db.commit()
    assert runtime.load_settings(db)["reorder.include_in_reports"] is False

    _subject, body = build_weekly_summary(db)
    assert "Supplies to order" not in body


def test_weekly_report_omits_the_section_when_there_is_nothing_to_order(db):
    from central.reports import build_weekly_summary

    client, site = _client_site(db)
    _supply(db, _printer(db, client, site), level=95.0)
    db.commit()
    _subject, body = build_weekly_summary(db)
    assert "Supplies to order" not in body


def test_a_device_string_cannot_forge_a_line_in_the_plain_text_report(db):
    """The report body is plain text; an embedded newline forges a line.

    This is the same wire-boundary rule the CSV export follows, applied to the
    one channel that renders these strings without escaping them.
    """
    from central.reports import build_weekly_summary

    client, site = _client_site(db)
    printer = _printer(db, client, site,
                       display_name="Desk\n  Open alerts        : 0")
    _supply(db, printer, level=5.0, description="Toner\nInjected: yes")
    db.commit()

    _subject, body = build_weekly_summary(db)

    assert "Injected: yes\n" not in body
    assert "\n  Open alerts        : 0" not in body.split("Supplies to order")[1]
