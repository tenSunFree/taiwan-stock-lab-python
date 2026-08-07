import datetime as dt

import pytest

from app.ingestion.trading_calendar import (
    StaleDataError,
    TradingCalendar,
    verify_source_date_matches,
)


def test_weekend_is_not_trading_day():
    cal = TradingCalendar()
    saturday = dt.date(2026, 8, 8)
    assert cal.is_trading_day(saturday) is False


def test_weekday_is_trading_day_by_default():
    cal = TradingCalendar()
    friday = dt.date(2026, 8, 7)
    assert cal.is_trading_day(friday) is True


def test_holiday_override():
    holiday = dt.date(2026, 8, 7)
    cal = TradingCalendar(holiday_dates={holiday})
    assert cal.is_trading_day(holiday) is False


def test_stale_data_raises():
    with pytest.raises(StaleDataError):
        verify_source_date_matches(
            target_date=dt.date(2026, 8, 7),
            source_reported_date=dt.date(2026, 8, 6),
            source_name="finmind",
        )


def test_matching_date_does_not_raise():
    verify_source_date_matches(
        target_date=dt.date(2026, 8, 7),
        source_reported_date=dt.date(2026, 8, 7),
        source_name="finmind",
    )
