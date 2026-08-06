"""Yield-gap and non-OEM detection (central.supply_yield).

The thing this feature can get wrong is not "the arithmetic is off by 3%". It is
telling an MSP their customer buys counterfeit toner when they do not. So the
tests are ordered by how expensive the failure is:

1.  **The cartridge-change signal is ONE rule**, shared with the depletion
    forecast. Two copies that disagree would accumulate a cartridge's pages
    against the wrong cartridge, and nothing would report it.
2.  **The measurement**: pages between replacements, meter-reset-safe,
    incremental across passes, and reading rollups where the raw readings have
    aged out -- because a drum lasts longer than the 90-day raw window.
3.  **Confidence**: too few cartridges is INSUFFICIENT DATA, never a verdict; a
    missing expectation is NO EXPECTATION, never "within expected". Absence is
    not a finding and it is not a clean bill of health.
4.  **The accusation**: the threshold is settable, defaults conservatively, and
    a normal cartridge beside a short one is not swept up with it.
5.  **Tenancy and authorization**, as for every staff report.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central import runtime
from central import supply_yield as sy
from central.main import app
from central.security import hash_password
from central.supplies import refill_boundaries
from central.worker import jobs

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


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


def _printer(db, client, site, ip="10.0.0.7", model="Brother MFC-L8900CDW series", **kw):
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


def _cartridges(
    db,
    printer,
    *,
    count,
    pages_each,
    start_pages=10_000,
    start_ts=None,
    steps=20,
    color="black",
    supply_type="toner",
    step_hours=12,
):
    """Write a REAL depleting reading series: N cartridges of a known yield.

    Each cartridge runs 100% -> 0% over ``steps`` intervals while the page meter
    climbs ``pages_each``. The next cartridge starts at 100% with the meter
    unchanged, which is exactly what a replacement looks like on the wire.

    Returns the final page count so a caller can keep going.
    """
    ts = start_ts or (NOW - timedelta(days=90))
    pages = start_pages
    per_step = pages_each // steps
    for _cart in range(count):
        for step in range(steps + 1):
            db.add(m.Reading(
                printer_id=printer.id,
                ts=ts,
                page_count=pages,
                supply_snapshot=[{
                    "type": supply_type,
                    "color": color,
                    "level_pct": 100.0 - step * (100.0 / steps),
                }],
            ))
            ts += timedelta(hours=step_hours)
            if step < steps:
                pages += per_step
    return pages, ts


def _scan(db, printer, now=NOW):
    result = sy.scan_printer(db, printer, now=now)
    db.commit()
    return result


def _cycles(db, printer):
    return list(
        db.query(m.SupplyCycle)
        .filter(m.SupplyCycle.printer_id == printer.id)
        .order_by(m.SupplyCycle.id)
        .all()
    )


def _expect(db, tag, pages, supply_type="toner", color=""):
    row = m.SupplyYieldExpectation(
        model_tag=tag, supply_type=supply_type, color=color, expected_pages=pages
    )
    db.add(row)
    db.flush()
    return row


#: The section the yield settings live in. ``save_settings`` records False for a
#: bool by its ABSENCE from the form -- an unchecked box posts nothing -- so a
#: test that wants one flag on and another off has to post the section, not the
#: pair. Passing ``{"yield.enabled": False}`` would set it to *True*.
YIELD_SECTION = ["Supplies (yield)"]


def _set_yield(db, **flags):
    """Post the whole Supplies (yield) section with the named bools ticked."""
    form = {key: "on" for key, value in flags.items() if value}
    runtime.save_settings(db, form, sections=YIELD_SECTION)


def _login(db, role=m.UserRole.admin, username="admin"):
    db.add(m.User(username=username, password_hash=hash_password("pw"), role=role))
    db.commit()
    client = TestClient(app)
    resp = client.post(
        "/login", data={"username": username, "password": "pw"}, follow_redirects=False
    )
    assert resp.status_code == 303
    return client


# --------------------------------------------------------------------------- #
# 1. One cartridge-change rule, shared with the forecast
# --------------------------------------------------------------------------- #
def test_refill_boundaries_finds_every_replacement_not_just_the_last():
    """The forecast only ever needed the last one. Yield needs all of them.

    That is the whole reason the rule was extracted rather than copied: a second
    implementation could disagree about where a cartridge started, and a
    cartridge's pages would be counted against its predecessor with nothing
    anywhere reporting it.
    """
    levels = [80.0, 40.0, 5.0, 100.0, 60.0, 10.0, 95.0, 50.0]
    assert refill_boundaries(levels) == [3, 6]


def test_a_small_rise_is_noise_not_a_new_cartridge():
    """Devices round, and a shaken cartridge genuinely reads a point higher."""
    assert refill_boundaries([40.0, 43.0, 41.0, 44.0]) == []
    assert refill_boundaries([40.0, 46.0]) == [1]  # 6 > the 5-point tolerance


def test_a_missing_level_is_silence_not_a_step():
    """A None between 4% and 100% is one replacement, not two steps of nothing."""
    assert refill_boundaries([50.0, None, 4.0, None, 100.0]) == [4]


def test_the_forecast_uses_the_same_rule():
    """Wired, not merely available -- the forecast must not keep a private copy.

    Fitting through the whole series would average a spent cartridge's slope
    against its replacement's and produce a confidently wrong days-to-empty.
    """
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    series = [
        (base, 80.0), (base + timedelta(days=2), 40.0), (base + timedelta(days=4), 5.0),
        (base + timedelta(days=5), 100.0),
        (base + timedelta(days=8), 80.0), (base + timedelta(days=11), 60.0),
    ]
    # Post-refill segment: 40 points over 6 days -> ~6.67/day, 60 left -> ~9 days.
    assert jobs.forecast_days_to_empty(series) == pytest.approx(9.0, abs=0.2)


# --------------------------------------------------------------------------- #
# 2. The measurement
# --------------------------------------------------------------------------- #
def test_pages_between_replacements_is_the_yield(db):
    """The headline number, against a series built to a known answer."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=4, pages_each=2400)
    db.commit()

    result = _scan(db, printer)
    assert len(result.closed) == 3  # the fourth cartridge is still fitted

    cycles = _cycles(db, printer)
    complete = [c for c in cycles if c.complete]
    assert len(complete) == 3
    for cycle in complete:
        assert cycle.pages == 2400
        assert sy.consumed_pct(cycle) == pytest.approx(100.0)
        assert sy.observed_yield(cycle) == pytest.approx(2400.0)
    assert [c for c in cycles if not c.complete][0].ended_at is None


