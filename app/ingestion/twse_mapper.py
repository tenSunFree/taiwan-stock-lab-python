"""
Map TWSE OpenAPI's STOCK_DAY_ALL dataset into DailyPrice domain models.

Endpoint: https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=open_data
Verified real response format (CSV):

    日期,證券代號,證券名稱,成交股數,成交金額,開盤價,最高價,最低價,收盤價,漲跌價差,成交筆數
    "1150807","2330","台積電","24414025","57947015347","2390.00","2395.00","2355.00","2370.00","5.0000","64670"

IMPORTANT — data characteristics of this dataset:

    - Only covers TWSE-listed (上市) instruments — TPEx (上櫃) needs a
      separate source (see app/ingestion/tpex_mapper.py, not yet
      implemented).
    - The 日期 field uses the ROC (Minguo) calendar, e.g. "1150807"
      means ROC year 115 = 2026-08-07 (ROC year + 1911 = Gregorian
      year).
    - This endpoint appears to only return the LATEST available
      trading day — it does not accept a date query parameter for
      arbitrary historical dates (per TWSE's own published Q&A on
      data.gov.tw). Do not assume it can be used to backfill an
      arbitrary target_date; that is a known limitation, not a bug in
      this mapper.
    - 漲跌價差 (price change) is the signed difference between the
      close price and the day's comparison base price. We derive
      reference_price = close_price - price_change from this single
      response — no second day's data is needed, unlike the
      FinMind-based provisional approximation (see finmind_mapper.py).
      This is still treated as provisional rather than fully
      authoritative until we confirm this comparison base is exactly
      the same "opening reference price" used for limit-up
      calculation in all cases (e.g. ex-rights days).
    - Rows with all-zero OHLC values (open/high/low/close = 0) mean
      "no announced trade price that day" — treated as missing, same
      convention as FinMind (see finmind_mapper.py's _to_decimal).
      成交股數/成交金額 (volume/turnover) can be legitimately 0 and
      are NOT treated as missing, only as failing data_quality_ok.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.models import DailyPrice


def parse_stock_day_all_csv(raw_text: str) -> list[dict[str, str]]:
    """
    Parse the raw open_data CSV text into a list of row dicts, keyed
    by the original Chinese column headers. This is the "raw snapshot"
    shape stored in RawSourcePayload — no business logic here, just
    structural CSV -> dict parsing.
    """
    reader = csv.DictReader(io.StringIO(raw_text))
    return [dict(row) for row in reader]


def roc_date_to_gregorian(roc_date_str: str) -> dt.date | None:
    """
    Convert TWSE's ROC-calendar date string (e.g. "1150807") to a
    Gregorian date. Format is a 3-digit ROC year followed by 2-digit
    month and 2-digit day (yyyMMdd), matching every response observed
    from this dataset (which only covers 2017 onward, i.e. ROC year
    106+, always 3 digits).
    """
    text = (roc_date_str or "").strip()
    if len(text) != 7 or not text.isdigit():
        return None
    roc_year = int(text[0:3])
    month = int(text[3:5])
    day = int(text[5:7])
    try:
        return dt.date(roc_year + 1911, month, day)
    except ValueError:
        return None


def _to_decimal(value: Any, *, zero_is_missing: bool = False) -> Decimal | None:
    """Same convention as finmind_mapper._to_decimal: zero_is_missing
    is chosen explicitly per field, not applied globally."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text == "":
        return None
    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite():
        return None
    if zero_is_missing and result == 0:
        return None
    return result


def _to_int(value: Any, *, zero_is_missing: bool = False) -> int | None:
    decimal_value = _to_decimal(value, zero_is_missing=False)
    if decimal_value is None:
        return None
    if decimal_value != decimal_value.to_integral_value():
        return None
    result = int(decimal_value)
    if zero_is_missing and result == 0:
        return None
    return result


def build_daily_prices(
    *, target_date: dt.date, rows: list[dict[str, str]]
) -> list[DailyPrice]:
    """
    Convert STOCK_DAY_ALL rows into DailyPrice records.

    Rows whose own 日期 field doesn't match target_date are skipped —
    given this endpoint's known "latest day only" limitation, this
    acts as a safety check rather than a filter you should rely on to
    select an arbitrary past date.
    """
    result: list[DailyPrice] = []

    for row in rows:
        row_date = roc_date_to_gregorian(row.get("日期", ""))
        if row_date != target_date:
            continue

        stock_id = (row.get("證券代號") or "").strip()
        if not stock_id:
            continue

        open_price = _to_decimal(row.get("開盤價"), zero_is_missing=True)
        high_price = _to_decimal(row.get("最高價"), zero_is_missing=True)
        low_price = _to_decimal(row.get("最低價"), zero_is_missing=True)
        close_price = _to_decimal(row.get("收盤價"), zero_is_missing=True)
        price_change = _to_decimal(row.get("漲跌價差"), zero_is_missing=False)
        volume = _to_int(row.get("成交股數"), zero_is_missing=False)
        turnover = _to_decimal(row.get("成交金額"), zero_is_missing=False)

        reference_price = None
        if close_price is not None and price_change is not None:
            reference_price = close_price - price_change

        data_quality_ok = (
            close_price is not None
            and high_price is not None
            and volume is not None
            and turnover is not None
            and volume > 0
            and turnover > 0
        )

        result.append(
            DailyPrice(
                trading_date=target_date,
                stock_id=stock_id,
                reference_price=reference_price,  # derived from price_change — see module docstring
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                volume=volume,
                turnover=turnover,
                limit_up_price=None,  # STOCK_DAY_ALL doesn't provide this directly either
                has_price_limit_today=True,  # still a provisional MVP assumption
                data_quality_ok=data_quality_ok,
            )
        )

    return result
