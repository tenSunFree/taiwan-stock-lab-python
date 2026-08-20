"""
Adapter between the scoring layer and the report-rendering layer.

ScoredStock (app/domain/scoring.py) intentionally has no stock_name or
rank — it's a pure scoring result, keyed by stock_id, indifferent to
display concerns. The report renderer should never import from
app.domain.scoring directly; this module is the single place that
bridges the two, so display formatting changes never leak into the
scoring domain and scoring changes never leak into the report format.

As of text-v2, this module also merges in Candidate (see
app.domain.candidate_builder) so the report can show close price,
change percent, and derived risk-input pattern features (one-price
limit-up) without re-fetching anything — CandidateBuilder already
carries the raw DailyPrice + LimitUpResult that Top 5 stocks came
from.
"""

from __future__ import annotations

from app.domain.candidate_builder import Candidate
from app.domain.models import StockMaster
from app.domain.risk_inputs import is_one_price_limit_up
from app.domain.scoring import FACTOR_WEIGHTS, ScoredStock
from app.reports.text_renderer import FACTOR_DISPLAY_NAMES, ReportStockView


def build_report_stocks(
    *,
    top_five: list[ScoredStock],
    stock_master: dict[str, StockMaster],
    candidates: dict[str, Candidate],
) -> list[ReportStockView]:
    """
    candidates: stock_id -> Candidate, keyed from the same
        CandidateBuilder output the Top 5 was selected from. Every
        stock_id in top_five MUST have a matching entry here — Top 5
        is a subset of the candidate pool by construction, so a
        missing entry means a pipeline invariant broke upstream and
        this function refuses to silently drop the stock or produce a
        report that quietly looks normal but is missing data.
    """
    results: list[ReportStockView] = []

    for rank, scored in enumerate(top_five, start=1):
        stock = stock_master.get(scored.stock_id)
        stock_name = stock.stock_name if stock is not None else scored.stock_id

        candidate = candidates.get(scored.stock_id)
        if candidate is None:
            raise RuntimeError(
                "Report invariant violated: Top-5 stock_id="
                f"{scored.stock_id!r} does not exist in CandidateBuilder "
                "output. Top 5 must always be a subset of the candidate "
                "pool passed in via `candidates`."
            )

        available_factors = [
            (name, value)
            for name, value in scored.factor_scores.items()
            if value is not None and name in FACTOR_WEIGHTS
        ]
        available_factors.sort(key=lambda item: item[1], reverse=True)
        top_factor_names = tuple(
            FACTOR_DISPLAY_NAMES.get(name, name) for name, _ in available_factors[:2]
        )

        # Preserve FACTOR_WEIGHTS' declared ordering so the "缺失資料"
        # list renders in a stable, predictable order.
        missing_factor_names = tuple(
            name for name in FACTOR_WEIGHTS if scored.factor_scores.get(name) is None
        )

        close_price = candidate.price.close_price
        reference_price = candidate.price.reference_price

        change_percent: float | None = None
        if (
            close_price is not None
            and reference_price is not None
            and reference_price > 0
        ):
            change_percent = float((close_price / reference_price - 1) * 100)

        one_price_limit_up = is_one_price_limit_up(
            price=candidate.price,
            limit_up_price=candidate.limit_up.limit_up_price,
        )

        results.append(
            ReportStockView(
                rank=rank,
                stock_id=scored.stock_id,
                stock_name=stock_name,
                total_score=scored.total_score,
                data_completeness=scored.data_completeness,
                top_factor_names=top_factor_names,
                risk_flags=scored.risk_flags,
                close_price=close_price,
                change_percent=change_percent,
                missing_factor_names=missing_factor_names,
                is_one_price_limit_up=one_price_limit_up,
            )
        )

    return results