def test_a_partly_used_cartridge_scales_to_a_full_one(db):
    """A cartridge run 100 -> 40 delivering 600 pages implies ~1000 for a full one.

    Without the scaling every early swap would look like a short cartridge, which
    is a false accusation manufactured by our own arithmetic.
    """
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    ts = NOW - timedelta(days=30)
    for level, pages in ((100.0, 5000), (70.0, 5300), (40.0, 5600), (100.0, 5600)):
        db.add(m.Reading(
            printer_id=printer.id, ts=ts, page_count=pages,
            supply_snapshot=[{"type": "toner", "color": "black", "level_pct": level}],
        ))
        ts += timedelta(days=2)
    db.commit()
    _scan(db, printer)

    cycle = [c for c in _cycles(db, printer) if c.complete][0]
    assert cycle.pages == 600
    assert sy.consumed_pct(cycle) == pytest.approx(60.0)
    assert sy.observed_yield(cycle) == pytest.approx(1000.0)


def test_a_meter_reset_contributes_zero_not_a_negative_cartridge(db):
    """A reflashed formatter drops the meter. Subtracting endpoints would invent
    a cartridge that yielded minus forty thousand pages -- and a NEGATIVE yield
    read as a shortfall is the false accusation this whole module is built to
    avoid."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    ts = NOW - timedelta(days=20)
    # 40k -> 40.5k, then the meter resets to 100 and climbs to 300.
    for level, pages in ((100.0, 40_000), (60.0, 40_500), (30.0, 100), (5.0, 300),
                         (100.0, 400)):
        db.add(m.Reading(
            printer_id=printer.id, ts=ts, page_count=pages,
            supply_snapshot=[{"type": "toner", "color": "black", "level_pct": level}],
        ))
        ts += timedelta(days=2)
    db.commit()
    _scan(db, printer)

    cycle = [c for c in _cycles(db, printer) if c.complete][0]
    assert cycle.pages == 700  # 500 before the reset + 200 after; never negative
    assert sy.observed_yield(cycle) > 0


def test_a_second_pass_extends_rather_than_recounting(db):
    """The watermark is the whole reason this is cheap. Re-reading a poll would
    add its pages to the cartridge a second time on every worker cycle."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    pages, ts = _cartridges(db, printer, count=1, pages_each=1000)
    db.commit()
    _scan(db, printer)
    first = _cycles(db, printer)[-1]
    pages_after_one_pass = first.pages

    # Nothing new: a re-run must be a no-op, not a second helping.
    result = _scan(db, printer)
    assert result.points == 0
    db.refresh(first)
    assert first.pages == pages_after_one_pass

    # New readings extend the SAME cycle rather than opening another.
    for extra in (400, 800):
        db.add(m.Reading(
            printer_id=printer.id, ts=ts, page_count=pages + extra,
            supply_snapshot=[{"type": "toner", "color": "black", "level_pct": 0.0}],
        ))
        ts += timedelta(hours=12)
    db.commit()
    _scan(db, printer)
    assert len(_cycles(db, printer)) == 1
    db.refresh(first)
    assert first.pages == pages_after_one_pass + 800


def test_a_replacement_across_two_passes_is_still_seen(db):
    """The boundary test carries the previous pass's level forward. Without that,
    a cartridge changed between two scans would never be detected at all."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    ts = NOW - timedelta(days=10)
    db.add(m.Reading(
        printer_id=printer.id, ts=ts, page_count=1000,
        supply_snapshot=[{"type": "toner", "color": "black", "level_pct": 8.0}],
    ))
    db.commit()
    _scan(db, printer)
    assert len(_cycles(db, printer)) == 1

    db.add(m.Reading(
        printer_id=printer.id, ts=ts + timedelta(days=1), page_count=1010,
        supply_snapshot=[{"type": "toner", "color": "black", "level_pct": 100.0}],
    ))
    db.commit()
    _scan(db, printer)
    cycles = _cycles(db, printer)
    assert len(cycles) == 2
    assert cycles[0].complete and cycles[0].ended_at is not None


def test_history_older_than_the_raw_window_comes_from_the_rollup(db):
    """A drum lasts a year; raw readings are kept 90 days. Reading only
    ``readings`` would make the longest-lived consumables the unmeasurable ones
    -- which is why ReadingRollup carries per-supply levels at all."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)

    # Two rollup days, 200 days back: a full cartridge and its replacement.
    for offset, (level, pages) in enumerate(((100.0, 1000), (4.0, 3000), (100.0, 3000))):
        day = (NOW - timedelta(days=200 - offset)).date()
        db.add(m.ReadingRollup(
            printer_id=printer.id,
            day=day,
            readings_count=1,
            first_ts=datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
            last_ts=datetime(day.year, day.month, day.day, 23, 0, tzinfo=timezone.utc),
            page_count=pages,
            supply_snapshot=[{"type": "toner", "color": "black", "level_pct": level}],
        ))
    db.commit()
    _scan(db, printer)

    complete = [c for c in _cycles(db, printer) if c.complete]
    assert len(complete) == 1
    assert complete[0].pages == 2000


