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


# --- today's candle from the official TWSE/TPEx DailyPrice --------------------


def _daily_price(**overrides):
    from decimal import Decimal

    from app.domain.models import DailyPrice

    fields = dict(
        trading_date=START + dt.timedelta(days=30),
        stock_id="1101",
        reference_price=Decimal("40.60"),
        open_price=Decimal("41.00"),
        high_price=Decimal("44.65"),
        low_price=Decimal("40.90"),
        close_price=Decimal("44.65"),
        volume=3_000_000,
        turnover=Decimal("130000000"),
    )
    fields.update(overrides)
    return DailyPrice(**fields)


def test_today_point_maps_the_official_candle():
    from app.domain.chart_data import today_point_from_daily_price

    point = today_point_from_daily_price(_daily_price())
    assert point == HistoricalPricePoint(
        trading_date=START + dt.timedelta(days=30),
        open=41.0,
        high=44.65,
        low=40.9,
        close=44.65,
        volume=3_000_000.0,
        turnover=130000000.0,
    )


@pytest.mark.parametrize(
    "missing",
    [
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "turnover",
    ],
)
def test_today_point_is_none_when_any_official_field_is_missing(missing):
    from app.domain.chart_data import today_point_from_daily_price

    assert today_point_from_daily_price(_daily_price(**{missing: None})) is None


def test_chart_is_capped_at_60_display_bars_for_every_series():
    data = build_stock_chart_data(history=flat(119), today=pt(119))
    assert len(data.bars) == 60
    for series in (data.ma5, data.ma20, data.ma60, data.avg_volume_20):
        assert len(series) == 60
    assert data.ma60[0] is not None  # warmed up by the 60 hidden sessions


@pytest.mark.parametrize(
    "history_count, last_ma60, first_ma60",
    [
        (20, False, False),
        (58, False, False),  # 59 sessions in total: MA60 not reachable
        (59, True, False),  # 60 sessions: only the LAST bar has MA60
        (60, True, False),
        (117, True, False),  # 118 sessions: first shown bar is index 58
        (118, True, True),  # 119 sessions: first shown bar is index 59 -> MA60
        (119, True, True),
        (250, True, True),  # capped at 120 sessions, still 60 bars
    ],
)
def test_ma60_availability_boundaries(history_count, last_ma60, first_ma60):
    """MA60 needs 60 closes, so bar index i has it iff i >= 59. The FIRST
    displayed bar is full[len(full)-60], hence 'first_ma60' flips at 119
    total sessions (118 history + today), not at 60."""
    data = build_stock_chart_data(history=flat(history_count), today=pt(history_count))
    assert len(data.bars) == min(60, history_count + 1)
    assert (data.ma60[-1] is not None) is last_ma60
    assert (data.ma60[0] is not None) is first_ma60
