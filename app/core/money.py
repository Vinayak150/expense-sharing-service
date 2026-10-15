"""Exact INR boundary conversion, independent of HTTP and persistence."""

import re
from decimal import Context, Decimal, InvalidOperation, localcontext

MAX_AMOUNT_PAISE = 100_000_000_000
MAX_AMOUNT_RUPEES = Decimal("1000000000.00")
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def rupees_to_paise(value: Decimal | int | str, *, allow_zero: bool = False) -> int:
    """Accept exact decimal values; reject floats, booleans and fractional paise."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise ValueError("Amount must be a decimal number or decimal string")
    if isinstance(value, str):
        if len(value) > 128 or not _DECIMAL.fullmatch(value):
            raise ValueError(
                "Amount must be a valid decimal representation of at most 128 characters"
            )
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("Amount must be a valid decimal number") from None
    if not amount.is_finite():
        raise ValueError("Amount must be finite")
    minimum = Decimal("0") if allow_zero else Decimal("0.01")
    if amount < minimum or amount > MAX_AMOUNT_RUPEES:
        raise ValueError(f"Amount must be between {minimum} and 1000000000.00 rupees")
    # Reject tiny nonzero custom shares before multiplication can underflow in
    # Decimal's finite exponent range. Zero shares remain valid; never round one.
    if 0 < amount < Decimal("0.01"):
        raise ValueError("Amount cannot contain fractional paise")
    digits = len(amount.as_tuple().digits)
    if digits > 128:
        raise ValueError("Amount representation is too long")
    # Independent of the caller's Decimal context; precision accommodates every digit.
    with localcontext(Context(prec=max(32, digits + 3))):
        scaled = amount * 100
        if scaled != scaled.to_integral_value():
            raise ValueError("Amount cannot contain fractional paise")
        return int(scaled)


def paise_to_rupees(value: int) -> str:
    """Canonical rupee string for totals and shares (including zero)."""
    if type(value) is not int or value < 0:
        raise ValueError("Paise must be a nonnegative integer")
    return f"{value // 100}.{value % 100:02d}"
