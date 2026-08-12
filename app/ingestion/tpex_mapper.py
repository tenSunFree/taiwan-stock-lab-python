"""
Map TPEx (Taipei Exchange) mainboard daily close quotes into
DailyPrice domain models.

Endpoint: https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes
Verified real response format (JSON array, one object per security):

    {
        "Date": "1150812",
        "SecuritiesCompanyCode": "006201",
        "CompanyName": "...",
        "Close": "45.21",
        "Change": "+1.41",
        "Open": "44.20",
        "High": "45.26",
        "Low": "44.20",
        "Average": "44.91",
        "TradingShares": "508551",
        "TransactionAmount": "22841328",
        "TransactionNumber": "930",
        "LatestBidPrice": "45.21",
        "LatesAskPrice": "45.23",
        "Capitals": "23446000",
        "NextReferencePrice": "45.21",
        "NextLimitUp": "49.73",
        "NextLimitDown": "40.69"
    }

IMPORTANT — data characteristics of this dataset:

    - This endpoint appears to only return the LATEST available
      trading day, no date query parameter (same limitation as TWSE's
      STOCK_DAY_ALL).
    - NextReferencePrice / NextLimitUp / NextLimitDown are for the
      *NEXT* trading session, not today. They must NEVER be used to
      determine whether TODAY's close was a limit-up — Close ==
      NextLimitUp is not a valid comparison. These three fields are
      intentionally NOT mapped into DailyPrice by this module; they
      remain available in the raw snapshot for a possible future
      "next session price limit" model, out of scope here.
    - reference_price is derived as Close - Change (today's own
      price-change field), matching the same provisional-fallback
      approach used in twse_mapper.py. This is NOT guaranteed correct
      on ex-rights/ex-dividend days, no-trade days, or other special
      adjustment events — TPEx's own rules describe multiple ways a
      reference price can be determined, and Close - Change is only
      one approximation, not an invariant. Do NOT assume
      NextReferencePrice always equals today's Close either — that
      happens to hold in ordinary cases but is not guaranteed by TPEx
      rules (e.g. ex-rights days).
    - security_type / instrument classification is intentionally NOT
      guessed here (e.g. codes like "006201" look ETF-like). This
      mapper only produces DailyPrice records; StockMaster
      classification (including hard-excluding non-common-stock
      instruments) is handled by FinMind's TaiwanStockInfo mapper —
      consistent with the TWSE pipeline's architecture.
    - Rows with all-zero OHLC values mean "no announced trade price
      that day" — treated as missing, same convention as the FinMind
      and TWSE mappers. TradingShares/TransactionAmount of 0 are real
      values, not treated as missing, but still fail
      data_quality_ok.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.models import DailyPrice


def roc_date_to_gregorian(roc_date_str: str) -> dt.date | None:
    """Convert TPEx's ROC-calendar date string (e.g. "1150812") to a
    Gregorian date. Same format as TWSE's STOCK_DAY_ALL."""
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
    *, target_date: dt.date, rows: list[dict[str, Any]]
) -> list[DailyPrice]:
    """
    Convert tpex_mainboard_daily_close_quotes rows into DailyPrice
    records. Rows whose own Date field doesn't match target_date are
    skipped — same safety check as twse_mapper, given this endpoint's
    known "latest day only" limitation.
    """
    result: list[DailyPrice] = []

    for row in rows:
        row_date = roc_date_to_gregorian(row.get("Date", ""))
        if row_date != target_date:
            continue

        stock_id = (row.get("SecuritiesCompanyCode") or "").strip()
        if not stock_id:
            continue

        open_price = _to_decimal(row.get("Open"), zero_is_missing=True)
        high_price = _to_decimal(row.get("High"), zero_is_missing=True)
        low_price = _to_decimal(row.get("Low"), zero_is_missing=True)
        close_price = _to_decimal(row.get("Close"), zero_is_missing=True)
        price_change = _to_decimal(row.get("Change"), zero_is_missing=False)
        volume = _to_int(row.get("TradingShares"), zero_is_missing=False)
        turnover = _to_decimal(row.get("TransactionAmount"), zero_is_missing=False)

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
                reference_price=reference_price,  # provisional, derived from Close - Change
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                volume=volume,
                turnover=turnover,
                limit_up_price=None,  # NextLimitUp is for the NEXT session — not usable here
                has_price_limit_today=True,  # provisional MVP assumption
                data_quality_ok=data_quality_ok,
            )
        )

    return result