def test_old_readings_with_no_rollup_yet_are_still_measured(db):
    """Found by the end-to-end smoke, and invisible to every unit test above it.

    Deletion ships OFF and the rollup pass works forward from a watermark a
    bounded number of printer-days per cycle. So a real install has raw rows for
    days below the cutoff with no rollup covering them -- and reading only
    rollups there discarded that history silently. The cartridges with the most
    history were measured over the least of it, which is exactly backwards.
    """
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    # 150 days back: below the 90-day cutoff, and no rollup exists.
    _cartridges(db, printer, count=4, pages_each=3000,
                start_ts=NOW - timedelta(days=150))
    db.commit()
    assert db.query(m.ReadingRollup).count() == 0

    _scan(db, printer)
    complete = [c for c in _cycles(db, printer) if c.complete]
    assert len(complete) == 3
    for cycle in complete:
        assert cycle.pages == 3000
        assert sy.observed_yield(cycle) == pytest.approx(3000.0)


def test_a_rolled_up_day_is_not_also_read_from_its_raw_rows(db):
    """The other half of the same rule: one day, one source. Both would credit a
    cartridge with twice the pages it printed."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    old = NOW - timedelta(days=150)
    day = old.date()
    for hour, (level, pages) in enumerate(((100.0, 1000), (40.0, 1600))):
        db.add(m.Reading(
            printer_id=printer.id, ts=old + timedelta(hours=hour), page_count=pages,
            supply_snapshot=[{"type": "toner", "color": "black", "level_pct": level}],
        ))
    db.add(m.ReadingRollup(
        printer_id=printer.id,
        day=day,
        readings_count=2,
        first_ts=old,
        last_ts=old + timedelta(hours=1),
        page_count=1600,
        supply_snapshot=[{"type": "toner", "color": "black", "level_pct": 40.0}],
    ))
    db.commit()
    _scan(db, printer)

    cycle = _cycles(db, printer)[0]
    assert cycle.readings_count == 1   # the rollup's point only
    assert cycle.pages == 0            # a single point has no delta to add


def test_the_rollup_and_raw_windows_do_not_overlap(db):
    """Both sources cover the same day only if the partition is wrong, and the
    symptom would be a cartridge credited with twice the pages it printed."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    inside = NOW - timedelta(days=5)   # well inside the 90-day raw window

    db.add(m.Reading(
        printer_id=printer.id, ts=inside, page_count=1000,
        supply_snapshot=[{"type": "toner", "color": "black", "level_pct": 90.0}],
    ))
    db.add(m.Reading(
        printer_id=printer.id, ts=inside + timedelta(hours=6), page_count=1500,
        supply_snapshot=[{"type": "toner", "color": "black", "level_pct": 10.0}],
    ))
    # A rollup for the same day -- which retention would never write, but a
    # backfill or a shortened retention window could.
    db.add(m.ReadingRollup(
        printer_id=printer.id,
        day=inside.date(),
        readings_count=2,
        first_ts=inside,
        last_ts=inside + timedelta(hours=6),
        page_count=1500,
        supply_snapshot=[{"type": "toner", "color": "black", "level_pct": 10.0}],
    ))
    db.commit()
    _scan(db, printer)

    cycle = _cycles(db, printer)[0]
    assert cycle.pages == 500  # not 1000
    assert cycle.readings_count == 2


def test_a_hostile_supply_snapshot_cannot_break_the_scan(db):
    """``supply_snapshot`` is JSON an agent pushed, so it is attacker-shaped. A
    scan that raises takes the worker cycle down for every other tenant.

    A level that is **not a number** (NaN, ±inf) is covered by
    ``test_a_level_that_is_not_a_number_is_skipped_by_the_parser`` rather than
    here, and deliberately -- do not add one back to this list. Postgres' ``json``
    type refuses the token ``NaN`` outright, so the INSERT fails during *setup*
    and this test never reaches the scan it exists to exercise. SQLite stores it
    happily (Python's ``json.dumps`` emits a bare ``NaN``, which is not valid
    JSON), so it passed here for as long as the suite ran only on SQLite and
    failed the moment a real Postgres ran it. It is also a state ingest cannot
    produce: ``SupplyIn._refuse_bad_level`` coerces a non-finite level to
    ``None`` long before anything is stored. So it is asserted where it can
    actually occur -- at the parser -- instead of through a round trip the
    database cannot make.
    """
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    ts = NOW - timedelta(days=3)
    for snapshot in (
        "not a list",
        [None, 42, "x"],
        [{"type": "toner", "level_pct": "lots"}],
        [{"type": 7, "level_pct": 50}],
        [{"type": "toner", "color": "black", "level_pct": True}],
        [{"type": "toner", "color": "black", "level_pct": 55}],   # the only real one
    ):
        db.add(m.Reading(printer_id=printer.id, ts=ts, page_count=10,
                         supply_snapshot=snapshot))
        ts += timedelta(hours=1)
    db.commit()

    _scan(db, printer)
    cycles = _cycles(db, printer)
    assert len(cycles) == 1
    assert cycles[0].supply_type == "toner" and cycles[0].color == "black"


