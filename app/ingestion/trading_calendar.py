"""
Trading-day determination.

Must not rely on "Monday through Friday" alone — needs to account for
national holidays, typhoon closures, ad-hoc market closures, makeup
trading days, etc. The formal approach is to maintain a
trading_calendar table (see app/db/models.py), sourced from:
    - TWSE's annual "record date suspension periods" announcements
    - TWSE / TPEx official trading calendars
This file provides the interface and a skeleton that "queries an API
dynamically first, falls back to a local table when unavailable."
Phase 1 starts with an overridable local table + a holiday-API
placeholder; the real trading_calendar data source must be wired up
before going live — don't rely on weekday-only logic in production.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class TradingCalendarEntry:
    trading_date: dt.date
    is_trading_day: bool
    market_status: str  # e.g. NORMAL, TYPHOON_CLOSED, MAKEUP_TRADING
    source: str


class TradingCalendar:
    """
    Trading calendar lookup interface.

    Production version: backed by the DB's trading_calendar table
    (synced periodically from TWSE announcements). This minimal
    version treats Monday-Friday as potential trading days, but
    callers must still verify, once daily price data is fetched, that
    "the data date returned by the source equals target_date" — to
    avoid silently reusing stale data from the previous trading day.
    """

    def __init__(self, holiday_dates: set[dt.date] | None = None) -> None:
        # holiday_dates: national holidays / ad-hoc closures / typhoon
        # closure dates. Recommended to import these from TWSE's
        # annual announcement into the DB rather than hardcoding them
        # in source code.
        self._holiday_dates = holiday_dates or set()

    def is_trading_day(self, target_date: dt.date) -> bool:
        if target_date.weekday() >= 5:  # Saturday, Sunday
            return False
        if target_date in self._holiday_dates:
            return False
        return True

    def get_entry(self, target_date: dt.date) -> TradingCalendarEntry:
        is_trading = self.is_trading_day(target_date)
        status = "NORMAL" if is_trading else "NON_TRADING_DAY"
        return TradingCalendarEntry(
            trading_date=target_date,
            is_trading_day=is_trading,
            market_status=status,
            source="local_weekday_and_holiday_set",
        )


def verify_source_date_matches(
    *, target_date: dt.date, source_reported_date: dt.date | None, source_name: str
) -> None:
    """
    Verify that the data date reported by a source equals target_date.
    On mismatch, treat it as WAITING_FOR_DATA and do not proceed to
    scoring.
    """
    if source_reported_date != target_date:
        raise StaleDataError(
            f"{source_name} reported data date {source_reported_date}, "
            f"which does not match the target trading date {target_date}; "
            f"the data may not have finished updating yet"
        )


class StaleDataError(RuntimeError):
    """Raised when a source's reported data date lags behind the
    target trading date, indicating the data is not ready yet."""
