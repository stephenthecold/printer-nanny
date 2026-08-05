"""Cost-per-page billing: money, meters, the invoice, and the two report fixes.

The properties worth stating up front, because each one is a way this feature
can be quietly wrong rather than loudly broken:

* **No float ever touches money.** A rate is six decimals and an invoice
  multiplies it by five-figure page counts; a float round trip through SQLite
  would show up as an invoice whose lines do not add up to its own total.
* **Rounding is half-up, at the line, once.** Not banker's rounding (which is
  what Python does by default and is not what an invoice does), and not per
  page (which drifts by up to half a cent times the page count).
* **An unknown meter is never billed as zero.** Blank and zero are different
  facts everywhere in this feature: the CSV, the engine and the UI.
* **A period delta never goes negative.** A firmware reflash or a replacement
  device resets the counter; the naive difference is a large negative number
  that would cancel out a month of real printing.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select

from central import billing
from central import models as m
from central import queries, reports
from central.main import app
from central.money import (
    Money,
    Rate,
    format_money,
    parse_amount,
    parse_rate,
    round_money,
    to_decimal,
)
from central.runtime import save_settings
from central.security import hash_password
from central.services import TenancyError

JULY = billing.month_bounds(2026, 7)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _seed_client(db, name="Acme"):
    client = m.Client(name=name)
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    return client, site


def _printer(db, client, site, ip="10.0.0.10", **kw):
    kw.setdefault("display_name", "Front Desk")
    kw.setdefault("brand", "Brother")
    kw.setdefault("model", "MFC-L8900CDW")
    p = m.Printer(
        client_id=client.id, site_id=site.id, ip=ip,
        status=m.PrinterStatus.ok, discovery_state=m.DiscoveryState.approved,
        **kw,
    )
    db.add(p)
    db.flush()
    return p


def _reading(db, printer, day, *, pages=None, mono=None, color=None, month=7, year=2026):
    db.add(m.Reading(
        printer_id=printer.id,
        ts=datetime(year, month, day, 12, 0, tzinfo=timezone.utc),
        page_count=pages, mono_count=mono, color_count=color,
        status=m.PrinterStatus.ok,
    ))


def _card(db, client, mono="0.0085", color="0.0720", **kw):
    card = m.BillingRateCard(
        client_id=client.id,
        name=kw.pop("name", "Standard"),
        mono_rate=Decimal(mono),
        color_rate=Decimal(color),
        **kw,
    )
    db.add(card)
    db.flush()
    return card


def _admin_client(db, username="admin", password="admin", role=m.UserRole.admin):
    db.add(m.User(username=username, password_hash=hash_password(password), role=role))
    db.commit()
    http = TestClient(app)
    resp = http.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert resp.status_code == 303
    return http


def _rows(raw: bytes):
    reader = csv.reader(io.StringIO(raw.decode("utf-8")))
    all_rows = list(reader)
    return all_rows[0], all_rows[1:]


# --------------------------------------------------------------------------- #
# 1) Money: exactness and the rounding rule
# --------------------------------------------------------------------------- #
def test_money_columns_round_trip_exactly_on_this_backend():
    """The stored value comes back as the same Decimal, not a float's shadow.

    SQLAlchemy's plain ``Numeric`` stores a float on SQLite and warns that
    rounding errors "may occur" -- which is exactness on the backend nobody
    develops against. This asserts against whichever backend the suite is
    actually running on.
    """
    md = MetaData()
    t = Table("money_probe", md,
              Column("id", Integer, primary_key=True),
              Column("rate", Rate()), Column("amount", Money()))
    engine = create_engine("sqlite://")
    md.create_all(engine)
    values = [Decimal("0.008500"), Decimal("0.072000"), Decimal("10.500000")]
    with engine.begin() as conn:
        conn.execute(t.insert(), [
            {"id": i, "rate": v, "amount": Decimal("1234.56")}
            for i, v in enumerate(values, start=1)
        ])
    with engine.connect() as conn:
        got = [row.rate for row in conn.execute(select(t).order_by(t.c.id))]
        # Ordering must agree with the numbers, not with their text form.
        ordered = [row.rate for row in conn.execute(select(t).order_by(t.c.rate))]
    assert got == values
    assert all(isinstance(v, Decimal) for v in got)
    assert ordered == sorted(values)


@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_no_float_is_ever_handed_to_the_driver(dialect_name):
    """The bind path must not pass through ``processors.to_float``.

    ``sqlalchemy.Numeric.bind_processor`` returns exactly that on any dialect
    whose ``supports_native_decimal`` is False -- which is the inherited default
    on both ``PGDialect`` and ``SQLiteDialect``. If a ``TypeDecorator`` lets the
    impl's processor run, the exact Decimal becomes a float on the way in and
    the only symptom is an invoice that stops adding up. Asserted against
    ``dialect_impl``, which is the object execution actually uses -- checking the
    undialected type measures a different processor and would pass either way.
    """
    from sqlalchemy.dialects import postgresql, sqlite

    dialect = (postgresql if dialect_name == "postgresql" else sqlite).dialect()
    impl = Rate().dialect_impl(dialect)
    bound = impl.bind_processor(dialect)(Decimal("0.0085"))
    assert not isinstance(bound, float), f"{dialect_name} bind produced a float: {bound!r}"
    assert Decimal(str(bound)) == Decimal("0.008500")
    assert impl.result_processor(dialect, None)(bound) == Decimal("0.008500")


def test_the_money_type_refuses_a_value_that_would_not_fit():
    """Silent truncation of a rate is a wrong price, not a formatting problem."""
    from sqlalchemy.dialects import sqlite

    impl = Rate().dialect_impl(sqlite.dialect())
    with pytest.raises(ValueError):
        impl.bind_processor(sqlite.dialect())(Decimal("1234567.0"))


def test_rounding_is_half_up_not_bankers():
    """Python rounds half to even by default; an invoice does not."""
    assert round_money(Decimal("0.005")) == Decimal("0.01")
    assert round_money(Decimal("2.675")) == Decimal("2.68")   # float round() gives 2.67
    assert round_money(Decimal("1.015")) == Decimal("1.02")
    assert round_money(Decimal("0.125")) == Decimal("0.13")   # ROUND_HALF_EVEN gives 0.12
    assert round_money(Decimal("0.135")) == Decimal("0.14")


def test_a_float_never_becomes_money():
    """By the time a float exists the precision is already gone."""
    with pytest.raises(ValueError):
        to_decimal(0.1)
    with pytest.raises(ValueError):
        parse_rate(0.0085)


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-Infinity", "-1", "abc", "", "1e3", "1_0"])
def test_parse_rate_refuses_junk(bad):
    """``Decimal('nan')`` parses happily and compares false to everything, so a
    NaN rate would price every invoice at nothing with nothing flagging it."""
    with pytest.raises(ValueError):
        parse_rate(bad)


def test_parse_amount_rounds_half_up_and_refuses_negatives():
    assert parse_amount("10.005") == Decimal("10.01")
    with pytest.raises(ValueError):
        parse_amount("-0.01")


def test_format_money_renders_none_blank_not_zero():
    assert format_money(None) == ""
    assert format_money(Decimal("0")) == "0.00"


# --------------------------------------------------------------------------- #
# 2) Period meters: reset-safe, half-open, blank-vs-zero
# --------------------------------------------------------------------------- #
def test_period_meters_sum_positive_deltas(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _reading(db, p, 2, pages=1000, mono=800, color=200)
    _reading(db, p, 10, pages=1500, mono=1150, color=350)
    _reading(db, p, 20, pages=1800, mono=1350, color=450)
    db.commit()

    got = queries.period_meters(db, p.id, *JULY)
    assert got.pages == 800
    assert got.mono == 550
    assert got.color == 250


def test_a_counter_reset_contributes_zero_not_a_negative(db):
    """A firmware reflash / replacement device resets the meter mid-period.

    The naive difference is -1400, which would cancel out a month of real
    printing and could make a whole client's invoice smaller than one printer's
    activity.
    """
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _reading(db, p, 2, pages=1000, mono=1000)
    _reading(db, p, 10, pages=1500, mono=1500)   # +500
    _reading(db, p, 11, pages=100, mono=100)     # reset: contributes 0, not -1400
    _reading(db, p, 20, pages=400, mono=400)     # +300
    db.commit()

    got = queries.period_meters(db, p.id, *JULY)
    assert got.pages == 800
    assert got.mono == 800
    assert got.pages >= 0


def test_an_unreported_meter_is_none_not_zero(db):
    """A mono-only device reports no colour meter. That is not zero colour pages."""
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _reading(db, p, 2, pages=1000, mono=1000)
    _reading(db, p, 20, pages=1400, mono=1400)
    db.commit()

    got = queries.period_meters(db, p.id, *JULY)
    assert got.color is None
    assert got.mono == 400


def test_a_meter_that_reported_and_did_not_move_is_zero_not_none(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _reading(db, p, 2, pages=1000, mono=900, color=100)
    _reading(db, p, 20, pages=1300, mono=1200, color=100)
    db.commit()

    got = queries.period_meters(db, p.id, *JULY)
    assert got.color == 0
    assert got.color is not None


def test_the_reading_before_the_period_seeds_the_delta(db):
    """Without a baseline every period silently discards the pages printed
    between the boundary and its first reading -- and consecutive periods would
    no longer add up to the lifetime total."""
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _reading(db, p, 30, pages=1000, mono=1000, month=6)   # June, just before July
    _reading(db, p, 2, pages=1200, mono=1200)             # July
    _reading(db, p, 20, pages=1500, mono=1500)
    db.commit()

    got = queries.period_meters(db, p.id, *JULY)
    assert got.pages == 500  # 1000 -> 1500, not 1200 -> 1500


def test_a_baseline_alone_does_not_make_a_meter_known(db):
    """The device reported colour in June and stopped in July. July's colour is
    unknown, not a suspiciously round zero."""
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _reading(db, p, 30, pages=1000, mono=900, color=100, month=6)
    _reading(db, p, 20, pages=1500, mono=1400)
    db.commit()

    got = queries.period_meters(db, p.id, *JULY)
    assert got.color is None
    assert got.mono == 500


def test_the_period_is_half_open_so_months_tile_exactly(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    db.add(m.Reading(printer_id=p.id, ts=datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc),
                     page_count=9999, status=m.PrinterStatus.ok))
    _reading(db, p, 31, pages=1000)
    db.commit()

    july = queries.period_meters(db, p.id, *JULY)
    august = queries.period_meters(db, p.id, *billing.month_bounds(2026, 8))
    # The 1 Aug 00:00 reading belongs to August, and August counts it against
    # July's closing value -- so the two periods neither double-count nor drop it.
    assert july.pages == 0
    assert august.pages == 8999


def test_printed_pages_helper_still_backs_the_esg_rollup(db):
    """The ESG rollup's private helper is now one call into the shared
    primitive; its behaviour must not have moved."""
    assert queries._printed_pages_for_printer([(100,), (150,), (10,), (60,)]) == 100
    assert queries.positive_delta([None, 100, None, 150]) == 50
    assert queries.positive_delta([]) == 0


# --------------------------------------------------------------------------- #
# 3) Tiered pricing
# --------------------------------------------------------------------------- #
def test_volume_bands_are_graduated_not_cliff_edged(db):
    client, _ = _seed_client(db)
    card = _card(db, client, mono="0.0080")
    db.add(m.BillingRateTier(rate_card_id=card.id, kind=m.MeterClass.mono,
                             up_to=5000, rate=Decimal("0.0120")))
    db.add(m.BillingRateTier(rate_card_id=card.id, kind=m.MeterClass.mono,
                             up_to=20000, rate=Decimal("0.0100")))
    db.commit()
    tiers = list(card.tiers)

    # 5,000 @ 0.012 = 60; 15,000 @ 0.010 = 150; 5,000 @ 0.008 = 40  => 250
    cost, detail = billing.tiered_cost(25000, tiers, card.mono_rate)
    assert cost == Decimal("250.000000")
    assert "5,000 @ 0.012000" in detail and "5,000 @ 0.008000" in detail

    # Inside the first band, only the first band's rate applies.
    assert billing.tiered_cost(1000, tiers, card.mono_rate)[0] == Decimal("12.000000")


def test_printing_one_more_page_never_lowers_the_bill(db):
    """The reason bands are marginal: a whole-volume rate is non-monotonic."""
    client, _ = _seed_client(db)
    card = _card(db, client, mono="0.0050")
    db.add(m.BillingRateTier(rate_card_id=card.id, kind=m.MeterClass.mono,
                             up_to=1000, rate=Decimal("0.0500")))
    db.commit()
    tiers = list(card.tiers)
    costs = [billing.tiered_cost(n, tiers, card.mono_rate)[0] for n in (999, 1000, 1001, 2000)]
    assert costs == sorted(costs)


def test_zero_pages_cost_nothing(db):
    client, _ = _seed_client(db)
    card = _card(db, client)
    assert billing.tiered_cost(0, [], card.mono_rate)[0] == Decimal("0")


# --------------------------------------------------------------------------- #
# 4) The invoice
# --------------------------------------------------------------------------- #
def test_invoice_prices_mono_and_colour_separately(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client, mono="0.0085", color="0.0720")
    _reading(db, p, 1, pages=10000, mono=8000, color=2000)
    _reading(db, p, 28, pages=15000, mono=12000, color=3000)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    by_kind = {line.kind: line for line in inv.lines}
    assert by_kind["mono"].pages == 4000
    assert by_kind["color"].pages == 1000
    # 4000 * 0.0085 = 34.00 ; 1000 * 0.072 = 72.00
    assert by_kind["mono"].amount == Decimal("34.00")
    assert by_kind["color"].amount == Decimal("72.00")
    assert inv.total == Decimal("106.00")
    assert inv.currency == "USD"
    assert inv.period_label == "2026-07"


def test_the_total_is_the_exact_sum_of_the_printed_lines(db):
    """Whatever else is true, an invoice must add up to itself."""
    client, site = _seed_client(db)
    _card(db, client, mono="0.008333", color="0.066667")
    for i in range(5):
        p = _printer(db, client, site, ip=f"10.0.0.{20 + i}")
        _reading(db, p, 1, pages=1000, mono=700, color=300)
        _reading(db, p, 28, pages=1000 + 137 * (i + 1),
                 mono=700 + 91 * (i + 1), color=300 + 46 * (i + 1))
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    assert inv.metered_subtotal == sum(line.amount for line in inv.lines)
    assert inv.total == inv.metered_subtotal
    assert all(line.amount == line.amount.quantize(Decimal("0.01")) for line in inv.lines)


def test_rounding_happens_at_the_line_not_per_page(db):
    """Per-page rounding would drift by up to half a cent per page.

    At 0.008500/page a per-page half-up round makes every page cost 0.01, so
    40,000 pages would bill 400.00 instead of 340.00 -- a $60 artefact of the
    rounding rule alone.
    """
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client, mono="0.0085")
    _reading(db, p, 1, pages=1000, mono=1000)
    _reading(db, p, 28, pages=41000, mono=41000)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    line = inv.lines[0]
    assert line.pages == 40000
    assert line.amount == Decimal("340.00")
    per_page = round_money(Decimal("0.0085")) * 40000
    assert per_page == Decimal("400.00")  # the wrong answer, stated so it stays wrong
    assert line.amount != per_page


def test_an_unreported_colour_meter_is_disclosed_not_billed_as_zero(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client)
    # Device reports a total and a mono meter, no colour meter at all.
    _reading(db, p, 1, pages=1000, mono=900)
    _reading(db, p, 28, pages=1600, mono=1300)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    kinds = {line.kind for line in inv.lines}
    assert kinds == {"mono"}
    assert inv.unbilled_pages == 200  # 600 total - 400 mono
    assert "colour meter" in inv.unbilled[0].reason


def test_a_device_with_no_split_at_all_bills_nothing_by_default(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client)
    _reading(db, p, 1, pages=1000)
    _reading(db, p, 28, pages=4000)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    assert inv.lines == []
    assert inv.unbilled_pages == 3000
    assert inv.total == Decimal("0")
    assert "no mono/colour split" in inv.unbilled[0].reason


def test_bill_as_mono_is_an_explicit_operator_decision(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client, mono="0.0100", unsplit_policy=m.UnsplitPolicy.bill_as_mono)
    _reading(db, p, 1, pages=1000)
    _reading(db, p, 28, pages=4000)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    assert [(line.kind, line.pages) for line in inv.lines] == [("mono", 3000)]
    assert inv.total == Decimal("30.00")
    assert inv.unbilled_pages == 0


def test_bill_as_mono_does_not_claim_a_partial_split(db):
    """The policy covers devices reporting NEITHER meter. Pages a device did not
    classify as mono are by definition not mono, so no policy may take them."""
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client, unsplit_policy=m.UnsplitPolicy.bill_as_mono)
    _reading(db, p, 1, pages=1000, mono=900)
    _reading(db, p, 28, pages=1600, mono=1300)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    assert [(line.kind, line.pages) for line in inv.lines] == [("mono", 400)]
    assert inv.unbilled_pages == 200


def test_a_total_exceeding_the_split_is_disclosed(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client)
    _reading(db, p, 1, pages=1000, mono=600, color=400)
    _reading(db, p, 28, pages=2000, mono=1100, color=700)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    # 1000 total pages, but mono+colour only account for 800.
    assert inv.unbilled_pages == 200
    assert "unknown class" in inv.unbilled[0].reason


def test_a_printer_with_no_readings_is_listed_rather_than_silently_absent(db):
    client, site = _seed_client(db)
    _printer(db, client, site)
    _card(db, client)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    assert inv.lines == []
    assert inv.unbilled[0].pages == 0
    assert "no meter readings" in inv.unbilled[0].reason


def test_minimum_commitment_is_an_explicit_line_not_an_inflated_total(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client, mono="0.0085", minimum_charge=Decimal("50.00"))
    _reading(db, p, 1, pages=1000, mono=1000, color=0)
    _reading(db, p, 28, pages=2000, mono=2000, color=0)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    assert inv.metered_subtotal == Decimal("8.50")
    assert inv.minimum_adjustment == Decimal("41.50")
    assert inv.total == Decimal("50.00")


def test_a_minimum_below_the_metered_work_changes_nothing(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client, mono="0.0085", minimum_charge=Decimal("5.00"))
    _reading(db, p, 1, pages=1000, mono=1000)
    _reading(db, p, 28, pages=3000, mono=3000)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    assert inv.minimum_adjustment == Decimal("0")
    assert inv.total == inv.metered_subtotal == Decimal("17.00")


def test_an_invoice_never_reaches_into_another_tenants_fleet(db):
    acme, acme_site = _seed_client(db, "Acme")
    beta, beta_site = _seed_client(db, "Beta")
    acme_p = _printer(db, acme, acme_site, ip="10.0.0.10")
    beta_p = _printer(db, beta, beta_site, ip="10.9.9.9")
    _card(db, acme, mono="0.0100")
    _reading(db, acme_p, 1, pages=0, mono=0)
    _reading(db, acme_p, 28, pages=100, mono=100)
    _reading(db, beta_p, 1, pages=0, mono=0)
    _reading(db, beta_p, 28, pages=99999, mono=99999)
    db.commit()

    inv = billing.build_invoice(db, acme.id, *JULY)
    assert {line.printer_id for line in inv.lines} == {acme_p.id}
    assert inv.printer_count == 1
    assert inv.total == Decimal("1.00")


def test_another_clients_rate_card_is_a_tenancy_error_not_a_validation_message(db):
    acme, _ = _seed_client(db, "Acme")
    beta, _ = _seed_client(db, "Beta")
    beta_card = _card(db, beta)
    db.commit()

    with pytest.raises(TenancyError):
        billing.build_invoice(db, acme.id, *JULY, rate_card=beta_card)


def test_no_active_rate_card_is_a_stated_operator_problem(db):
    client, _ = _seed_client(db)
    _card(db, client, active=False)
    db.commit()

    with pytest.raises(billing.BillingError) as exc:
        billing.build_invoice(db, client.id, *JULY)
    assert "rate card" in str(exc.value)


def test_a_backwards_period_is_refused(db):
    client, _ = _seed_client(db)
    _card(db, client)
    db.commit()
    start, end = JULY
    with pytest.raises(billing.BillingError):
        billing.build_invoice(db, client.id, end, start)


def test_only_one_rate_card_per_client_can_be_active(db):
    """Enforced by the schema, not by discipline: without it, "the client's rate
    card" has no answer and an invoice's rates depend on row order."""
    from sqlalchemy.exc import IntegrityError

    client, _ = _seed_client(db)
    _card(db, client, name="First")
    db.commit()
    with pytest.raises(IntegrityError):
        _card(db, client, name="Second")  # also active -> refused by the index
    db.rollback()


