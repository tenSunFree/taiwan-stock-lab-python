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
carries the raw DailyPrice + LimitUpResult that the ranked stocks came
from.

As of text-v5, also merges in RegulatoryRiskStatus (see
app.domain.models) so the report can show WHY a stock is flagged
ATTENTION_STOCK/DISPOSITION_STOCK (short reason text) and, for
disposition, the active period — not just the bare flag name. Unlike
stock_master/candidates above, a missing regulatory_by_stock entry is
NOT a pipeline invariant violation: most ranked stocks simply aren't
under attention/disposition at all, so a lookup miss here just means
"no detail to show," the same everyday case as a stock having no
risk_flags.
"""

from __future__ import annotations

from app.domain.candidate_builder import Candidate
from app.domain.models import RegulatoryRiskStatus, StockMaster
from app.domain.risk_inputs import is_one_price_limit_up
from app.domain.scoring import FACTOR_WEIGHTS, ScoredStock
from app.reports.text_renderer import FACTOR_DISPLAY_NAMES, ReportStockView


def build_report_stocks(
    *,
    ranked_stocks: list[ScoredStock],
    stock_master: dict[str, StockMaster],
    candidates: dict[str, Candidate],
    regulatory_by_stock: dict[str, RegulatoryRiskStatus] | None = None,
) -> list[ReportStockView]:
    """
    candidates: stock_id -> Candidate, keyed from the same
        CandidateBuilder output ranked_stocks was selected from. Every
        stock_id in ranked_stocks MUST have a matching entry here —
        ranked_stocks is a subset of the candidate pool by
        construction, so a missing entry means a pipeline invariant
        broke upstream and this function refuses to silently drop the
        stock or produce a report that quietly looks normal but is
        missing data.
    regulatory_by_stock: stock_id -> RegulatoryRiskStatus, the same
        merged dict app.jobs.daily_ranking builds in Step 1d/Step 5.
        Optional and defaults to {} — unlike `candidates` above, an
        absent entry here is the ordinary case (most stocks aren't
        flagged), not an invariant violation.
    """
    regulatory_by_stock = regulatory_by_stock or {}
    results: list[ReportStockView] = []

    for rank, scored in enumerate(ranked_stocks, start=1):
        stock = stock_master.get(scored.stock_id)
        stock_name = stock.stock_name if stock is not None else scored.stock_id

        candidate = candidates.get(scored.stock_id)
        if candidate is None:
            raise RuntimeError(
                "Report invariant violated: ranked stock_id="
                f"{scored.stock_id!r} does not exist in CandidateBuilder "
                "output. Ranked stocks must always be a subset of the "
                "candidate pool passed in via `candidates`."
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

        regulatory = regulatory_by_stock.get(scored.stock_id)

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
                attention_reason=(
                    regulatory.attention_reason if regulatory is not None else None
                ),
                disposition_start_date=(
                    regulatory.disposition_start_date
                    if regulatory is not None
                    else None
                ),
                disposition_end_date=(
                    regulatory.disposition_end_date if regulatory is not None else None
                ),
                disposition_reason=(
                    regulatory.disposition_reason if regulatory is not None else None
                ),
            )
        )

    return results
