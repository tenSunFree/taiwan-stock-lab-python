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


def relative_score_sample_size(series: pd.Series) -> int:
    """
    Count of values actually available to percentile_score's ranking
    population (i.e. len(valid) inside that function). Exposed
    separately so callers (app.domain.scoring) can attach "how many
    candidates was this percentile computed against" to each factor's
    Relative Score — needed for the small-sample degrade rule (see
    rank_within_pool below): a percentile computed against only 3
    candidates is not the same kind of statement as one computed
    against 40.
    """
    numeric = pd.to_numeric(series, errors="coerce")
    return int(numeric.notna().sum())


def rank_within_pool(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    """
    1-based rank of each value within the same non-missing population
    percentile_score would use (1 = best). Missing input values stay
    missing (NaN) in the output, same convention as percentile_score.

    Plain ordinal rank — no winsorization, no rescaling. Used as the
    small-sample fallback display for Relative Score: when the
    candidate pool is too small for a 0-100 percentile to mean much
    (see relative_score_sample_size's docstring), "ranked 1st of 3" is
    a more honest statement than "100/100".
    """
    numeric = pd.to_numeric(series, errors="coerce")
    ascending = not higher_is_better
    return numeric.rank(method="min", ascending=ascending)


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


# Momentum scoring thresholds, pulled out as named module-level
# constants (rather than living only inside bounded_momentum_score's
# own parameter defaults) so other layers — notably
# app.reports.signal_explainer, which needs to describe WHY a given
# return_5d landed in a particular band — can import the exact same
# numbers instead of hardcoding a second copy that could silently
# drift out of sync with this function's actual behavior.
MOMENTUM_IDEAL_LOW = 0.03
MOMENTUM_IDEAL_HIGH = 0.15
MOMENTUM_DANGEROUS_HIGH = 0.40


def bounded_momentum_score(
    returns: pd.Series,
    *,
    ideal_low: float = MOMENTUM_IDEAL_LOW,
    ideal_high: float = MOMENTUM_IDEAL_HIGH,
    dangerous_high: float = MOMENTUM_DANGEROUS_HIGH,
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
