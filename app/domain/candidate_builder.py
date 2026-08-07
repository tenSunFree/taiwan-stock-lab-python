"""
Candidate pool construction.

Pipeline: all TWSE/TPEx listed instruments -> filter to common stocks
-> exclude records with missing required fields -> close price equals
the legal limit-up price -> apply the minimum turnover threshold ->
sort by turnover descending -> take at most 50.

Hard exclusions (ETF/warrant/suspended/incomplete data) happen at this
layer; soft risk flags (attention stock, one-price limit-up, etc.) are
deliberately NOT applied here — soft risk means "keep it but penalize
the score," and that responsibility belongs to the next stage's
RiskPolicy, not the candidate pool.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.limit_up import LimitUpResult, evaluate_limit_up
from app.domain.models import DailyPrice, SecurityType, StockMaster


@dataclass(frozen=True)
class Candidate:
    stock: StockMaster
    price: DailyPrice
    limit_up: LimitUpResult


class CandidateBuilder:
    def __init__(self, minimum_turnover: Decimal, maximum_candidates: int = 50) -> None:
        self.minimum_turnover = minimum_turnover
        self.maximum_candidates = maximum_candidates

    def build(
        self, stocks: list[StockMaster], prices: list[DailyPrice]
    ) -> list[Candidate]:
        stock_map = {stock.stock_id: stock for stock in stocks}
        candidates: list[Candidate] = []

        for price in prices:
            stock = stock_map.get(price.stock_id)
            if stock is None or not stock.is_active:
                continue

            if stock.security_type != SecurityType.COMMON_STOCK:
                continue  # hard exclusion: ETF / ETN / warrant / non-target instruments

            if not price.data_quality_ok:
                continue  # hard exclusion: data quality check failed

            if price.turnover is None or price.turnover < self.minimum_turnover:
                continue  # hard exclusion: turnover below the minimum tradable threshold

            result = evaluate_limit_up(
                security_type=stock.security_type.value,
                close_price=price.close_price,
                high_price=price.high_price,
                source_limit_up_price=price.limit_up_price,
                reference_price=price.reference_price,
                has_price_limit_today=price.has_price_limit_today,
                data_quality_ok=price.data_quality_ok,
            )

            if not result.is_close_limit_up:
                continue

            candidates.append(Candidate(stock=stock, price=price, limit_up=result))

        candidates.sort(
            key=lambda c: (c.price.turnover or Decimal("0"), c.price.volume or 0),
            reverse=True,
        )

        return candidates[: self.maximum_candidates]
