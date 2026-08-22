"""
P/E ratio eligibility filter.

This is an ELIGIBILITY RULE, not a scoring factor: a candidate either
satisfies 0 < P/E <= maximum_pe_ratio or it doesn't. There is no
partial credit, and P/E is deliberately kept out of
app.domain.scoring's FACTOR_WEIGHTS — a stock that fails this filter
never reaches FinMind enrichment or multi-factor scoring at all, the
same way RiskPolicy's hard exclusions work.

Applying this filter BEFORE FinMind enrichment (see
app.jobs.daily_ranking's pipeline ordering) is deliberate: it avoids
spending FinMind API calls (historical price / institutional flow /
monthly revenue) on candidates that are already disqualified.
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.candidate_builder import Candidate
from app.domain.models import StockValuation


def filter_candidates_by_pe(
    candidates: list[Candidate],
    valuations_by_stock: dict[str, StockValuation],
    *,
    maximum_pe_ratio: Decimal,
) -> list[Candidate]:
    """
    Keep only candidates satisfying 0 < P/E <= maximum_pe_ratio.

    Fail-closed policy — every one of these is treated as "cannot
    verify the P/E threshold is met" and excludes the candidate,
    never as "assume it passes":
        - no valuation record for this stock_id at all
        - valuation.pe_ratio is None (source did not publish a P/E,
          typically because trailing EPS is zero or negative)
        - pe_ratio <= 0 (shouldn't normally occur once the mapper is
          faithfully parsing the source, but defend against it anyway
          rather than trust upstream data blindly)
        - pe_ratio > maximum_pe_ratio

    Candidate order is preserved (this is a filter, not a sort).

    valuations_by_stock is expected to already be scoped to a single
    market snapshot from app.ingestion.valuation_mapper — each
    StockValuation's trading_date reflects the actual date its P/E
    figure is from (the newest available as of target_date, which may
    be a day or more earlier — see valuation_mapper's module
    docstring for why). This function itself does not check
    trading_date/staleness any further; that decision already happened
    in the mapper.
    """
    if maximum_pe_ratio <= 0:
        raise ValueError("maximum_pe_ratio must be positive")

    eligible: list[Candidate] = []

    for candidate in candidates:
        valuation = valuations_by_stock.get(candidate.stock.stock_id)

        if valuation is None:
            continue

        pe_ratio = valuation.pe_ratio

        if pe_ratio is None:
            continue

        if pe_ratio <= 0:
            continue

        if pe_ratio > maximum_pe_ratio:
            continue

        eligible.append(candidate)

    return eligible
