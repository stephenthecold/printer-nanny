"""Exact fixed-point money, on both backends, plus the one rounding rule.

**Money is never a float here.** Cost-per-page contracts are priced in
fractions of a cent (``0.0085``/page is an ordinary mono rate), an invoice
multiplies that by five-figure page counts, and binary floating point cannot
represent either operand exactly. ``0.1 + 0.2`` is the toy example; the real one
is an MSP's invoice total disagreeing with the sum of its own lines by a cent
and nobody being able to say which number is wrong.

WHY A TYPE DECORATOR RATHER THAN ``Numeric``
--------------------------------------------
SQLAlchemy's ``Numeric`` is exact on Postgres and *not* on SQLite: the SQLite
dialect has no decimal storage, so it stores a float and converts back, warning
``Dialect sqlite+pysqlite does *not* support Decimal objects natively, and
SQLAlchemy must convert from floating point - rounding errors and other issues
may occur``. This project runs its entire test suite and every local dev
install on SQLite, so "exact on the backend nobody develops against" is exactness
in name only -- the arithmetic would be verified against the wrong storage.

``FixedPoint`` therefore stores NUMERIC on Postgres and a **zero-padded,
fixed-scale decimal string** on SQLite. The padding is not decoration: it makes
the text form sort lexicographically in the same order as the number, so
``ORDER BY mono_rate`` means the same thing on both backends. Values are
non-negative by construction (see ``parse_rate``/``parse_amount``), which is what
makes that hold -- a negative value would sort backwards, so the bind path
refuses one rather than storing something that silently mis-sorts.

Two constraints that come with the SQLite variant, stated so they are not
discovered later:

* **Never SUM or AVG a fixed-point column in SQL.** On SQLite it is text, and
  SQLite would coerce it to a float -- reintroducing exactly what this type
  exists to prevent. All arithmetic in this app happens in Python ``Decimal``.
  Ordering and equality are safe; aggregation is not.
* A value that will not fit the declared precision raises at bind time rather
  than being silently truncated to a different number.

THE ROUNDING RULE
-----------------
**Half-up, at the invoice line, once.** ``round_money`` quantizes to two decimal
places with ``ROUND_HALF_UP``. Python's default is banker's rounding
(``ROUND_HALF_EVEN``), which is defensible statistically and is *not* what an
invoice does or what a customer checking it with a calculator expects.

Rounding happens at the line and nowhere else. Pages are multiplied by a
six-decimal rate at full precision, graduated bands are summed at full
precision, and the single quantization is applied to the resulting line amount.
Rounding per page would drift by up to half a cent times the page count -- on
40,000 pages that is $200 of pure artefact. The invoice total is then the exact
sum of already-rounded lines, so the printed lines always add up to the printed
total.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

from sqlalchemy import Numeric, String
from sqlalchemy.types import TypeDecorator

__all__ = [
    "CENTS",
    "FixedPoint",
    "Money",
    "Rate",
    "format_money",
    "format_rate",
    "parse_amount",
    "parse_rate",
    "round_money",
    "to_decimal",
]

#: The quantum every money amount is rounded to.
CENTS = Decimal("0.01")

#: Scale used for cost-per-page rates. Six decimals is the industry spelling of
#: a CPP contract ("$0.008500 per mono page"); two would round most real rates
#: to zero.
RATE_SCALE = 6
RATE_PRECISION = 12
MONEY_SCALE = 2
MONEY_PRECISION = 12

# A plain decimal literal. Deliberately NOT ``Decimal(raw)``, which happily
# accepts "nan", "inf", "-Infinity" and "1_0" -- an operator typing NaN into a
# rate field would otherwise poison every invoice that card ever produces, and
# NaN compares false to everything so no downstream check would catch it.
_DECIMAL_LITERAL = re.compile(r"^\+?(?:\d+(?:\.\d*)?|\.\d+)$")


class FixedPoint(TypeDecorator):
    """Exact fixed-point decimal: NUMERIC on Postgres, padded text on SQLite.

    ``precision``/``scale`` mean what they mean in SQL: total significant digits
    and digits after the point.
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int, scale: int) -> None:
        super().__init__(precision=precision, scale=scale, asdecimal=True)
        self.precision = precision
        self.scale = scale
        # precision digits + the '.' == the fixed text width on SQLite.
        self._width = precision + 1
        self._quantum = Decimal(1).scaleb(-scale)

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(self._width))
        return dialect.type_descriptor(
            Numeric(precision=self.precision, scale=self.scale, asdecimal=True)
        )

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        dec = to_decimal(value)
        if dec < 0:
            raise ValueError(
                "fixed-point columns in this schema are non-negative "
                f"(got {dec}); a credit is not modelled as a negative rate"
            )
        quantized = dec.quantize(self._quantum, rounding=ROUND_HALF_UP)
        integral_digits = len(quantized.as_tuple().digits) + quantized.as_tuple().exponent
        if integral_digits > self.precision - self.scale:
            raise ValueError(
                f"{quantized} does not fit NUMERIC({self.precision},{self.scale})"
            )
        if dialect.name == "sqlite":
            # Zero-padded to a fixed width so text ordering == numeric ordering.
            return "{:0{w}.{s}f}".format(quantized, w=self._width, s=self.scale)
        return quantized

    def process_result_value(self, value: Any, dialect: Any) -> Optional[Decimal]:
        if value is None:
            return None
        # str() first: on Postgres this is already a Decimal (no-op round trip),
        # on SQLite it is the padded text, and either way we never touch a float.
        return Decimal(str(value)).quantize(self._quantum)

    # ----------------------------------------------------------------- #
    # The processor chain stops here, deliberately.
    #
    # ``TypeDecorator`` normally composes ``process_bind_param`` with the
    # *impl's* processor, and ``sqlalchemy.Numeric.bind_processor`` returns
    # ``processors.to_float`` on any dialect that does not advertise native
    # decimal support -- ``DefaultDialect.supports_native_decimal`` is False,
    # and both ``PGDialect`` and ``SQLiteDialect`` inherit that False. One line
    # of SQLAlchemy would therefore convert the exact Decimal this module exists
    # to preserve into a float on the way to the driver, silently, and the only
    # symptom would be an invoice whose lines stop adding up to its total.
    #
    # Today neither supported backend actually reaches that line (SQLite is
    # handed a String impl above, and Postgres adapts Numeric to
    # ``_PsycopgNumeric``, whose bind processor is None), so this override
    # changes no current behaviour. It is here so that remaining true is not
    # contingent on a dialect internal nobody would think to re-check.
    # ----------------------------------------------------------------- #
    def bind_processor(self, dialect: Any) -> Any:
        def process(value: Any) -> Any:
            return self.process_bind_param(value, dialect)

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: Any) -> Optional[Decimal]:
            return self.process_result_value(value, dialect)

        return process


