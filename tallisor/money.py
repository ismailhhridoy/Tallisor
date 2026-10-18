"""Money helpers. All amounts inside the engine are integer minor units (cents).

Floats are never used for money: every parse goes through Decimal and every
calculation is integer arithmetic, so totals reconcile to the cent.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

MINUS = "−"
_DECIMAL_COMMA = re.compile(r"^\(?-?(\d{1,3}(\.\d{3})+|\d+),\d{1,2}\)?$")


class MoneyParseError(ValueError):
    pass


def to_cents(value) -> int:
    """Parse '1,234.56', '-12.5', '(45.00)', '$9.99' or a number into cents."""
    if value is None:
        raise MoneyParseError("empty amount")
    if isinstance(value, int):
        return value * 100
    text = str(value).strip().replace("$", "").replace(MINUS, "-").replace(" ", "").replace(" ", "")
    if text == "":
        raise MoneyParseError("empty amount")
    # Decimal-comma style used across Europe, Latin America and many bank exports:
    # "10,00", "1.234,56", "-0,5". A comma followed by exactly one or two digits at
    # the end is a decimal mark; otherwise commas are thousands separators.
    if _DECIMAL_COMMA.match(text):
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    try:
        dec = Decimal(text)
    except InvalidOperation as exc:
        raise MoneyParseError(f"not an amount: {value!r}") from exc
    cents = int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return -cents if negative else cents


def to_cents_or_zero(value) -> int:
    try:
        return to_cents(value)
    except MoneyParseError:
        return 0


def fee_cents(amount_cents: int, basis_points: int, fixed_cents: int) -> int:
    """Percentage-plus-fixed card fee, rounded half up, on a positive amount."""
    amount = abs(amount_cents)
    return (amount * basis_points + 5000) // 10000 + fixed_cents


def convert(amount_minor: int, rate: Decimal) -> int:
    """Convert minor units with an exchange rate, rounded half up."""
    return int((Decimal(amount_minor) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt(cents: int, symbol: str = "$", signed: bool = False) -> str:
    sign = ""
    if cents < 0:
        sign = MINUS
    elif signed and cents > 0:
        sign = "+"
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{symbol}{whole:,}.{frac:02d}"
