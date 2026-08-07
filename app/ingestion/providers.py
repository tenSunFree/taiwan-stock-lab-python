"""
MarketDataProvider protocol.

Never assume any source (FinMind or otherwise) directly provides
fields like limit_up_price / security_type / is_disposition.

Approach:
    - Split "fetch stock master data" and "fetch daily prices" into
      separate methods, each returning the clean domain models
      (app/domain/models.py).
    - Each provider implementation (FinMindProvider / TwseProvider,
      ...) is responsible for knowing whether that particular source
      has a given field; if not, it returns None rather than guessing
      or backfilling a value.
    - Upstream code (candidate_builder / limit_up) always goes through
      this interface and never touches a source's raw JSON structure
      directly, so switching or merging data sources never requires
      touching downstream logic.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol

from app.domain.models import DailyPrice, StockMaster


class MarketDataProvider(Protocol):
    async def fetch_stock_master(self) -> list[StockMaster]:
        """Fetch the currently active stock list (market, instrument
        type, risk status)."""
        ...

    async def fetch_daily_prices(self, trading_date: dt.date) -> list[DailyPrice]:
        """
        Fetch daily prices for the given trading date.

        Implementations must explicitly handle: does this source
        provide reference_price? Does it provide limit_up_price?
        Any field the source lacks must be returned as None — never
        guessed (e.g. never use the previous day's close as
        reference_price).
        """
        ...


class MultiSourceProvider:
    """
    Minimal multi-source merge skeleton: primary source wins, missing
    fields are backfilled from the fallback source. Real "cross-source
    validation / conflict detection" (DATA_CONFLICT) logic belongs in
    the validation layer — this only demonstrates field-level fallback
    to get data flowing end to end.
    """

    def __init__(
        self, primary: MarketDataProvider, fallback: MarketDataProvider | None = None
    ):
        self.primary = primary
        self.fallback = fallback

    async def fetch_daily_prices(self, trading_date: dt.date) -> list[DailyPrice]:
        primary_rows = {
            row.stock_id: row
            for row in await self.primary.fetch_daily_prices(trading_date)
        }

        if self.fallback is None:
            return list(primary_rows.values())

        fallback_rows = {
            row.stock_id: row
            for row in await self.fallback.fetch_daily_prices(trading_date)
        }

        merged: list[DailyPrice] = []
        for stock_id, row in primary_rows.items():
            fb = fallback_rows.get(stock_id)
            if fb is None:
                merged.append(row)
                continue

            merged.append(
                DailyPrice(
                    trading_date=row.trading_date,
                    stock_id=row.stock_id,
                    reference_price=row.reference_price or fb.reference_price,
                    open_price=row.open_price or fb.open_price,
                    high_price=row.high_price or fb.high_price,
                    low_price=row.low_price or fb.low_price,
                    close_price=row.close_price or fb.close_price,
                    volume=row.volume or fb.volume,
                    turnover=row.turnover or fb.turnover,
                    limit_up_price=row.limit_up_price or fb.limit_up_price,
                    has_price_limit_today=row.has_price_limit_today,
                    data_quality_ok=row.data_quality_ok and fb.data_quality_ok,
                )
            )
        return merged
