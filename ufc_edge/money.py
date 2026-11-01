"""Money handling.

All money is held as integer pence. Floats are never used for balances,
because binary floating point cannot represent most decimal amounts exactly
and rounding drift in a ledger is unacceptable.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal


def pounds_to_pence(pounds: str | int | Decimal) -> int:
    """Convert a pound amount such as "40" or "12.50" to pence, exactly."""
    value = Decimal(str(pounds)) * 100
    if value != value.to_integral_value():
        raise ValueError(f"Amount {pounds!r} has more than two decimal places.")
    return int(value)


def fmt(pence: int) -> str:
    """Format pence as a pound string, e.g. 4050 -> '£40.50', -250 -> '-£2.50'."""
    sign = "-" if pence < 0 else ""
    pence = abs(pence)
    return f"{sign}£{pence // 100}.{pence % 100:02d}"


def winning_return_pence(stake_pence: int, decimal_odds: Decimal) -> int:
    """Total return (stake + profit) for a winning bet, rounded *down* to the penny.

    Rounding down means the ledger can never credit more than the bookmaker
    actually pays. Any shortfall is caught at reconciliation.
    """
    if decimal_odds <= 1:
        raise ValueError("Decimal odds must be greater than 1.")
    return int((Decimal(stake_pence) * decimal_odds).to_integral_value(rounding=ROUND_FLOOR))
