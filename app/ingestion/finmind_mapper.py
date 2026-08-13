"""
Map FinMind Taiwan market datasets into clean domain models.

This module is intentionally the FinMind-specific boundary of the
application. Domain layers must not depend directly on FinMind field
names such as ``Trading_Volume`` or ``Trading_money``.

Supported source datasets:

    TaiwanStockPrice
        Daily OHLC, volume, turnover amount, etc.

    TaiwanStockInfo
        Stock name, market (TWSE / TPEx via the `type` field),
        industry category, etc. NOTE: TaiwanStockInfo.type is the
        MARKET (twse/tpex), not an instrument-type classification —
        do not confuse it with SecurityType.

IMPORTANT — current data limitations
------------------------------------

reference_price
    TaiwanStockPrice does not provide the official opening reference
    price (開盤競價基準). For the current pipeline-integration phase
    only, this mapper approximates it using the previous trading
    day's close. This approximation is NOT authoritative — it is
    wrong on ex-rights/ex-dividend days, capital-reduction events,
    newly listed securities, and other adjustment days. Any limit-up
    result produced from this fallback must be treated as
    PROVISIONAL until an official TWSE/TPEx reference-price source is
    connected (see TwseClient/TpexClient TODOs in market_data_client.py).

limit_up_price
    Never provided by FinMind — always None from this mapper.
    app.domain.limit_up will calculate a provisional value from the
    approximated reference_price above.

security_type
    TaiwanStockInfo gives useful metadata (industry_category,
    stock_name) but no dedicated instrument-type field. This mapper
    classifies known categories (ETF/ETN/DR) explicitly from
    industry_category/stock_name text matches, and otherwise falls
    back to a 4-digit-numeric-code heuristic for COMMON_STOCK. Any
    stock_id it cannot confidently classify is SecurityType.UNKNOWN —
    never optimistically COMMON_STOCK — because CandidateBuilder only
    admits COMMON_STOCK, and a false-positive classification would
    silently let a non-target instrument into the candidate pool.

has_price_limit_today
    FinMind's basic daily-price dataset does not flag exceptional
    no-price-limit days (e.g. first days of a new listing). This
    mapper assumes True for every row — a provisional MVP assumption,
    not a verified fact.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.feature_builder import HistoricalPricePoint

from app.domain.models import DailyPrice, Market, SecurityType, StockMaster


def _to_decimal(value: Any, *, zero_is_missing: bool = False) -> Decimal | None:
    """
    Convert a JSON scalar to Decimal. FinMind responses may contain
    JSON numbers or numeric strings.

    zero_is_missing must be chosen explicitly per field rather than
    globally treating every zero as missing — a zero price usually
    means "no announced price that day" (per FinMind's own docs), but
    zero is a legitimate value for other fields.
    """
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite():
        return None
    if zero_is_missing and result == 0:
        return None
    return result


def _to_int(value: Any, *, zero_is_missing: bool = False) -> int | None:
    if value is None or value == "":
        return None
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not decimal_value.is_finite():
        return None
    if decimal_value != decimal_value.to_integral_value():
        return None
    result = int(decimal_value)
    if zero_is_missing and result == 0:
        return None
    return result


def _market_from_finmind_type(value: Any) -> Market | None:
    """TaiwanStockInfo.type -> domain Market. Currently 'twse'/'tpex'."""
    normalized = str(value or "").strip().lower()
    if normalized == "twse":
        return Market.TWSE
    if normalized == "tpex":
        return Market.TPEX
    return None


def _security_type_from_stock_info(
    *, stock_id: str, industry_category: Any, stock_name: Any
) -> SecurityType:
    """
    Fail-closed classification: prefer UNKNOWN over a false
    COMMON_STOCK positive. See module docstring.
    """
    category = str(industry_category or "").strip().upper()
    name = str(stock_name or "").strip().upper()

    if "ETF" in category or "ETF" in name:
        return SecurityType.ETF
    if "ETN" in category or "ETN" in name:
        return SecurityType.ETN
    if "權證" in category or "權證" in name:
        return SecurityType.WARRANT
    if (
        "存託憑證" in category
        or "存託憑證" in name
        or category == "DR"
        or "TDR" in category
        or "TDR" in name
    ):
        return SecurityType.DR

    # Only after known fund/structured-product categories are ruled
    # out does a 4-digit numeric code become a usable common-stock
    # signal. Still heuristic — kept local to this FinMind adapter.
    if len(stock_id) == 4 and stock_id.isdigit():
        return SecurityType.COMMON_STOCK

    return SecurityType.UNKNOWN


def build_stock_master(stock_info_rows: list[dict[str, Any]]) -> dict[str, StockMaster]:
    """Convert TaiwanStockInfo rows into StockMaster records. Rows
    outside TWSE/TPEx are ignored (current strategy targets listed and
    OTC Taiwan securities only, not emerging/興櫃)."""
    result: dict[str, StockMaster] = {}
    for row in stock_info_rows:
        stock_id = str(row.get("stock_id") or "").strip()
        if not stock_id:
            continue

        market = _market_from_finmind_type(row.get("type"))
        if market is None:
            continue

        stock_name = str(row.get("stock_name") or stock_id).strip()
        industry = str(row.get("industry_category") or "").strip() or None

        result[stock_id] = StockMaster(
            stock_id=stock_id,
            stock_name=stock_name,
            market=market,
            security_type=_security_type_from_stock_info(
                stock_id=stock_id, industry_category=industry, stock_name=stock_name
            ),
            industry=industry,
            is_active=True,
            # TaiwanStockInfo has no risk-status fields; defaults stay
            # False until a dedicated risk-status source is wired in.
            is_attention=False,
            is_disposition=False,
            is_managed=False,
        )
    return result


def _previous_close_map(previous_day_rows: list[dict[str, Any]]) -> dict[str, Decimal]:
    """stock_id -> previous close. Used only as a PROVISIONAL
    reference_price approximation — see module docstring."""
    result: dict[str, Decimal] = {}
    for row in previous_day_rows:
        stock_id = str(row.get("stock_id") or "").strip()
        if not stock_id:
            continue
        close = _to_decimal(row.get("close"), zero_is_missing=True)
        if close is not None:
            result[stock_id] = close
    return result


def build_daily_prices(
    *,
    target_date: dt.date,
    today_rows: list[dict[str, Any]],
    previous_day_rows: list[dict[str, Any]],
) -> list[DailyPrice]:
    """
    Convert TaiwanStockPrice rows into DailyPrice records.
    previous_day_rows must represent the immediately preceding TRADING
    DAY, not simply target_date - 1 day.
    """
    previous_close_by_stock = _previous_close_map(previous_day_rows)
    expected_date = target_date.isoformat()
    result: list[DailyPrice] = []

    for row in today_rows:
        stock_id = str(row.get("stock_id") or "").strip()
        row_date = str(row.get("date") or "").strip()
        if not stock_id or row_date != expected_date:
            continue

        open_price = _to_decimal(row.get("open"), zero_is_missing=True)
        high_price = _to_decimal(row.get("max"), zero_is_missing=True)
        low_price = _to_decimal(row.get("min"), zero_is_missing=True)
        close_price = _to_decimal(row.get("close"), zero_is_missing=True)
        volume = _to_int(row.get("Trading_Volume"), zero_is_missing=False)
        turnover = _to_decimal(row.get("Trading_money"), zero_is_missing=False)

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
                reference_price=previous_close_by_stock.get(
                    stock_id
                ),  # PROVISIONAL, see docstring
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                volume=volume,
                turnover=turnover,
                limit_up_price=None,  # TaiwanStockPrice never provides this
                has_price_limit_today=True,  # PROVISIONAL MVP assumption
                data_quality_ok=data_quality_ok,
            )
        )
    return result


def build_historical_price_points(
    rows: list[dict[str, Any]],
) -> list[HistoricalPricePoint]:
    """
    Convert FinMind TaiwanStockPrice per-stock history rows (fetched
    via FinMindClient.fetch_stock_price_history, which includes
    data_id) into provider-independent HistoricalPricePoint records
    for app.domain.feature_builder.

    Rows missing close/volume/turnover, or with an unparsable date,
    are dropped entirely — a row that's only partially usable is not
    usable at all for this purpose (unlike build_daily_prices(),
    which keeps partial rows and lets data_quality_ok reflect that).
    """
    result: list[HistoricalPricePoint] = []
    for row in rows:
        date_text = str(row.get("date") or "").strip()
        try:
            trading_date = dt.date.fromisoformat(date_text)
        except ValueError:
            continue

        close = _to_decimal(row.get("close"), zero_is_missing=True)
        volume = _to_int(row.get("Trading_Volume"), zero_is_missing=False)
        turnover = _to_decimal(row.get("Trading_money"), zero_is_missing=False)

        if close is None or volume is None or turnover is None:
            continue

        result.append(
            HistoricalPricePoint(
                trading_date=trading_date,
                close=float(close),
                volume=float(volume),
                turnover=float(turnover),
            )
        )

    result.sort(key=lambda point: point.trading_date)
    return result