def test_a_level_that_is_not_a_number_is_skipped_by_the_parser():
    """A level that is not a number is not a level -- NaN and ±inf are skipped.

    Driven straight at ``_snapshot_levels`` rather than through the database,
    because Postgres cannot store any of these in a ``json`` column (see the
    test above) and ingest coerces them away before storage. That leaves the
    parser as the only place the value can actually appear: a rollup row or a
    hand-written one, where nothing has re-validated it.

    Worth keeping because the safe spelling is one keystroke from the unsafe
    one. **Every comparison against NaN is False**, so the natural range check
    ``if level < 0 or level > 100: skip`` passes NaN straight through, and it
    would then propagate into the cartridge arithmetic as a silent poison --
    every subsequent comparison against it False, so a cycle never closes and
    nothing anywhere reports why. The parser tests ``level != level``; this is
    what holds it there.
    """
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert sy._snapshot_levels([{"type": "toner", "level_pct": bad}]) == {}, bad

    # ...and a real slot standing beside a poisoned one still parses, so the
    # skip is targeted rather than "this snapshot looked odd, drop all of it".
    assert sy._snapshot_levels(
        [
            {"type": "toner", "level_pct": float("nan")},
            {"type": "toner", "color": "black", "level_pct": 55},
        ]
    ) == {("toner", "black"): 55.0}


def test_a_slot_with_no_colour_stores_empty_string_not_null(db):
    """NULLs compare distinct in SQL. A NULL colour would make "the open cycle
    for this slot" unfindable, and a second one would be opened every pass."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    ts = NOW - timedelta(days=4)
    for level in (100.0, 50.0, 10.0):
        db.add(m.Reading(
            printer_id=printer.id, ts=ts, page_count=100,
            supply_snapshot=[{"type": "drum", "level_pct": level}],
        ))
        ts += timedelta(days=1)
    db.commit()

    _scan(db, printer)
    _scan(db, printer)
    cycles = _cycles(db, printer)
    assert len(cycles) == 1
    assert cycles[0].color == ""


# --------------------------------------------------------------------------- #
# 3. Confidence
# --------------------------------------------------------------------------- #
def test_one_short_cartridge_is_not_a_finding(db):
    """The single most important negative in this module."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=2, pages_each=300)  # one completed cycle
    db.commit()
    _scan(db, printer)
    _expect(db, "MFC-L8900", 3000)
    db.commit()

    row = sy.assessments(db, thresholds=sy.YieldThresholds())[0]
    assert row.verdict == sy.VERDICT_INSUFFICIENT
    assert row.cycles_counted == 1
    assert row.ratio is None                    # no comparison is offered
    assert row.observed_pages == pytest.approx(300.0)  # the measurement still is
    assert "1 of 3 completed cartridge(s)" in "; ".join(row.reasons)


def test_a_missing_expectation_is_not_a_clean_bill_of_health(db):
    """Absence reported as safety is the mistake the posture report was
    corrected for. It is the same mistake here, wearing a commercial hat."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=5, pages_each=900)
    db.commit()
    _scan(db, printer)
    db.commit()

    row = sy.assessments(db, thresholds=sy.YieldThresholds())[0]
    assert row.verdict == sy.VERDICT_NO_EXPECTATION
    assert row.verdict != sy.VERDICT_WITHIN
    assert not row.has_verdict
    assert row.observed_pages == pytest.approx(900.0)


def test_a_cartridge_pulled_early_is_recorded_but_not_counted(db):
    """It says nothing about yield, so it must not vote -- and it must still be
    visible, because a technician checking a finding needs to see it."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    ts = NOW - timedelta(days=20)
    # 100 -> 80 over 100 pages, then replaced. 20 points consumed.
    for level, pages in ((100.0, 500), (80.0, 600), (100.0, 600), (30.0, 2000)):
        db.add(m.Reading(
            printer_id=printer.id, ts=ts, page_count=pages,
            supply_snapshot=[{"type": "toner", "color": "black", "level_pct": level}],
        ))
        ts += timedelta(days=2)
    db.commit()
    _scan(db, printer)
    db.commit()

    row = sy.assessments(db, thresholds=sy.YieldThresholds())[0]
    assert row.cycles_total == 1        # recorded
    assert row.cycles_counted == 0      # not evidence
    assert row.verdict == sy.VERDICT_INSUFFICIENT


def test_the_summary_never_folds_absence_into_findings(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=3, pages_each=800)
    db.commit()
    _scan(db, printer)
    db.commit()

    rows = sy.assessments(db, thresholds=sy.YieldThresholds())
    summary = sy.summarize(rows)
    assert summary["below"] == 0
    assert summary["no_verdict"] == len(rows)
    assert summary["slots"] == 1


# --------------------------------------------------------------------------- #
# 4. Expected yield: where it comes from
# --------------------------------------------------------------------------- #
def test_an_entered_expectation_matches_a_model_substring(db):
    rows = [
        m.SupplyYieldExpectation(
            model_tag="MFC-L8900", supply_type="toner", color="", expected_pages=3000
        )
    ]
    found = sy.expectation_for(rows, "Brother MFC-L8900CDW series", "toner", "black")
    assert found is not None and found.pages == 3000
    assert found.source == sy.SOURCE_OPERATOR
    assert sy.expectation_for(rows, "HP LaserJet M404", "toner", "black") is None


def test_the_longest_matching_tag_wins(db):
    rows = [
        m.SupplyYieldExpectation(model_tag="MFC", supply_type="toner", color="",
                                 expected_pages=1000),
        m.SupplyYieldExpectation(model_tag="MFC-L8900CDW", supply_type="toner",
                                 color="", expected_pages=6500),
    ]
    found = sy.expectation_for(rows, "Brother MFC-L8900CDW series", "toner", "black")
    assert found.pages == 6500