# --------------------------------------------------------------------------- #
# 5) Invoice CSV
# --------------------------------------------------------------------------- #
def test_invoice_csv_neutralises_a_hostile_printer_name_but_not_numbers(db):
    """``display_name`` is operator text and ``model`` is an SNMP string off a
    device on a client LAN; this file gets opened in Excel."""
    client, site = _seed_client(db)
    p = _printer(db, client, site, display_name='=HYPERLINK("http://evil/","click")')
    _card(db, client, mono="0.0085")
    _reading(db, p, 1, pages=0, mono=0)
    _reading(db, p, 28, pages=1000, mono=1000)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    header, rows = _rows(billing.invoice_csv(inv))
    line = rows[0]
    assert line[header.index("printer")].startswith("'=HYPERLINK")
    # The numbers are numbers and must stay importable.
    assert line[header.index("pages")] == "1000"
    assert line[header.index("amount")] == "8.50"


def test_invoice_csv_leaves_an_unbilled_amount_blank_not_zero(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client)
    _reading(db, p, 1, pages=1000)
    _reading(db, p, 28, pages=4000)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    header, rows = _rows(billing.invoice_csv(inv))
    unbilled = [r for r in rows if r[header.index("kind")] == "unbilled"]
    assert unbilled and unbilled[0][header.index("amount")] == ""
    assert unbilled[0][header.index("pages")] == "3000"
    total = [r for r in rows if r[header.index("kind")] == "total"][0]
    assert total[header.index("amount")] == "0.00"


