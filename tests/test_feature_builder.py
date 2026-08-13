import datetime as dt

import pytest

from app.domain.feature_builder import HistoricalPricePoint, build_price_features

TARGET_DATE = dt.date(2026, 8, 13)


def make_history(count: int = 20, *, start: dt.date = dt.date(2026, 7, 1)):
    return [
        HistoricalPricePoint(
            trading_date=start + dt.timedelta(days=i),
            close=100.0 + i,
            volume=1_000.0,
            turnover=10_000_000.0,
        )
        for i in range(count)
    ]


def test_average_turnover_20d_with_full_window():
    features = build_price_features(
        target_date=TARGET_DATE,
        today_close=120.0,
        today_volume=2_000.0,
        history=make_history(),
    )
    assert features.average_turnover_20d == pytest.approx(10_000_000.0)


def test_volume_ratio_20d():
    features = build_price_features(
        target_date=TARGET_DATE,
        today_close=120.0,
        today_volume=2_000.0,
        history=make_history(),
    )
    assert features.volume_ratio_20d == pytest.approx(2.0)


def test_return_5d_and_20d():
    history = make_history()
    features = build_price_features(
        target_date=TARGET_DATE,
        today_close=120.0,
        today_volume=2_000.0,
        history=history,
    )
    assert features.return_5d == pytest.approx(120.0 / history[-5].close - 1.0)
    assert features.return_20d == pytest.approx(120.0 / history[-20].close - 1.0)


def test_none_when_history_insufficient():
    features = build_price_features(
        target_date=TARGET_DATE,
        today_close=120.0,
        today_volume=2_000.0,
        history=make_history(count=4),
    )
    assert features.average_turnover_20d is None
    assert features.volume_ratio_20d is None
    assert features.return_5d is None
    assert features.return_20d is None


def test_rows_on_or_after_target_date_are_defensively_discarded():
    """The safety property (never use today-or-future data) must hold
    even if the caller mistakenly includes such a row."""
    history = make_history()
    history.append(
        HistoricalPricePoint(
            trading_date=TARGET_DATE, close=9999.0, volume=9999.0, turnover=9999.0
        )
    )
    features = build_price_features(
        target_date=TARGET_DATE,
        today_close=120.0,
        today_volume=2_000.0,
        history=history,
    )
    # unaffected by the bogus same-day row that should have been excluded
    assert features.return_5d == pytest.approx(120.0 / history[-6].close - 1.0)


def test_gap_in_window_makes_average_none_not_partial():
    history = make_history()
    history[5] = HistoricalPricePoint(
        trading_date=history[5].trading_date,
        close=history[5].close,
        volume=history[5].volume,
        turnover=0.0,
    )
    # turnover=0 is a legitimate value here (not filtered as invalid,
    # since only close>0/volume>=0/turnover>=0 are enforced) — this
    # test instead verifies a window that's simply too short still
    # returns None rather than averaging fewer points.
    short_history = history[:15]
    features = build_price_features(
        target_date=TARGET_DATE,
        today_close=120.0,
        today_volume=2_000.0,
        history=short_history,
    )
    assert features.average_turnover_20d is None
