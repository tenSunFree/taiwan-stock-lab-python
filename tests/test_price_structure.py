import datetime as dt

import pytest

from app.domain.feature_builder import HistoricalPricePoint
from app.domain.price_structure import (
    RANGE_WINDOW,
    build_low_level_position,
    build_range_20d,
    build_valid_history,
    has_valid_candle,
)

START = dt.date(2026, 1, 1)


def pt(i, *, o=100.0, h=102.0, l=98.0, c=100.0, v=1000.0):
    return HistoricalPricePoint(
        trading_date=START + dt.timedelta(days=i),
        close=c,
        volume=v,
        turnover=v * c,
        open=o,
        high=h,
        low=l,
    )


def flat(n, **kw):
    return [pt(i, **kw) for i in range(n)]


def test_range_is_max_high_min_low_of_last_20_sessions():
    history = flat(30)
    history[2] = pt(2, h=999.0, l=1.0)  # older than the window -> ignored
    history[15] = pt(15, h=120.0, l=95.0)
    result = build_range_20d(history)
    assert (result.high, result.low) == (120.0, 95.0)


def test_range_requires_exactly_20_valid_sessions():
    assert build_range_20d(flat(RANGE_WINDOW - 1)) is None
    assert build_range_20d(flat(RANGE_WINDOW)) is not None


def test_range_is_none_when_any_window_row_lacks_high_low():
    history = flat(20)
    history[7] = HistoricalPricePoint(
        trading_date=history[7].trading_date, close=100.0, volume=1.0, turnover=1.0
    )
    assert build_range_20d(history) is None  # never falls back to close


def test_missing_high_low_outside_the_window_is_irrelevant():
    history = flat(25)
    history[0] = HistoricalPricePoint(
        trading_date=history[0].trading_date, close=100.0, volume=1.0, turnover=1.0
    )
    assert build_range_20d(history) is not None


def test_position_uses_close_of_t_minus_1():
    history = flat(20)
    history[0] = pt(0, h=130.0, l=90.0)
    history[-1] = pt(19, o=95, h=97, l=94, c=95.0)
    result = build_low_level_position(history)
    assert result.position == pytest.approx((95 - 90) / (130 - 90))
    assert (result.range_20d.high, result.range_20d.low) == (130.0, 90.0)


def test_position_flat_range_is_none_not_division_by_zero():
    history = [pt(i, o=100, h=100, l=100, c=100) for i in range(20)]
    result = build_low_level_position(history)
    assert result.position is None
    assert result.range_20d is not None  # the range exists, position doesn't


def test_position_insufficient_history():
    result = build_low_level_position(flat(5))
    assert result.position is None and result.range_20d is None


def test_today_limit_up_high_does_not_expand_prior_20d_range():
    """build_valid_history is what guarantees T is never in the window."""
    prior = flat(20)
    today = pt(20, o=100, h=999.0, l=1.0, c=110.0)
    cleaned = build_valid_history(prior + [today], target_date=today.trading_date)
    result = build_range_20d(cleaned)
    assert result.high == 102.0 and result.low == 98.0


def test_build_valid_history_drops_future_and_non_positive_close():
    target = START + dt.timedelta(days=10)
    rows = flat(10) + [pt(10), pt(11), pt(3, c=-1.0)]
    cleaned = build_valid_history(rows, target_date=target)
    assert cleaned is not None
    # 10 valid sessions (dates 0-9). The bad-close row on date 3 is dropped
    # BEFORE the duplicate check, so it is not mistaken for a duplicate date.
    assert len(cleaned) == 10
    assert all(p.trading_date < target for p in cleaned)


def test_build_valid_history_duplicate_date_invalidates_series():
    rows = flat(5) + [pt(2)]
    assert build_valid_history(rows, target_date=START + dt.timedelta(days=99)) is None


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, True),
        ({"h": 99.0}, False),  # high below close
        ({"l": 101.0}, False),  # low above close
        ({"o": 0.0}, False),
    ],
)
def test_has_valid_candle(kwargs, expected):
    assert has_valid_candle(pt(0, **kwargs)) is expected


def test_has_valid_candle_false_when_ohlc_missing():
    bare = HistoricalPricePoint(
        trading_date=START, close=100.0, volume=1.0, turnover=1.0
    )
    assert has_valid_candle(bare) is False
