"""
Tick size table and limit-up price calculation for TWSE common stocks.

Official basis: Taiwan Stock Exchange Operating Rules, Articles 62-63,
current regime effective since 2005-03-01. Applies only to common
stocks / depositary receipts and similar general securities; ETFs,
warrants, convertible bonds, etc. have their own tick tables and must
not share this module — build separate functions for those instruments.

Tick table (correct version, 6 bands only; note that 100~500 is a
SINGLE band, not split into 100~150 / 150~500 — that was an error in
the original requirements document that has been fixed here, since it
would have produced wrong limit-up prices for stocks priced 150~500):

    price < 10                 -> 0.01
    10   <= price < 50          -> 0.05
    50   <= price < 100         -> 0.10
    100  <= price < 500         -> 0.50
    500  <= price < 1000        -> 1.00
    price >= 1000               -> 5.00
"""

from decimal import ROUND_HALF_UP, Decimal

# (lower bound [inclusive], upper bound [exclusive]) -> tick size
_TICK_TABLE: list[tuple[Decimal, Decimal, Decimal]] = [
    (Decimal("0"), Decimal("10"), Decimal("0.01")),
    (Decimal("10"), Decimal("50"), Decimal("0.05")),
    (Decimal("50"), Decimal("100"), Decimal("0.10")),
    (
        Decimal("100"),
        Decimal("500"),
        Decimal("0.50"),
    ),  # fixed: was wrongly split into two bands
    (Decimal("500"), Decimal("1000"), Decimal("1.00")),
    (Decimal("1000"), Decimal("999999999"), Decimal("5.00")),
]

LIMIT_UP_RATIO = Decimal("1.10")  # standard 10% price limit for common stocks


def get_tick_size(price: Decimal) -> Decimal:
    """Return the tick size applicable to a common stock at the given price."""
    for lower, upper, tick in _TICK_TABLE:
        if lower <= price < upper:
            return tick
    raise ValueError(f"price out of supported range: {price}")


def round_to_tick(price: Decimal, tick: Decimal, *, round_down: bool) -> Decimal:
    """
    Snap a price to the nearest valid tick.

    round_down=True  used when the price must not exceed the limit-up
                      cap; rounds down to the nearest valid tick.
    round_down=False used for general rounding scenarios.
    """
    ticks = price / tick
    if round_down:
        ticks = ticks.to_integral_value(rounding="ROUND_DOWN")
    else:
        ticks = ticks.to_integral_value(rounding=ROUND_HALF_UP)
    return (ticks * tick).quantize(tick)


def calculate_limit_up_price(reference_price: Decimal) -> Decimal:
    """
    Calculate the "legal" limit-up price from the opening reference price.

    Matches the official worked example:
        1. raw_limit = reference_price * 1.10
        2. Round raw_limit down to the tick size of the band it falls
           in, ensuring the result never exceeds the 10% cap.

    This function is only a fallback for when the data source does not
    directly provide a limit-up price. The formal precedence should
    always prefer a source-provided value (see app/domain/limit_up.py).
    """
    if reference_price <= 0:
        raise ValueError("reference_price must be positive")

    raw_limit = reference_price * LIMIT_UP_RATIO
    tick = get_tick_size(raw_limit)
    return round_to_tick(raw_limit, tick, round_down=True)