def Money() -> FixedPoint:  # noqa: N802 - reads as a type at the call site
    """A currency amount: 10 integer digits, 2 decimals."""
    return FixedPoint(MONEY_PRECISION, MONEY_SCALE)


def Rate() -> FixedPoint:  # noqa: N802 - reads as a type at the call site
    """A cost-per-page rate: 6 integer digits, 6 decimals."""
    return FixedPoint(RATE_PRECISION, RATE_SCALE)


def to_decimal(value: Any) -> Decimal:
    """Coerce to ``Decimal`` without ever passing through binary float.

    A ``float`` argument is refused outright rather than converted: by the time
    a float exists the precision is already gone, and accepting it here is how
    float money creeps back in one call site at a time.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("money must be a finite decimal")
        return value
    if isinstance(value, bool):  # bool is an int; billing at "True" per page is not a thing
        raise ValueError("money must be a number, not a bool")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        raise ValueError(
            "refusing to build money from a float -- pass a str, int or Decimal"
        )
    if isinstance(value, str):
        return _parse_literal(value)
    raise ValueError(f"cannot interpret {type(value).__name__} as money")


def _parse_literal(raw: str) -> Decimal:
    text = raw.strip()
    if not _DECIMAL_LITERAL.match(text):
        raise ValueError(f"not a non-negative decimal number: {raw!r}")
    try:
        dec = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - regex already rejected these
        raise ValueError(f"not a decimal number: {raw!r}") from exc
    if not dec.is_finite():  # pragma: no cover - regex already rejected these
        raise ValueError(f"not a finite number: {raw!r}")
    return dec


def parse_rate(raw: Any, *, field: str = "rate") -> Decimal:
    """Operator input -> a cost-per-page rate. Raises ``ValueError`` on junk.

    Negative rates are refused: a negative cost per page is a credit, which is a
    different document, and allowing one here would let a typo turn an invoice
    into a refund with nothing flagging it.
    """
    try:
        dec = to_decimal(raw)
    except ValueError as exc:
        raise ValueError(f"{field}: {exc}") from exc
    if dec < 0:
        raise ValueError(f"{field}: must not be negative")
    quantized = dec.quantize(Decimal(1).scaleb(-RATE_SCALE), rounding=ROUND_HALF_UP)
    if len(quantized.as_tuple().digits) + quantized.as_tuple().exponent > RATE_PRECISION - RATE_SCALE:
        raise ValueError(f"{field}: too large (max 6 digits before the decimal point)")
    return quantized


def parse_amount(raw: Any, *, field: str = "amount") -> Decimal:
    """Operator input -> a currency amount (2dp, half-up)."""
    try:
        dec = to_decimal(raw)
    except ValueError as exc:
        raise ValueError(f"{field}: {exc}") from exc
    if dec < 0:
        raise ValueError(f"{field}: must not be negative")
    quantized = round_money(dec)
    if len(quantized.as_tuple().digits) + quantized.as_tuple().exponent > MONEY_PRECISION - MONEY_SCALE:
        raise ValueError(f"{field}: too large")
    return quantized


def round_money(amount: Decimal) -> Decimal:
    """The project's one rounding rule: 2dp, ROUND_HALF_UP.

    Applied at the invoice line and nowhere else -- see the module docstring.
    """
    return to_decimal(amount).quantize(CENTS, rounding=ROUND_HALF_UP)


def format_money(amount: Optional[Decimal]) -> str:
    """Render an amount for display/CSV. ``None`` renders blank, never ``0.00``.

    Blank and zero are different facts: "we could not price this" versus "this
    cost nothing". Collapsing them is how an unknown meter gets billed as zero.
    """
    if amount is None:
        return ""
    return "{:.2f}".format(to_decimal(amount))


def format_rate(rate: Optional[Decimal]) -> str:
    if rate is None:
        return ""
    return "{:.6f}".format(to_decimal(rate))
