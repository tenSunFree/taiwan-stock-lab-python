import datetime as dt

import pytest

from app.domain.chart_data import (
    build_stock_chart_data,
    moving_average_series,
    prior_average_series,
)
from app.domain.feature_builder import HistoricalPricePoint

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


def test_moving_average_none_until_window_filled():
    ma = moving_average_series([1, 2, 3, 4, 5, 6], 5)
    assert ma[:4] == [None] * 4
    assert ma[4] == 3.0 and ma[5] == 4.0


def test_prior_average_excludes_the_current_bar():
    avg = prior_average_series([10.0] * 20 + [1000.0], window=20)
    assert avg[19] is None
    assert avg[20] == 10.0


def test_slices_to_60_but_ma60_is_warm_on_the_first_bar():
    today = pt(119, o=100, h=111, l=100, c=110.0, v=9000.0)
    data = build_stock_chart_data(history=flat(119), today=today)
    assert len(data.bars) == len(data.ma5) == len(data.ma60) == 60
    assert data.bars[-1] == today
    assert data.ma60[0] is not None
    assert data.ma5[-1] == pytest.approx((100 * 4 + 110) / 5)


def test_keeps_at_most_120_sessions_for_ma_computation():
    # 200 sessions in: only the last 120 feed the MAs. Old closes of 50
    # must not leak into MA60 of the displayed window.
    old = [pt(i, o=50, h=52, l=48, c=50.0) for i in range(80)]
    recent = [pt(i, c=100.0) for i in range(80, 199)]
    data = build_stock_chart_data(history=old + recent, today=pt(199))
    assert data.ma60[0] == pytest.approx(100.0)


def test_today_is_excluded_from_the_prior_20d_range():
    """Real regression: today's extreme candle sits in the displayed bars
    but must not move range_20d_high/low."""
    today = pt(30, o=100, h=999.0, l=1.0, c=110.0)
    data = build_stock_chart_data(history=flat(30), today=today)
    assert data.bars[-1].high == 999.0
    assert data.range_20d.high == 102.0
    assert data.range_20d.low == 98.0


def test_range_matches_price_structure_single_source():
    from app.domain.price_structure import build_range_20d

    history = flat(30)
    history[22] = pt(22, h=130.0, l=90.0)
    data = build_stock_chart_data(history=history, today=pt(30))
    assert data.range_20d == build_range_20d(history[-20:])


def test_volume_average_denominator_matches_volume_ratio_20d():
    from app.domain.feature_builder import build_price_features

    history = [pt(i, v=1000.0 + i) for i in range(30)]
    today = pt(30, v=9000.0)
    data = build_stock_chart_data(history=history, today=today)
    features = build_price_features(
        target_date=today.trading_date,
        today_close=today.close,
        today_volume=today.volume,
        history=history,
    )
    assert today.volume / data.avg_volume_20[-1] == pytest.approx(
        features.volume_ratio_20d
    )


def test_short_history_draws_what_exists_without_padding():
    data = build_stock_chart_data(history=flat(7), today=pt(7))
    assert len(data.bars) == 8
    assert data.ma5[3] is None and data.ma5[4] is not None
    assert all(v is None for v in data.ma20)
    assert data.range_20d is None


def test_unavailable_when_today_candle_is_invalid_or_missing():
    bare_today = HistoricalPricePoint(
        trading_date=START + dt.timedelta(days=30),
        close=100.0,
        volume=1.0,
        turnover=1.0,
    )
    assert build_stock_chart_data(history=flat(30), today=bare_today) is None
    assert build_stock_chart_data(history=flat(30), today=pt(30, c=0.0)) is None


def test_unavailable_when_history_has_duplicate_dates():
    assert build_stock_chart_data(history=flat(30) + [pt(5)], today=pt(31)) is None


def test_unavailable_when_a_displayed_bar_lacks_ohlc():
    history = flat(30)
    history[20] = HistoricalPricePoint(
        trading_date=history[20].trading_date, close=100.0, volume=1.0, turnover=1.0
    )
    assert build_stock_chart_data(history=history, today=pt(30)) is None


def test_missing_ohlc_in_the_hidden_warmup_zone_is_tolerated():
    history = flat(110)
    history[3] = HistoricalPricePoint(  # far outside the last-60 window
        trading_date=history[3].trading_date, close=100.0, volume=1.0, turnover=1.0
    )
    assert build_stock_chart_data(history=history, today=pt(110)) is not None
