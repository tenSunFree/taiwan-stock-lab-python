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

from dataclasses import dataclass

import pandas as pd

from app.domain.features import StockFeatures
from app.domain.normalization import bounded_momentum_score, percentile_score

FACTOR_WEIGHTS: dict[str, float] = {
    "liquidity": 0.25,
    "volume_price": 0.20,
    "momentum": 0.15,
    "institutional": 0.15,
    "fundamental": 0.15,
    "risk_quality": 0.10,
}


@dataclass(frozen=True)
class ScoredStock:
    stock_id: str
    total_score: float
    data_completeness: float
    factor_scores: dict[str, float | None]
    risk_flags: tuple[str, ...]


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

        results.append(
            ScoredStock(
                stock_id=stock_id,
                total_score=total_score,
                data_completeness=data_completeness,
                factor_scores=row_scores,
                risk_flags=flags_by_stock.get(stock_id, tuple()),
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
