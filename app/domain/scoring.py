"""
Multi-factor scoring.

Normalization population: the day's candidate-pool cross-section
(i.e. this batch of up to 50 limit-up candidates compared against
each other), not the whole market — the ranking is meant to answer
"which of today's limit-up candidates is relatively higher quality,"
not "how does this stock rank against the entire market." As noted in
the requirements, scores can be unstable when the candidate count is
small; this is an accepted trade-off for v1. If out-of-sample results
show it's too unstable, consider switching to a full-market
cross-section or a rolling historical distribution instead.

Missing-data handling: missing factors are never filled with a score
of 50. Instead, the total score is renormalized over the available
weight, and data_completeness is recorded; stocks below the
completeness threshold are not eligible for ranking.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.domain.features import StockFeatures
from app.domain.normalization import (
    bounded_momentum_score,
    percentile_score,
    rank_within_pool,
    relative_score_sample_size,
)

FACTOR_WEIGHTS: dict[str, float] = {
    "liquidity": 0.25,
    "volume_price": 0.20,
    "momentum": 0.15,
    "institutional": 0.15,
    "fundamental": 0.15,
    "risk_quality": 0.10,
}

# Factors whose factor_scores[key] is a candidate-pool percentile (see
# the percentile_score calls in score_candidates below), and therefore
# have a meaningful "how many candidates was this ranked against"
# (relative_sample_size) and ordinal rank (relative_rank).
#
# "momentum" is deliberately excluded: it is scored by
# bounded_momentum_score, an ABSOLUTE non-monotonic rule against fixed
# thresholds (see that function's own docstring), not a percentile
# against the pool — "sample size" is not a meaningful concept for it.
RELATIVE_SCORE_FACTORS: tuple[str, ...] = (
    "liquidity",
    "volume_price",
    "institutional",
    "fundamental",
    "risk_quality",
)

# Maps each RELATIVE_SCORE_FACTORS entry to the raw column in
# _build_factor_frame's output that percentile_score actually consumes
# for it — used below to compute relative_sample_size/relative_rank
# from the exact same raw values and higher_is_better direction as the
# factor_scores[...] = percentile_score(...) calls, so the two can
# never silently drift apart.
_RAW_COLUMN_BY_RELATIVE_FACTOR: dict[str, str] = {
    "liquidity": "turnover",
    "volume_price": "volume_ratio_20d",
    "institutional": "institutional_net_buy_ratio_5d",
    "fundamental": "revenue_yoy",
    "risk_quality": "risk_quality_raw",
}


@dataclass(frozen=True)
class ScoredStock:
    stock_id: str
    total_score: float
    data_completeness: float
    factor_scores: dict[str, float | None]
    risk_flags: tuple[str, ...]

    # Carried straight through from StockFeatures.risk_missing_inputs
    # (see that dataclass's own docstring) so the report layer can
    # render an accurate "why is risk_quality missing" reason instead
    # of a generic/stale one. Defaults to () so any existing caller
    # that constructs a ScoredStock without this field (e.g. an older
    # test fixture) keeps working unchanged.
    risk_missing_inputs: tuple[str, ...] = ()

    # Relative Score metadata for the small-sample degrade rule (see
    # RELATIVE_SCORE_FACTORS above). Both dicts are keyed by the same
    # factor names as factor_scores, but ONLY for factors in
    # RELATIVE_SCORE_FACTORS — "momentum" is never a key in either.
    # This is purely additive report-layer metadata: it does NOT feed
    # weighted_sum/total_score/data_completeness above in any way, and
    # existing callers that construct a ScoredStock without these
    # fields (e.g. older test fixtures) keep working unchanged since
    # both default to {}.
    #
    # relative_sample_size[factor]: how many candidates in this run's
    #   pool had a non-missing raw value for that factor (i.e. the
    #   population percentile_score actually ranked against).
    relative_sample_size: dict[str, int] = field(default_factory=dict)
    # relative_rank[factor]: this stock's 1-based ordinal rank within
    #   that same population (1 = best), or None if this stock's own
    #   value for that factor was missing.
    relative_rank: dict[str, int | None] = field(default_factory=dict)


def _build_factor_frame(features: list[StockFeatures]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "stock_id": [f.stock_id for f in features],
            "turnover": [f.turnover for f in features],
            "volume_ratio_20d": [f.volume_ratio_20d for f in features],
            "return_5d": [f.return_5d for f in features],
            "institutional_net_buy_ratio_5d": [
                f.institutional_net_buy_ratio_5d for f in features
            ],
            "revenue_yoy": [f.revenue_yoy for f in features],
            "risk_quality_raw": [f.risk_quality_raw for f in features],
        }
    ).set_index("stock_id")


def score_candidates(features: list[StockFeatures]) -> list[ScoredStock]:
    if not features:
        return []

    df = _build_factor_frame(features)

    factor_scores = pd.DataFrame(index=df.index)
    factor_scores["liquidity"] = percentile_score(df["turnover"], higher_is_better=True)
    factor_scores["volume_price"] = percentile_score(
        df["volume_ratio_20d"], higher_is_better=True
    )
    factor_scores["momentum"] = bounded_momentum_score(df["return_5d"])
    factor_scores["institutional"] = percentile_score(
        df["institutional_net_buy_ratio_5d"], higher_is_better=True
    )
    factor_scores["fundamental"] = percentile_score(
        df["revenue_yoy"], higher_is_better=True
    )
    factor_scores["risk_quality"] = percentile_score(
        df["risk_quality_raw"], higher_is_better=True
    )

    flags_by_stock = {f.stock_id: f.risk_flags for f in features}
    risk_missing_inputs_by_stock = {f.stock_id: f.risk_missing_inputs for f in features}

    # Relative Score metadata (see RELATIVE_SCORE_FACTORS and
    # ScoredStock.relative_sample_size/relative_rank above) — computed
    # from the same raw columns/higher_is_better direction as the
    # percentile_score calls above, but kept in a separate frame so it
    # can never influence factor_scores/weighted_sum/total_score.
    relative_sample_sizes: dict[str, int] = {
        factor_name: relative_score_sample_size(df[column])
        for factor_name, column in _RAW_COLUMN_BY_RELATIVE_FACTOR.items()
    }
    relative_ranks = pd.DataFrame(
        {
            factor_name: rank_within_pool(df[column], higher_is_better=True)
            for factor_name, column in _RAW_COLUMN_BY_RELATIVE_FACTOR.items()
        },
        index=df.index,
    )

    results: list[ScoredStock] = []
    total_weight = sum(FACTOR_WEIGHTS.values())

    for stock_id, row in factor_scores.iterrows():
        weighted_sum = 0.0
        available_weight = 0.0
        row_scores: dict[str, float | None] = {}

        for factor_name, weight in FACTOR_WEIGHTS.items():
            value = row[factor_name]
            if pd.isna(value):
                row_scores[factor_name] = None
                continue
            row_scores[factor_name] = float(value)
            weighted_sum += float(value) * weight
            available_weight += weight

        if available_weight == 0:
            # every factor is missing; this stock cannot be scored, skip it
            continue

        total_score = round(weighted_sum / available_weight, 2)
        data_completeness = round(available_weight / total_weight, 4)

        stock_relative_rank: dict[str, int | None] = {}
        for factor_name in RELATIVE_SCORE_FACTORS:
            rank_value = relative_ranks.loc[stock_id, factor_name]
            stock_relative_rank[factor_name] = (
                None if pd.isna(rank_value) else int(rank_value)
            )

        results.append(
            ScoredStock(
                stock_id=stock_id,
                total_score=total_score,
                data_completeness=data_completeness,
                factor_scores=row_scores,
                risk_flags=flags_by_stock.get(stock_id, tuple()),
                risk_missing_inputs=risk_missing_inputs_by_stock.get(stock_id, tuple()),
                relative_sample_size=dict(relative_sample_sizes),
                relative_rank=stock_relative_rank,
            )
        )

    return results


def select_top_n(
    scored: list[ScoredStock],
    turnover_by_stock: dict[str, float],
    *,
    limit: int = 10,
    minimum_data_completeness: float = 0.80,
) -> list[ScoredStock]:
    """
    Select the top-ranked stocks from the scored pool.

    Eligibility gate: only stocks whose data_completeness meets
    minimum_data_completeness are considered — a stock scored on too
    little data is excluded rather than ranked on an unreliable score.

    Ranking key: (total_score, turnover) descending, both reversed
    together — turnover only acts as a tie-breaker when total_score is
    equal; it never overrides total_score on its own.

    limit: how many stocks to return at most (renamed from the old
    hardcoded top-5 cutoff — see select_top_n's callers in
    app/jobs/daily_ranking.py for the actual configured value).
    """
    if limit <= 0:
        raise ValueError("limit must be positive")

    eligible = [
        stock
        for stock in scored
        if stock.data_completeness >= minimum_data_completeness
    ]

    ranked = sorted(
        eligible,
        key=lambda stock: (
            stock.total_score,
            turnover_by_stock.get(stock.stock_id, 0.0),
        ),
        reverse=True,
    )

    return ranked[:limit]