def test_an_exact_colour_beats_any_colour(db):
    rows = [
        m.SupplyYieldExpectation(model_tag="MFC-L8900CDW", supply_type="toner",
                                 color="", expected_pages=3000),
        m.SupplyYieldExpectation(model_tag="MFC-L8900", supply_type="toner",
                                 color="black", expected_pages=4500),
    ]
    found = sy.expectation_for(rows, "Brother MFC-L8900CDW", "toner", "black")
    assert found.pages == 4500


def test_an_equally_specific_tie_is_refused_rather_than_guessed(db):
    """A coin flip whose losing side looks exactly like data. The gap would be an
    artefact of row order -- same rule as an ambiguous driver package."""
    rows = [
        m.SupplyYieldExpectation(model_tag="L8900", supply_type="toner", color="",
                                 expected_pages=3000),
        m.SupplyYieldExpectation(model_tag="MFC-L", supply_type="toner", color="",
                                 expected_pages=9000),
    ]
    found = sy.expectation_for(rows, "Brother MFC-L8900CDW", "toner", "black")
    assert found is not None
    assert found.source == sy.SOURCE_AMBIGUOUS
    assert found.pages is None and not found.known


def test_a_two_character_tag_never_matches(db):
    rows = [m.SupplyYieldExpectation(model_tag="L8", supply_type="toner", color="",
                                     expected_pages=3000)]
    assert sy.expectation_for(rows, "Brother MFC-L8900CDW", "toner", "black") is None


def test_the_fleet_baseline_needs_enough_samples_from_enough_printers(db):
    th = sy.YieldThresholds(baseline_min_cycles=5, baseline_min_printers=3)
    # Five samples, but from two printers: not a baseline, it is two devices'
    # habits.
    assert sy.fleet_baseline([(1, 900.0)] * 3 + [(2, 900.0)] * 2, 9, th) is None
    # Enough printers, too few samples.
    assert sy.fleet_baseline([(1, 900.0), (2, 900.0), (3, 900.0)], 9, th) is None
    ok = sy.fleet_baseline(
        [(1, 900.0), (2, 1000.0), (3, 1100.0), (4, 950.0), (5, 1050.0)], 9, th
    )
    assert ok is not None and ok.source == sy.SOURCE_FLEET
    assert ok.pages == pytest.approx(1000.0)   # median, not mean
    assert ok.sample_cycles == 5 and ok.sample_printers == 5


def test_a_printer_is_never_part_of_its_own_baseline(db):
    """Include it and a printer running non-OEM contributes its own low yields to
    the expectation it is measured against -- so the more consistently it
    under-delivers, the less it looks like it does."""
    th = sy.YieldThresholds(baseline_min_cycles=3, baseline_min_printers=2)
    samples = [(7, 100.0), (7, 100.0), (7, 100.0), (1, 1000.0), (2, 1000.0),
               (3, 1000.0)]
    baseline = sy.fleet_baseline(samples, exclude_printer_id=7, thresholds=th)
    assert baseline.pages == pytest.approx(1000.0)
    assert 7 not in {p for p, _ in samples if p == 7 and False}  # documentation
    assert baseline.sample_cycles == 3


def test_an_entered_expectation_beats_the_fleet_baseline(db):
    """A datasheet figure is a fact about the hardware; a fleet median is a
    comparison against peers, and a fleet all running non-OEM calibrates to it."""
    client, site = _client_site(db)
    peers = []
    for i in range(4):
        peer = _printer(db, client, site, ip="10.0.0.%d" % (20 + i))
        _cartridges(db, peer, count=3, pages_each=1000, start_pages=1000 * i)
        peers.append(peer)
    subject = _printer(db, client, site, ip="10.0.0.99")
    _cartridges(db, subject, count=4, pages_each=980)
    db.commit()
    for printer in peers + [subject]:
        _scan(db, printer)

    th = sy.YieldThresholds()
    by_printer = {r.printer_id: r for r in sy.assessments(db, thresholds=th)}
    assert by_printer[subject.id].expected.source == sy.SOURCE_FLEET
    assert by_printer[subject.id].verdict == sy.VERDICT_WITHIN

    _expect(db, "MFC-L8900", 5000)
    db.commit()
    row = {r.printer_id: r for r in sy.assessments(db, thresholds=th)}[subject.id]
    assert row.expected.source == sy.SOURCE_OPERATOR
    assert row.expected.pages == 5000
    assert row.verdict == sy.VERDICT_BELOW   # 980 against a rated 5000


# --------------------------------------------------------------------------- #
# 5. The accusation
# --------------------------------------------------------------------------- #
def test_a_short_printer_is_flagged_and_a_normal_one_beside_it_is_not(db):
    """The whole feature, in one assertion: two printers of the same model, one
    delivering a third of the rated pages and one delivering all of them."""
    client, site = _client_site(db)
    good = _printer(db, client, site, ip="10.0.0.10")
    bad = _printer(db, client, site, ip="10.0.0.11")
    _cartridges(db, good, count=4, pages_each=3000)
    _cartridges(db, bad, count=4, pages_each=1000)
    db.commit()
    _scan(db, good)
    _scan(db, bad)
    _expect(db, "MFC-L8900", 3000)
    db.commit()

    rows = {r.printer_id: r for r in sy.assessments(db, thresholds=sy.YieldThresholds())}
    assert rows[good.id].verdict == sy.VERDICT_WITHIN
    assert rows[bad.id].verdict == sy.VERDICT_BELOW
    assert rows[bad.id].shortfall_pct == pytest.approx(66.7, abs=0.5)
    # Findings sort first, so the operator sees the one that matters.
    assert sy.assessments(db, thresholds=sy.YieldThresholds())[0].printer_id == bad.id


