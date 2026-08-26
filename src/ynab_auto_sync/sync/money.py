from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

# Internal canonical precision - always 3 decimal places, matching YNAB's own
# milliunits granularity. This is fixed and unrelated to a deployment's
# currency_decimal_places config: 3 decimal places is the actual ceiling
# YNAB's API can represent, regardless of which currency is in use.
_INTERNAL_QUANTUM = Decimal("0.001")

MILLIUNITS_PER_MAJOR_UNIT = Decimal(1000)


def to_milliunits(amount: Decimal) -> int:
    """Convert a major-unit currency amount (e.g. Decimal("158.48") kr) to
    YNAB's int milliunits. ROUND_HALF_EVEN matches Python's builtin round()
    semantics exactly, so this is a behavioral match for the
    round(float(amount) * 1000) it replaces - see CLAUDE.md's dedup-key
    stability requirement for why that match must be exact, not approximate.
    """
    return int((amount * MILLIUNITS_PER_MAJOR_UNIT).to_integral_value(rounding=ROUND_HALF_EVEN))


def from_milliunits(milliunits: int) -> Decimal:
    """Inverse of to_milliunits(). Always exact - 1000 is a power of ten."""
    return (Decimal(milliunits) / MILLIUNITS_PER_MAJOR_UNIT).quantize(_INTERNAL_QUANTUM)


def parse_provider_amount(raw: str | float, decimal_places: int) -> Decimal:
    """Convert a raw provider value (JSON number, xlsx cell, etc.) to an
    exact Decimal, quantized to decimal_places.

    Critically: for a float input this goes through str(raw), never
    Decimal(raw) directly. Decimal(raw) on a float inherits its binary
    representation noise (e.g. Decimal(0.1) ==
    Decimal("0.1000000000000000055511151231257827021181583404541015625")).
    str(raw) on a Python float uses the shortest-round-trip repr, which
    recovers the original decimal text for any realistic bank amount.
    """
    if isinstance(raw, float):
        value = Decimal(str(raw))
    else:
        value = Decimal(raw)
    quantum = Decimal(1).scaleb(-decimal_places)
    return value.quantize(quantum, rounding=ROUND_HALF_EVEN)
