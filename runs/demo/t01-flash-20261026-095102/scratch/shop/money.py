"""Money helpers. All amounts are integer cents."""

from decimal import ROUND_HALF_UP, Decimal


def to_cents(amount: str | int | float | Decimal) -> int:
    """Parse a dollar amount such as ``"12.34"`` into cents, rounding half up."""
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(value * 100)


def round_half_up(value: Decimal | int | float | str) -> int:
    """Round to a whole number, with .5 going away from zero."""
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def format_cents(cents: int, symbol: str = "$") -> str:
    """Format cents as text, e.g. ``1234`` -> ``"$12.34"`` and ``-150`` -> ``"-$1.50"``."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{symbol}{whole}.{frac:02d}"


def split_evenly(total_cents: int, parts: int) -> list[int]:
    """Split ``total_cents`` into ``parts`` shares that differ by at most one cent."""
    if parts <= 0:
        raise ValueError("parts must be positive")
    base, extra = divmod(total_cents, parts)
    return [base + 1 if i < extra else base for i in range(parts)]