def test_the_threshold_is_operator_settable(db):
    """A 20% shortfall is inside the shipped 30% default and outside a 15% one."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=4, pages_each=2400)
    db.commit()
    _scan(db, printer)
    _expect(db, "MFC-L8900", 3000)
    db.commit()

    assert sy.assessments(db, thresholds=sy.YieldThresholds())[0].verdict == (
        sy.VERDICT_WITHIN
    )
    strict = sy.YieldThresholds(shortfall_pct=15.0)
    assert sy.assessments(db, thresholds=strict)[0].verdict == sy.VERDICT_BELOW


def test_the_shipped_default_is_conservative():
    """A false positive tells an MSP their customer buys grey-market cartridges."""
    shipped = sy.YieldThresholds.from_runtime(runtime.default_settings())
    assert shipped.shortfall_pct >= 25.0
    assert shipped.min_cycles >= 3
    assert shipped.min_consumed_pct >= 50.0
    assert shipped.emit_events is False


def test_thresholds_cannot_be_set_to_something_that_reports_a_single_sample():
    """A hand-edited app_settings row must not be able to turn one cartridge into
    a finding, or turn a 0% shortfall into one."""
    hostile = sy.YieldThresholds.from_runtime({
        "yield.min_cycles": 1,
        "yield.shortfall_pct": 0,
        "yield.min_consumed_pct": 1,
        "yield.baseline_min_printers": 1,
        "yield.scan_interval_min": 0,
    })
    assert hostile.min_cycles >= 2
    assert hostile.shortfall_pct >= 5.0
    assert hostile.min_consumed_pct >= 20.0
    assert hostile.baseline_min_printers >= 2
    assert hostile.scan_interval_min >= 1


def test_garbage_settings_degrade_to_the_shipped_defaults():
    junk = sy.YieldThresholds.from_runtime(
        {"yield.min_cycles": "three", "yield.shortfall_pct": None}
    )
    assert junk.min_cycles == sy.YieldThresholds().min_cycles
    assert junk.shortfall_pct == sy.YieldThresholds().shortfall_pct


# --------------------------------------------------------------------------- #
# 6. The worker job
# --------------------------------------------------------------------------- #
def test_the_job_records_cycles_and_gates_on_its_interval(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=3, pages_each=1500)
    db.commit()

    first = jobs.scan_supply_cycles(db, now=NOW)["supply_cycles"]
    assert first["cartridges_replaced"] == 2
    assert first["printers"] == 1

    # Inside the interval: not merely a cheap pass, a skipped one.
    assert jobs.scan_supply_cycles(db, now=NOW + timedelta(minutes=5)) == {}
    later = jobs.scan_supply_cycles(db, now=NOW + timedelta(hours=7))
    assert "supply_cycles" in later


def test_the_job_can_be_switched_off(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=3, pages_each=1500)
    _set_yield(db, **{"yield.enabled": False})
    db.commit()

    assert jobs.scan_supply_cycles(db, now=NOW) == {"supply_cycles": "disabled"}
    assert _cycles(db, printer) == []


def test_a_pending_printer_is_not_measured(db):
    """A device in the discovery queue is not part of anybody's fleet yet."""
    client, site = _client_site(db)
    pending = _printer(db, client, site, discovery_state=m.DiscoveryState.pending)
    _cartridges(db, pending, count=3, pages_each=1000)
    db.commit()

    jobs.scan_supply_cycles(db, now=NOW)
    assert _cycles(db, pending) == []


def test_events_are_off_by_default_and_say_so(db):
    """Not "delivered". The ChannelResult.sent rule, applied to events."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=3, pages_each=1000)
    db.commit()

    result = jobs.scan_supply_cycles(db, now=NOW)["supply_cycles"]
    assert result["events"]["published"] == 0
    assert "off" in result["events"]["reason"]
    assert db.query(m.OutboundEvent).count() == 0


def test_a_replacement_emits_exactly_one_event_ever(db):
    """Keyed on the cycle id, so a replayed worker cycle cannot re-announce a
    cartridge change that was already reported."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=3, pages_each=1000)
    _set_yield(db, **{"yield.enabled": True, "yield.emit_events": True})
    db.commit()

    jobs.scan_supply_cycles(db, now=NOW)
    replaced = db.query(m.OutboundEvent).filter(
        m.OutboundEvent.type == "supply.replaced"
    ).all()
    assert len(replaced) == 2
    body = replaced[0].data
    assert body["supply"]["type"] == "toner"
    assert body["cycle"]["pages"] == 1000
    assert body["cycle"]["observed_yield_pages"] == 1000
    assert body["printer"]["ip"] == printer.ip

    # A second pass over the same history must not emit a second time.
    jobs.scan_supply_cycles(db, now=NOW + timedelta(days=1))
    assert db.query(m.OutboundEvent).filter(
        m.OutboundEvent.type == "supply.replaced"
    ).count() == 2


