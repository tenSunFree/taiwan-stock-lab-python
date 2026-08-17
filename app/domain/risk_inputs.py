"""
Heuristic/derived reconstructions of RiskPolicy inputs that no wired-in
data source directly provides as an official field.

is_ky_stock() is a naming-convention heuristic, not an official flag.
is_one_price_limit_up() is NOT a heuristic — it's computed directly
and reliably from today's own OHLC plus the already-resolved legal
limit-up price (see app.domain.limit_up), the same source of truth
CandidateBuilder itself relies on.

Deliberately NOT included here: any reconstruction of historical
consecutive-limit-up days from raw close prices. This system's own
rule (see app.domain.limit_up's module docstring) is that limit-up
determination must never be done via "previous close * 1.10" as a
formal rule — only as a rough pre-screening hint — precisely because
ex-rights days, ex-dividend days, and other reference-price special
cases make that arithmetic wrong on exactly the days it matters most.
Feeding such a self-reconstructed value into RiskPolicy as if it were
a real historical determination would violate that same principle one
layer up. Until a real per-session historical reference-price/
limit-up source is wired in, consecutive_limit_up_days stays None
(handled explicitly by RiskPolicy.assess() and build_risk_quality_raw()).
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.models import DailyPrice


def is_ky_stock(stock_name: str) -> bool:
    """
    Taiwan convention: KY (foreign-registered, 第一上市/上櫃) companies
    carry a "-KY" suffix in their listed name. This is a naming
    heuristic, not an authoritative regulatory status —
    TaiwanStockInfo has no dedicated is_ky field (see
    finmind_mapper.build_stock_master's module docstring).
    """
    normalized = stock_name.strip().upper()
    return "-KY" in normalized or normalized.startswith("F-KY")


def is_one_price_limit_up(*, price: DailyPrice, limit_up_price: Decimal | None) -> bool:
    """
    一字型漲停 (locked limit-up with zero intraday trading range):
    open == high == low == close == limit_up_price. Uses the
    already-resolved legal limit-up price (from
    app.domain.limit_up.LimitUpResult) rather than recalculating it
    here — same source of truth as CandidateBuilder's own
    determination.
    """
    if limit_up_price is None:
        return False

    values = (price.open_price, price.high_price, price.low_price, price.close_price)
    if any(value is None for value in values):
        return False

    return all(value == limit_up_price for value in values)
