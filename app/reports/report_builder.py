"""
Adapter between the scoring layer and the report-rendering layer.

ScoredStock (app/domain/scoring.py) intentionally has no stock_name or
rank — it's a pure scoring result, keyed by stock_id, indifferent to
display concerns. The report renderer should never import from
app.domain.scoring directly; this module is the single place that
bridges the two, so display formatting changes never leak into the
scoring domain and scoring changes never leak into the report format.
"""

from __future__ import annotations

from app.domain.models import StockMaster
from app.domain.scoring import FACTOR_WEIGHTS, ScoredStock
from app.reports.text_renderer import FACTOR_DISPLAY_NAMES, ReportStockView


def build_report_stocks(
    *,
    top_five: list[ScoredStock],
    stock_master: dict[str, StockMaster],
) -> list[ReportStockView]:
    results: list[ReportStockView] = []

    for rank, scored in enumerate(top_five, start=1):
        stock = stock_master.get(scored.stock_id)
        stock_name = stock.stock_name if stock is not None else scored.stock_id

        available_factors = [
            (name, value)
            for name, value in scored.factor_scores.items()
            if value is not None and name in FACTOR_WEIGHTS
        ]
        available_factors.sort(key=lambda item: item[1], reverse=True)
        top_factor_names = tuple(
            FACTOR_DISPLAY_NAMES.get(name, name) for name, _ in available_factors[:2]
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
            )
        )

    return results