def test_invoice_csv_carries_the_minimum_adjustment_row(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client, mono="0.0085", minimum_charge=Decimal("50.00"))
    _reading(db, p, 1, pages=0, mono=0)
    _reading(db, p, 28, pages=1000, mono=1000)
    db.commit()

    inv = billing.build_invoice(db, client.id, *JULY)
    header, rows = _rows(billing.invoice_csv(inv))
    kinds = [r[header.index("kind")] for r in rows]
    assert "minimum_adjustment" in kinds
    total = [r for r in rows if r[header.index("kind")] == "total"][0]
    assert total[header.index("amount")] == "50.00"


# --------------------------------------------------------------------------- #
# 6) C3 -- period columns on the monthly billing CSV
# --------------------------------------------------------------------------- #
def test_billing_csv_adds_period_columns_beside_the_lifetime_ones(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    p.page_count, p.mono_count, p.color_count = 15000, 12000, 3000
    _reading(db, p, 1, pages=10000, mono=8000, color=2000)
    _reading(db, p, 28, pages=15000, mono=12000, color=3000)
    db.commit()

    header, rows = _rows(reports.build_monthly_billing_csv(db, *JULY))
    for col in ("page_count", "mono_count", "color_count",
                "pages_period", "mono_period", "color_period"):
        assert col in header
    row = rows[0]
    # Lifetime columns keep their exact previous meaning.
    assert row[header.index("page_count")] == "15000"
    assert row[header.index("mono_count")] == "12000"
    assert row[header.index("color_count")] == "3000"
    # Period columns are what was printed inside the window.
    assert row[header.index("pages_period")] == "5000"
    assert row[header.index("mono_period")] == "4000"
    assert row[header.index("color_period")] == "1000"


def test_period_columns_never_go_negative_across_a_counter_reset(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _reading(db, p, 1, pages=9000, mono=9000)
    _reading(db, p, 10, pages=9500, mono=9500)
    _reading(db, p, 11, pages=20, mono=20)      # replaced device / reflash
    _reading(db, p, 28, pages=320, mono=320)
    db.commit()

    header, rows = _rows(reports.build_monthly_billing_csv(db, *JULY))
    assert rows[0][header.index("pages_period")] == "800"
    assert rows[0][header.index("mono_period")] == "800"


def test_period_columns_are_blank_when_the_meter_is_unknown(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _reading(db, p, 1, pages=1000)
    _reading(db, p, 28, pages=1400)
    db.commit()

    header, rows = _rows(reports.build_monthly_billing_csv(db, *JULY))
    assert rows[0][header.index("pages_period")] == "400"
    assert rows[0][header.index("mono_period")] == ""
    assert rows[0][header.index("color_period")] == ""


def test_a_hostile_snmp_string_is_still_neutralised_with_the_new_columns(db):
    """The new columns must not have bypassed ``safe_writer`` -- and the numeric
    deltas must not be apostrophe-prefixed, or the billing import breaks."""
    client, site = _seed_client(db)
    p = _printer(db, client, site, model="=cmd|'/c calc'!A1", hostname="@SUM(1+1)")
    _reading(db, p, 1, pages=1000, mono=1000)
    _reading(db, p, 28, pages=1400, mono=1400)
    db.commit()

    header, rows = _rows(reports.build_monthly_billing_csv(db, *JULY))
    row = rows[0]
    assert row[header.index("model")].startswith("'=")
    assert row[header.index("hostname")].startswith("'@")
    assert row[header.index("pages_period")] == "400"
    assert row[header.index("mono_period")] == "400"


def test_billing_csv_defaults_to_the_last_complete_month(db):
    """No explicit window -> the last complete calendar month, never a partial one."""
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    now = datetime.now(timezone.utc)
    start, _ = billing.previous_month(now)
    db.add(m.Reading(printer_id=p.id, ts=start + timedelta(days=1),
                     page_count=1000, status=m.PrinterStatus.ok))
    db.add(m.Reading(printer_id=p.id, ts=start + timedelta(days=20),
                     page_count=1250, status=m.PrinterStatus.ok))
    db.commit()

    header, rows = _rows(reports.build_monthly_billing_csv(db))
    assert rows[0][header.index("pages_period")] == "250"


# --------------------------------------------------------------------------- #
# 7) C4 -- monthly_day must not skip months
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "setting,year,month,expected_day",
    [
        (31, 2026, 2, 28),   # February, non-leap
        (31, 2024, 2, 29),   # February, leap year
        (31, 2026, 4, 30),   # a 30-day month
        (31, 2026, 1, 31),   # a 31-day month: unchanged
        (29, 2026, 2, 28),   # 29 in a non-leap February
        (29, 2024, 2, 29),   # 29 in a leap February: unchanged
        (30, 2026, 2, 28),
        (1, 2026, 2, 1),
        (0, 2026, 2, 1),     # nonsense low value still fires, on the 1st
    ],
)
def test_monthly_day_is_clamped_into_the_month_that_exists(setting, year, month, expected_day):
    assert billing.clamp_day_of_month(setting, year, month) == expected_day


@pytest.mark.parametrize(
    "setting,year,month,day",
    [(31, 2026, 2, 28), (31, 2024, 2, 29), (31, 2026, 4, 30), (29, 2026, 2, 28)],
)
def test_the_monthly_report_actually_sends_on_a_short_month(db, monkeypatch, setting, year,
                                                            month, day):
    """The bug this fixes: ``now.day == want_dom`` loses five of twelve billing
    reports a year at 31, and February three years in four at 29."""
    save_settings(db, {"reports.monthly_enabled": "on",
                       "reports.monthly_day": str(setting),
                       "reports.send_hour": "7",
                       "reports.recipients": "billing@msp.example"})
    sent = {}

    def _fake_deliver(db_, rt, subject, body, attachments=None):
        sent["subject"] = subject
        sent["body"] = body
        sent["attachments"] = attachments
        return True, "ok"

    monkeypatch.setattr(reports, "_deliver", _fake_deliver)
    out = reports.run_scheduled_reports(
        db, now=datetime(year, month, day, 9, 0, tzinfo=timezone.utc)
    )
    assert out["monthly_report"] == "sent", out
    assert sent["attachments"][0][0] == f"printer-nanny-billing-{year}-{month:02d}.csv"
    # The body has to say which window the period columns cover -- "the June
    # report" and "June's pages" are different things.
    assert "Billing period:" in sent["body"]


def test_the_monthly_report_still_does_not_fire_on_the_wrong_day(db, monkeypatch):
    save_settings(db, {"reports.monthly_enabled": "on", "reports.monthly_day": "31",
                       "reports.send_hour": "7", "reports.recipients": "b@msp.example"})
    monkeypatch.setattr(reports, "_deliver", lambda *a, **k: (True, "ok"))
    out = reports.run_scheduled_reports(
        db, now=datetime(2026, 2, 27, 9, 0, tzinfo=timezone.utc)
    )
    assert out["monthly_report"] == "skipped"


def test_the_monthly_report_bills_the_last_complete_month(db, monkeypatch):
    """Sent in July, it bills June -- and says so."""
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _reading(db, p, 2, pages=1000, mono=1000, month=6)
    _reading(db, p, 28, pages=1700, mono=1700, month=6)
    _reading(db, p, 5, pages=9999, mono=9999, month=7)  # July: must not be billed
    save_settings(db, {"reports.monthly_enabled": "on", "reports.monthly_day": "1",
                       "reports.send_hour": "7", "reports.recipients": "b@msp.example"})
    db.commit()
    captured = {}

    def _fake_deliver(db_, rt, subject, body, attachments=None):
        captured["csv"] = attachments[0][2]
        captured["body"] = body
        return True, "ok"

    monkeypatch.setattr(reports, "_deliver", _fake_deliver)
    out = reports.run_scheduled_reports(
        db, now=datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)
    )
    assert out["monthly_report"] == "sent"
    header, rows = _rows(captured["csv"])
    assert rows[0][header.index("pages_period")] == "700"
    assert "2026-06-01 to 2026-07-01" in captured["body"]


# --------------------------------------------------------------------------- #
# 8) The operator surface
# --------------------------------------------------------------------------- #
def test_billing_page_is_admin_only(db):
    _seed_client(db)
    db.commit()
    tech = _admin_client(db, "tech", "techpw", role=m.UserRole.tech)
    assert tech.get("/manage/billing", follow_redirects=False).status_code == 303
    assert tech.post("/manage/billing/rate-cards",
                     data={"client_id": "1", "name": "x", "mono_rate": "0.01",
                           "color_rate": "0.02"},
                     follow_redirects=False).status_code == 303
    assert db.scalar(select(m.BillingRateCard.id)) is None


def test_an_admin_can_create_activate_and_price_a_rate_card(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _reading(db, p, 1, pages=0, mono=0, color=0)
    _reading(db, p, 28, pages=5000, mono=4000, color=1000)
    db.commit()
    http = _admin_client(db)

    resp = http.post("/manage/billing/rate-cards", data={
        "client_id": str(client.id), "name": "Standard MPS", "currency": "gbp",
        "mono_rate": "0.0085", "color_rate": "0.0720",
        "minimum_charge": "", "unsplit_policy": "exclude", "notes": "contract 42",
    }, follow_redirects=False)
    assert resp.status_code == 303

    card = db.scalar(select(m.BillingRateCard))
    assert card.currency == "GBP"
    assert card.mono_rate == Decimal("0.008500")
    assert card.active is True

    page = http.get(f"/manage/billing?client_id={client.id}&month=2026-07")
    assert page.status_code == 200
    # 4000 * 0.0085 = 34.00, 1000 * 0.072 = 72.00 -> 106.00
    assert "106.00" in page.text
    assert "Standard MPS" in page.text


def test_an_invalid_rate_is_refused_rather_than_stored(db):
    client, _ = _seed_client(db)
    db.commit()
    http = _admin_client(db)
    resp = http.post("/manage/billing/rate-cards", data={
        "client_id": str(client.id), "name": "Bad", "currency": "USD",
        "mono_rate": "nan", "color_rate": "0.05",
    }, follow_redirects=True)
    assert resp.status_code == 200
    assert db.scalar(select(m.BillingRateCard.id)) is None
    assert "not created" in resp.text


def test_a_negative_rate_is_refused(db):
    client, _ = _seed_client(db)
    db.commit()
    http = _admin_client(db)
    http.post("/manage/billing/rate-cards", data={
        "client_id": str(client.id), "name": "Credit", "currency": "USD",
        "mono_rate": "-0.01", "color_rate": "0.05",
    }, follow_redirects=False)
    assert db.scalar(select(m.BillingRateCard.id)) is None


def test_editing_a_card_under_the_wrong_client_is_refused(db):
    acme, _ = _seed_client(db, "Acme")
    beta, _ = _seed_client(db, "Beta")
    beta_card = _card(db, beta, mono="0.0100")
    db.commit()
    http = _admin_client(db)

    resp = http.post(f"/manage/billing/rate-cards/{beta_card.id}", data={
        "client_id": str(acme.id), "mono_rate": "9.999999", "minimum_charge": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(m.BillingRateCard, beta_card.id).mono_rate == Decimal("0.010000")


def test_activating_a_card_retires_the_incumbent(db):
    client, _ = _seed_client(db)
    first = _card(db, client, name="First")
    db.commit()
    second = m.BillingRateCard(client_id=client.id, name="Second",
                               mono_rate=Decimal("0.01"), color_rate=Decimal("0.09"),
                               active=False)
    db.add(second)
    db.commit()
    http = _admin_client(db)

    resp = http.post(f"/manage/billing/rate-cards/{second.id}/activate",
                     data={"client_id": str(client.id)}, follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert db.get(m.BillingRateCard, first.id).active is False
    assert db.get(m.BillingRateCard, second.id).active is True


def test_volume_bands_can_be_added_and_removed(db):
    client, _ = _seed_client(db)
    card = _card(db, client)
    db.commit()
    http = _admin_client(db)

    http.post(f"/manage/billing/rate-cards/{card.id}/tiers", data={
        "client_id": str(client.id), "kind": "mono", "up_to": "5000", "rate": "0.0120",
    }, follow_redirects=False)
    db.expire_all()
    tier = db.scalar(select(m.BillingRateTier))
    assert tier.up_to == 5000 and tier.rate == Decimal("0.012000")

    http.post(f"/manage/billing/tiers/{tier.id}/delete",
              data={"client_id": str(client.id)}, follow_redirects=False)
    db.expire_all()
    assert db.scalar(select(m.BillingRateTier)) is None


def test_a_band_ceiling_of_zero_is_refused(db):
    client, _ = _seed_client(db)
    card = _card(db, client)
    db.commit()
    http = _admin_client(db)
    http.post(f"/manage/billing/rate-cards/{card.id}/tiers", data={
        "client_id": str(client.id), "kind": "mono", "up_to": "0", "rate": "0.01",
    }, follow_redirects=False)
    assert db.scalar(select(m.BillingRateTier)) is None


def test_the_invoice_csv_download_is_audited(db):
    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client, mono="0.0100")
    _reading(db, p, 1, pages=0, mono=0)
    _reading(db, p, 28, pages=1000, mono=1000)
    db.commit()
    http = _admin_client(db)

    resp = http.get(f"/manage/billing/invoice.csv?client_id={client.id}&month=2026-07")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment;" in resp.headers["content-disposition"]
    header, rows = _rows(resp.content)
    assert [r for r in rows if r[header.index("kind")] == "total"][0][header.index("amount")] \
        == "10.00"

    entry = db.scalar(
        select(m.AuditLog).where(m.AuditLog.action == "billing.invoice.export")
    )
    assert entry is not None
    assert "2026-07" in entry.detail and "10.00" in entry.detail


def test_every_control_on_a_POPULATED_billing_page_has_an_accessible_name(db):
    """The shared a11y sweep renders this page against an empty database, so it
    sees the "no clients yet" branch and none of the per-card controls. Those
    live inside a loop -- exactly where an id-based label scheme silently
    mis-associates every row after the first -- so they need their own check.
    """
    from tests.test_dashboard_a11y import _parse

    client, _ = _seed_client(db)
    card = _card(db, client, minimum_charge=Decimal("50.00"))
    db.add(m.BillingRateTier(rate_card_id=card.id, kind=m.MeterClass.mono,
                             up_to=5000, rate=Decimal("0.012")))
    db.commit()
    http = _admin_client(db)

    parsed = _parse(http.get(f"/manage/billing?client_id={client.id}").text)
    unlabelled = [
        c for c in parsed.controls
        if not c["wrapped"] and not c["aria"]
        and not (c["id"] and c["id"] in parsed.label_for)
    ]
    assert not unlabelled, [f"<{c['tag']} name={c['name']}>" for c in unlabelled]
    assert parsed.controls, "the page rendered no controls; this test is checking nothing"


def test_the_billing_page_uses_the_component_layer_not_inline_tailwind(db):
    """Cards must carry min-w-0 and table scrollers must be positioned.

    Both are load-bearing layout that only fails on a narrow viewport, and the
    shared sweep again only sees this page's empty state.
    """
    import re as _re

    client, site = _seed_client(db)
    p = _printer(db, client, site)
    _card(db, client)
    _reading(db, p, 1, pages=0, mono=0)
    _reading(db, p, 28, pages=100, mono=100)
    db.commit()
    http = _admin_client(db)
    html = http.get(f"/manage/billing?client_id={client.id}&month=2026-07").text

    cards = _re.findall(r'class="([^"]*bg-white rounded-lg shadow[^"]*)"', html)
    assert cards
    assert all("min-w-0" in c for c in cards), cards
    wrappers = _re.findall(r'class="([^"]*overflow-x-auto[^"]*)"', html)
    assert wrappers
    assert all("relative" in w for w in wrappers), wrappers


def test_the_page_explains_itself_when_there_is_no_rate_card(db):
    client, _ = _seed_client(db)
    db.commit()
    http = _admin_client(db)
    page = http.get(f"/manage/billing?client_id={client.id}")
    assert page.status_code == 200
    assert "no active rate card" in page.text


def test_rate_card_changes_are_audited(db):
    client, _ = _seed_client(db)
    db.commit()
    http = _admin_client(db)
    http.post("/manage/billing/rate-cards", data={
        "client_id": str(client.id), "name": "Standard", "currency": "USD",
        "mono_rate": "0.0085", "color_rate": "0.0720",
    }, follow_redirects=False)

    entry = db.scalar(
        select(m.AuditLog).where(m.AuditLog.action == "billing.rate_card.create")
    )
    assert entry is not None
    assert "0.008500" in entry.detail
