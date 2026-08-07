from decimal import Decimal

import pytest

from app.domain.limit_up import LimitUpSource, evaluate_limit_up
from app.domain.price_ticks import calculate_limit_up_price, get_tick_size


def test_tick_size_100_to_500_is_single_band():
    """Regression test: 100~500 must not be wrongly split into two bands."""
    assert get_tick_size(Decimal("120")) == Decimal("0.50")
    assert get_tick_size(Decimal("300")) == Decimal("0.50")
    assert get_tick_size(Decimal("499.99")) == Decimal("0.50")


@pytest.mark.parametrize(
    "price,expected_tick",
    [
        (Decimal("8"), Decimal("0.01")),
        (Decimal("30"), Decimal("0.05")),
        (Decimal("80"), Decimal("0.10")),
        (Decimal("700"), Decimal("1.00")),
        (Decimal("1500"), Decimal("5.00")),
    ],
)
def test_tick_size_bands(price, expected_tick):
    assert get_tick_size(price) == expected_tick


def test_calculate_limit_up_price_official_example():
    """Cross-checked against the TWSE official worked example:
    reference price 40.60 -> limit-up price 44.65."""
    assert calculate_limit_up_price(Decimal("40.60")) == Decimal("44.65")


def _walk_up_limit_price(reference_price: Decimal) -> Decimal:
    """
    Reference implementation: walk the price upward tick by tick to
    find the legal limit-up price. Used to verify that
    calculate_limit_up_price()'s faster approach ("round down using
    the tick size of raw_limit's own band") produces the same answer
    when the walk crosses a tick-size band boundary.
    """
    maximum_price = reference_price * Decimal("1.10")
    current = reference_price
    last_valid = reference_price
    while True:
        tick = get_tick_size(current)
        candidate = current + tick
        if candidate > maximum_price:
            return last_valid
        last_valid = candidate
        current = candidate


@pytest.mark.parametrize(
    "reference_price",
    [
        Decimal("48"),      # limit-up crosses the 50 boundary (48*1.1=52.8)
        Decimal("9.5"),     # crosses the 10 boundary
        Decimal("95"),      # crosses the 100 boundary
        Decimal("454.5"),   # crosses the 500 boundary
        Decimal("909"),     # crosses the 1000 boundary
    ],
)
def test_limit_up_price_matches_walk_up_across_tick_boundaries(reference_price):
    """
    Regression test: confirm that "round down using raw_limit's own
    tick size" and "walk upward tick by tick" agree even when the walk
    crosses a tick-size band boundary (they are mathematically
    equivalent under this tick table, because every band boundary is
    an integer multiple of both the tick size before and after it).
    """
    assert calculate_limit_up_price(reference_price) == _walk_up_limit_price(reference_price)


def test_evaluate_limit_up_true_case():
    result = evaluate_limit_up(
        security_type="COMMON_STOCK",
        close_price=Decimal("44.65"),
        source_limit_up_price=Decimal("44.65"),
        reference_price=Decimal("40.60"),
        has_price_limit_today=True,
        data_quality_ok=True,
    )
    assert result.is_limit_up is True
    assert result.limit_up_source == LimitUpSource.SOURCE_PROVIDED


def test_evaluate_limit_up_false_when_close_below_limit():
    result = evaluate_limit_up(
        security_type="COMMON_STOCK",
        close_price=Decimal("44.60"),
        source_limit_up_price=Decimal("44.65"),
        reference_price=Decimal("40.60"),
        has_price_limit_today=True,
        data_quality_ok=True,
    )
    assert result.is_limit_up is False


def test_evaluate_limit_up_non_common_stock_excluded():
    result = evaluate_limit_up(
        security_type="ETF",
        close_price=Decimal("44.65"),
        source_limit_up_price=Decimal("44.65"),
        reference_price=Decimal("40.60"),
        has_price_limit_today=True,
        data_quality_ok=True,
    )
    assert result.is_limit_up is False
