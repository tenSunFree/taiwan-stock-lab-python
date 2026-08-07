"""
Risk policy.

Deliberately separate from CandidateBuilder: the candidate pool has
already performed "hard exclusion" (non-common-stock, incomplete
data, turnover too low). This module handles the "soft" risk that
applies once a stock has already made it into the pool — attention
stock, one-price limit-up, too many consecutive limit-up days, etc.
These stocks may still make the Top 5; they simply score lower on
risk quality.

Threshold values here are only the initial assumptions for
strategy-v1 and must be tuned against historical and out-of-sample
performance — hence everything lives in RiskPolicyConfig instead of
being hardcoded into the logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RiskPolicyConfig:
    """
    strategy-v1 initial assumptions; must be revisited after backtesting.
    """

    maximum_consecutive_limit_up_days: int = 3
    excessive_return_5d: float = 0.35  # 5-day cumulative return above this is flagged as elevated risk
    minimum_data_completeness: float = 0.80

    # whether the following statuses are allowed into the candidate set
    # (True = allowed but flagged/penalized, False = hard exclusion)
    allow_attention_stock: bool = True
    allow_ky_stock: bool = True
    allow_one_price_limit_up: bool = True  # opened and locked at limit-up with no chance to buy in


@dataclass(frozen=True)
class RiskAssessment:
    stock_id: str
    is_excluded: bool
    exclusion_reason: str | None
    risk_flags: tuple[str, ...] = field(default_factory=tuple)


class RiskPolicy:
    def __init__(self, config: RiskPolicyConfig | None = None) -> None:
        self.config = config or RiskPolicyConfig()

    def assess(
        self,
        *,
        stock_id: str,
        is_attention: bool,
        is_disposition: bool,
        is_managed: bool,
        is_ky: bool,
        is_one_price_limit_up: bool,
        consecutive_limit_up_days: int,
        return_5d: float | None,
    ) -> RiskAssessment:
        # --- Hard exclusion (strategy-level, distinct from the
        # instrument-type exclusion done in CandidateBuilder) ---
        if is_disposition:
            return RiskAssessment(stock_id, True, "disposition stock, excluded by strategy policy")
        if is_managed:
            return RiskAssessment(stock_id, True, "full-cash-delivery / managed stock, excluded by strategy policy")
        if not self.config.allow_attention_stock and is_attention:
            return RiskAssessment(stock_id, True, "attention stock, excluded by strategy policy")
        if not self.config.allow_ky_stock and is_ky:
            return RiskAssessment(stock_id, True, "KY (foreign-registered) stock, excluded by strategy policy")

        # --- Soft risk flags (kept but recorded, used to penalize the
        # risk-quality factor at the scoring stage) ---
        flags: list[str] = []

        if is_attention:
            flags.append("ATTENTION_STOCK")
        if is_ky:
            flags.append("KY_STOCK")
        if is_one_price_limit_up:
            flags.append("ONE_PRICE_LIMIT_UP")
        if consecutive_limit_up_days > self.config.maximum_consecutive_limit_up_days:
            flags.append("EXCESSIVE_CONSECUTIVE_LIMIT_UP")
        if return_5d is not None and return_5d >= self.config.excessive_return_5d:
            flags.append("HIGH_FIVE_DAY_RETURN")

        return RiskAssessment(stock_id, False, None, tuple(flags))
