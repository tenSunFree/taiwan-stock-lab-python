"""
Shared domain models.

Deliberately decoupled from what any particular data source looks
like: regardless of the raw fields returned by FinMind / TWSE / TPEx,
everything must be converted into the clean models defined here before
being passed down to limit-up detection, candidate building, scoring,
etc.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Market(StrEnum):
    TWSE = "TWSE"
    TPEX = "TPEX"


class SecurityType(StrEnum):
    COMMON_STOCK = "COMMON_STOCK"
    ETF = "ETF"
    ETN = "ETN"
    WARRANT = "WARRANT"
    DR = "DR"  # depositary receipt
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class StockMaster:
    stock_id: str
    stock_name: str
    market: Market
    security_type: SecurityType
    industry: str | None = None
    is_active: bool = True
    is_attention: bool | None = None  # under "attention" watch status; None = unknown
    # under disposition/restricted trading; None = unknown
    is_disposition: bool | None = None
    is_managed: bool | None = None  # full-cash-delivery / managed stock; None = unknown


@dataclass(frozen=True)
class DailyPrice:
    """
    Cleaned daily price record.

    reference_price / limit_up_price may be None — never assume any
    data source provides these fields. None means "this source did not
    provide it; try another source or fall back to calculation," and
    that decision is deferred to the caller (the limit_up module), not
    made here.
    """

    trading_date: date
    stock_id: str
    reference_price: Decimal | None
    open_price: Decimal | None
    high_price: Decimal | None
    low_price: Decimal | None
    close_price: Decimal | None
    volume: int | None
    turnover: Decimal | None
    limit_up_price: Decimal | None = None  # source-provided limit-up price, if any
    has_price_limit_today: bool = True  # False = special day with no daily price limit
    data_quality_ok: bool = True