def test_a_shortfall_event_carries_the_provenance_of_its_expectation(db):
    """A consumer that cannot tell a datasheet figure from a fleet median cannot
    weigh the finding, and this finding may reach a customer."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=4, pages_each=800)
    _expect(db, "MFC-L8900", 4000)
    _set_yield(db, **{"yield.enabled": True, "yield.emit_events": True})
    db.commit()

    jobs.scan_supply_cycles(db, now=NOW)
    events = db.query(m.OutboundEvent).filter(
        m.OutboundEvent.type == "supply.yield_below_expected"
    ).all()
    assert len(events) == 1
    payload = events[0].data["yield"]
    assert payload["observed_pages"] == 800
    assert payload["expected_pages"] == 4000
    assert payload["expected_source"] == sy.SOURCE_OPERATOR
    assert payload["shortfall_pct"] == pytest.approx(80.0)
    assert payload["cycles_counted"] == 3


def test_every_event_type_this_module_sends_is_in_the_catalogue(db):
    from central.events.catalogue import CATALOGUE

    assert "supply.replaced" in CATALOGUE
    assert "supply.yield_below_expected" in CATALOGUE


# --------------------------------------------------------------------------- #
# 7. Tenancy and authorization
# --------------------------------------------------------------------------- #
def test_the_report_scopes_to_one_client_but_the_baseline_does_not(db):
    """Scoping the baseline per tenant would leave a customer with two printers
    permanently un-assessable, and would let one customer's non-OEM habit define
    what normal means for their own devices."""
    acme, acme_site = _client_site(db, "Acme")
    globex, globex_site = _client_site(db, "Globex", "Depot")
    for i in range(4):
        peer = _printer(db, globex, globex_site, ip="10.1.0.%d" % (10 + i))
        _cartridges(db, peer, count=3, pages_each=3000, start_pages=1000 * i)
    subject = _printer(db, acme, acme_site, ip="10.0.0.50")
    _cartridges(db, subject, count=4, pages_each=900)
    db.commit()
    jobs.scan_supply_cycles(db, now=NOW)

    scoped = sy.assessments(db, client_id=acme.id, thresholds=sy.YieldThresholds())
    assert {r.client_id for r in scoped} == {acme.id}
    assert scoped[0].expected.source == sy.SOURCE_FLEET   # from Globex's printers
    assert scoped[0].verdict == sy.VERDICT_BELOW


def test_a_customer_user_never_reaches_the_yield_report(db):
    """Same decision as /security/posture: a shortfall is an MSP conversation,
    not a self-service page."""
    client, site = _client_site(db)
    db.add(m.Client(name="Other"))
    db.flush()
    user = m.User(
        username="cust", password_hash=hash_password("pw"),
        role=m.UserRole.client_readonly, client_id=client.id,
    )
    db.add(user)
    db.commit()

    http = TestClient(app)
    assert http.post("/login", data={"username": "cust", "password": "pw"},
                     follow_redirects=False).status_code == 303
    resp = http.get("/supplies/yield", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal"


def test_the_report_needs_a_session(db):
    resp = TestClient(app).get("/supplies/yield", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_the_page_renders_the_finding_and_names_no_verdict(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=4, pages_each=700)
    _expect(db, "MFC-L8900", 3000)
    db.commit()
    jobs.scan_supply_cycles(db, now=NOW)

    http = _login(db)
    body = http.get("/supplies/yield").text
    assert "below expected" in body
    # The interpretation is offered as prose, never as the verdict itself.
    assert "not a verdict" in body
    assert "non-OEM" not in body.split("Assessment")[1].split("Recent cartridge")[0]


def test_an_unknown_client_filter_widens_rather_than_erroring(db):
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=3, pages_each=1000)
    db.commit()
    jobs.scan_supply_cycles(db, now=NOW)

    http = _login(db)
    assert http.get("/supplies/yield?client_id=9999").status_code == 200
    assert http.get("/supplies/yield?client_id=not-a-number").status_code == 200


# --------------------------------------------------------------------------- #
# 8. Managing expectations
# --------------------------------------------------------------------------- #
def test_saving_an_expectation_is_audited_with_its_numbers(db):
    """The numbers are what decide whether a customer gets accused."""
    http = _login(db)
    resp = http.post(
        "/supplies/yield/expectations",
        data={"model_tag": "MFC-L8900", "supply_type": "toner", "color": "black",
              "expected_pages": "6500", "note": "TN-421"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = db.query(m.SupplyYieldExpectation).one()
    assert row.expected_pages == 6500 and row.color == "black"

    entry = db.query(m.AuditLog).filter(
        m.AuditLog.action == "supply_yield.expectation_create"
    ).one()
    assert "6500" in entry.detail and "MFC-L8900" in entry.detail


def test_re_saving_the_same_slot_corrects_rather_than_duplicating(db):
    """A duplicate would be an equally-specific tie, which the matcher refuses --
    silently costing the operator the expectation they already had."""
    http = _login(db)
    for pages in ("3000", "4200"):
        http.post(
            "/supplies/yield/expectations",
            data={"model_tag": "MFC-L8900", "supply_type": "toner", "color": "",
                  "expected_pages": pages},
            follow_redirects=False,
        )
    rows = db.query(m.SupplyYieldExpectation).all()
    assert len(rows) == 1 and rows[0].expected_pages == 4200


@pytest.mark.parametrize(
    "payload",
    [
        {"model_tag": "L8", "supply_type": "toner", "expected_pages": "3000"},
        {"model_tag": "MFC-L8900", "supply_type": "plutonium", "expected_pages": "3000"},
        {"model_tag": "MFC-L8900", "supply_type": "toner", "expected_pages": "0"},
        {"model_tag": "MFC-L8900", "supply_type": "toner", "expected_pages": "nine"},
        {"model_tag": "MFC-L8900", "supply_type": "toner",
         "expected_pages": "99999999"},
    ],
)
def test_a_refused_expectation_stores_nothing(db, payload):
    http = _login(db)
    resp = http.post("/supplies/yield/expectations", data=payload,
                     follow_redirects=False)
    assert resp.status_code == 303
    assert db.query(m.SupplyYieldExpectation).count() == 0


def test_deleting_an_expectation_keeps_every_measurement(db):
    """That is the whole reason a verdict is computed on read rather than stored."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=4, pages_each=700)
    _expect(db, "MFC-L8900", 3000)
    db.commit()
    jobs.scan_supply_cycles(db, now=NOW)
    cycle_count = db.query(m.SupplyCycle).count()
    row_id = db.query(m.SupplyYieldExpectation).one().id

    http = _login(db)
    resp = http.post("/supplies/yield/expectations/%d/delete" % row_id,
                     follow_redirects=False)
    assert resp.status_code == 303
    assert db.query(m.SupplyYieldExpectation).count() == 0
    assert db.query(m.SupplyCycle).count() == cycle_count
    assert db.query(m.AuditLog).filter(
        m.AuditLog.action == "supply_yield.expectation_delete"
    ).count() == 1
    # The verdict falls back rather than vanishing.
    assert sy.assessments(db, thresholds=sy.YieldThresholds())[0].verdict == (
        sy.VERDICT_NO_EXPECTATION
    )


