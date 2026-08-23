"""Small shared helpers.

Deliberately tiny — this is not a junk drawer. Anything here must be needed by
more than one module and carry no business logic.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = ["format_days"]


def format_days(value: Decimal | int | float | str) -> str:
    """Render a day count for humans: 15.00 -> '15', 1.500 -> '1.5'.

    Decimal's 'g' format does NOT strip trailing zeros the way float's does —
    f"{Decimal('15.00'):g}" is '15.00', not '15'. That surprise has bitten this
    codebase twice (the dashboard's bracket-change note and the classification
    explain line), so the fix lives in one place.

    normalize() handles the stripping; the integral guard stops a whole number
    like 100 from rendering in exponent form as '1E+2'.
    """
    normalised = Decimal(str(value)).normalize()
    if normalised == normalised.to_integral_value():
        normalised = normalised.quantize(Decimal("1"))
    return f"{normalised:f}"
