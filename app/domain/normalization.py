"""
Factor normalization.

Uses Winsorization -> Percentile Rank -> rescale to 0~100 to bring
factors of different units onto a common "0 = worst, 100 = best"
scale. A dedicated non-monotonic scoring function is provided for the
momentum factor, because "the higher the return, the higher the
score" is a dangerous assumption in the limit-up context — a stock
that has already rallied hard for several days typically carries
higher chase-in risk the next day, not lower.
"""

from __future__ import annotations

import pandas as pd


def percentile_score(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    valid = numeric.dropna()

    if valid.empty:
        return pd.Series(float("nan"), index=series.index)

    if valid.nunique() == 1:
        result = pd.Series(50.0, index=series.index)
        result[numeric.isna()] = float("nan")
        return result

    lower = valid.quantile(0.05)
    upper = valid.quantile(0.95)
    clipped = numeric.clip(lower=lower, upper=upper)

    score = clipped.rank(pct=True, method="average") * 100
    if not higher_is_better:
        score = 100 - score
    return score


def bounded_momentum_score(
    returns: pd.Series,
    *,
    ideal_low: float = 0.03,
    ideal_high: float = 0.15,
    dangerous_high: float = 0.40,
) -> pd.Series:
    """
    Non-monotonic momentum scoring:
        <= 0                        -> lower score (flat or declining)
        0 ~ ideal_low                -> score increases with the return
        ideal_low ~ ideal_high       -> full-score zone (moderate rally)
        ideal_high ~ dangerous_high  -> score decreases as the return grows (overheated)
        >= dangerous_high            -> fixed low score (extreme short-term rally, high chase risk)

    The specific thresholds (ideal_low/high, dangerous_high) are
    strategy-v1 initial assumptions and must be calibrated against
    historical return backtests, not theoretical values.
    """

    def score(value: float) -> float:
        if pd.isna(value):
            return float("nan")
        if value <= 0:
            return max(0.0, 50 + value * 100)
        if value < ideal_low:
            return 50 + value / ideal_low * 25
        if value <= ideal_high:
            return 100.0
        if value >= dangerous_high:
            return 20.0
        ratio = (value - ideal_high) / (dangerous_high - ideal_high)
        return 100 - ratio * 80

    return returns.apply(score)