def test_a_customer_user_cannot_write_an_expectation(db):
    client, _site = _client_site(db)
    db.add(m.User(username="cust", password_hash=hash_password("pw"),
                  role=m.UserRole.client_readonly, client_id=client.id))
    db.commit()
    http = TestClient(app)
    http.post("/login", data={"username": "cust", "password": "pw"},
              follow_redirects=False)
    resp = http.post(
        "/supplies/yield/expectations",
        data={"model_tag": "MFC-L8900", "supply_type": "toner",
              "expected_pages": "3000"},
        follow_redirects=False,
    )
    # 403: signed in as a customer, refused. Not asked to sign in again.
    assert resp.status_code == 403
    assert db.query(m.SupplyYieldExpectation).count() == 0


def test_the_replacement_log_is_not_written_to_printer_events(db):
    """printer_events is the counting source for occurrence-rate rules. A rule
    matching everything above a severity would start counting toner changes as
    printer faults -- a new feature silently changing an existing one's numbers.
    """
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    _cartridges(db, printer, count=3, pages_each=1000)
    db.commit()
    jobs.scan_supply_cycles(db, now=NOW)

    assert db.query(m.PrinterEvent).count() == 0
    assert len(sy.recent_replacements(db)) == 2


def test_the_replacement_log_is_tenant_scopeable(db):
    acme, acme_site = _client_site(db, "Acme")
    globex, globex_site = _client_site(db, "Globex", "Depot")
    mine = _printer(db, acme, acme_site, ip="10.0.0.60")
    theirs = _printer(db, globex, globex_site, ip="10.1.0.60")
    _cartridges(db, mine, count=2, pages_each=500)
    _cartridges(db, theirs, count=2, pages_each=500)
    db.commit()
    jobs.scan_supply_cycles(db, now=NOW)

    scoped = sy.recent_replacements(db, client_id=acme.id)
    assert scoped and all(c.printer_id == mine.id for c in scoped)


def test_the_settings_page_renders_the_yield_thresholds(db):
    """A threshold an operator cannot find is a threshold they cannot make
    conservative for their own fleet."""
    http = _login(db)
    body = http.get("/settings?group=alerts").text
    assert "yield.shortfall_pct" in body
    assert "yield.min_cycles" in body


def test_the_nav_offers_the_report_to_staff_only(db):
    staff = _login(db)
    assert '/supplies/yield' in staff.get("/").text

    client, _site = _client_site(db)
    db.add(m.User(username="cust", password_hash=hash_password("pw"),
                  role=m.UserRole.client_readonly, client_id=client.id))
    db.commit()
    http = TestClient(app)
    http.post("/login", data={"username": "cust", "password": "pw"},
              follow_redirects=False)
    assert "/supplies/yield" not in http.get("/portal").text


def test_a_reading_series_that_only_rises_yields_nothing(db):
    """A receptacle fills rather than empties. It must not read as a stream of
    replacements, each 'yielding' the pages printed since the last one."""
    client, site = _client_site(db)
    printer = _printer(db, client, site)
    ts = NOW - timedelta(days=30)
    for level in (5.0, 40.0, 75.0, 95.0):
        db.add(m.Reading(
            printer_id=printer.id, ts=ts, page_count=1000,
            supply_snapshot=[{"type": "waste", "color": "", "level_pct": level}],
        ))
        ts += timedelta(days=3)
    db.commit()
    _scan(db, printer)

    # Each rise IS read as a boundary -- there is no other signal -- but every
    # resulting cycle consumed nothing, so none of them is ever evidence.
    for cycle in _cycles(db, printer):
        assert sy.consumed_pct(cycle) == 0.0
        assert sy.observed_yield(cycle) is None
        assert not sy.qualifies(cycle, sy.YieldThresholds())
    rows = sy.assessments(db, thresholds=sy.YieldThresholds())
    assert rows and rows[0].cycles_counted == 0
    assert rows[0].verdict == sy.VERDICT_INSUFFICIENT


def test_the_raw_cutoff_follows_the_retention_setting(db):
    """Both halves of the partition move together, or a widened retention window
    silently starts double-counting."""
    from central.retention import effective_raw_days

    cut = sy.raw_cutoff(NOW, {"retention.raw_days": 120})
    assert cut == datetime(2026, 4, 3, tzinfo=timezone.utc)
    assert effective_raw_days({"retention.raw_days": 120}) == 120
    # Below the forecast's floor it clamps, and the cutoff clamps with it.
    floored = sy.raw_cutoff(NOW, {"retention.raw_days": 1})
    assert floored > datetime(2026, 4, 3, tzinfo=timezone.utc)
    assert isinstance(cut.date(), date)
