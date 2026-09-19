"""
Feature input model used for scoring. Kept separate from DailyPrice
because features usually require additional historical price,
institutional, and revenue data combined together — not something a
single day's price record alone provides.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.institutional_flow_builder import InstitutionalDataCutoff
from app.domain.technical_signal_builder import LowFirstLimitUpSignal


@dataclass(frozen=True)
class StockFeatures:
    stock_id: str

    # liquidity
    turnover: float
    average_turnover_20d: float | None

    # volume/price structure
    volume_ratio_20d: float | None  # today's volume / 20-day average volume

    # momentum
    return_5d: float | None
    return_20d: float | None

    # institutional / ownership flow
    institutional_net_buy_ratio_5d: (
        float | None
    )  # trailing 5-session institutional net-buy shares / total trading volume shares

    # fundamentals
    revenue_yoy: float | None  # latest monthly revenue YoY growth

    # risk-quality input (derived from RiskPolicy's risk_flags count/severity;
    # simplified here to a 0~1 score, where 1 means best risk quality, no flags)
    risk_quality_raw: float | None

    # institutional / ownership flow — DISPLAY-ONLY signal, distinct
    # from institutional_net_buy_ratio_5d above: whether cumulative
    # institutional net-buy shares over the trailing 3 sessions is
    # strictly positive (see
    # app.domain.institutional_flow_builder.build_institutional_net_buy_positive).
    # None means "insufficient/missing data for the window", same
    # tri-state convention as the regulatory status fields below.
    # Defaults to None (and is listed after risk_quality_raw, not next
    # to institutional_net_buy_ratio_5d above) purely so existing
    # StockFeatures(...) call sites that don't set it explicitly keep
    # working — dataclasses require every field after the first
    # defaulted one to also have a default.
    institutional_net_buy_3d_positive: bool | None = None

    # technical / price-structure — ANOTHER DISPLAY-ONLY signal,
    # independent of both institutional_net_buy_3d_positive above and
    # the "momentum" scoring factor (return_5d/return_20d-derived):
    # whether today's close is both (a) near the low end of its own
    # trailing 20-session trading range, AND (b) just crossed above
    # its own 5-session moving average today (see
    # app.domain.technical_signal_builder.build_low_with_rising_signal
    # for the exact thresholds and crossover definition). None means
    # insufficient trailing history to compute the 20-session range at
    # all, same tri-state convention as every other optional signal in
    # this dataclass. Also listed after risk_quality_raw for the same
    # dataclass-field-ordering reason as institutional_net_buy_3d_positive
    # above, not because it's thematically related to risk quality.
    technical_low_with_rising_signal: bool | None = None

    # technical / price-structure — SIBLING of
    # technical_low_with_rising_signal above, a SEPARATE DISPLAY-ONLY
    # signal (see app.domain.technical_signal_builder's own module
    # docstring for why this is a standalone field rather than OR'd
    # into the existing one, per that module's own stated design
    # decision): whether today's close is (a) the legal limit-up
    # price, (b) near the low end of its own trailing 20-session
    # trading range, AND (c) NOT a continuation of a limit-up that
    # already happened on the immediately preceding trading session
    # (see
    # app.domain.technical_signal_builder.build_low_first_limit_up_signal).
    #
    # A LowFirstLimitUpSignal, NOT a bare bool | None — this carries
    # the FULL breakdown (is_low, range_position, is_close_limit_up,
    # previous_session_limit_up_estimated,
    # previous_session_check_provisional), not just the collapsed
    # tri-state result, so downstream layers (report explainer,
    # future backtest/debug tooling) can see WHY the overall result
    # came out the way it did without recomputing anything — see that
    # dataclass's own docstring. Read `.matched` for the equivalent of
    # the old plain tri-state bool (this is exactly what
    # app.reports.text_renderer's rendered "低檔首板" line does; the
    # rendered report text itself is unchanged by this richer type).
    #
    # (c) relies on estimate_previous_session_limit_up()'s PROVISIONAL,
    # single-day approximation (previous close as an approximate
    # reference price, same convention app.ingestion.finmind_mapper
    # already uses for today's own reference_price) — see that
    # function's own docstring for exactly which days it is wrong on
    # (ex-rights/ex-dividend/capital-reduction/newly-listed days). This
    # field must therefore NEVER be fed into RiskPolicy,
    # consecutive_limit_up_days, or any FACTOR_WEIGHTS scoring factor
    # — same restriction as technical_low_with_rising_signal above, on
    # top of the PROVISIONAL caveat that field doesn't carry.
    #
    # None (the OUTER None, distinct from any of the dataclass's own
    # None sub-fields) means this was never computed at all this run
    # — e.g. the FinMind historical-price fetch failed or returned no
    # usable rows for this stock (see
    # app.jobs.daily_ranking.build_stock_features's block 1) — same
    # convention as every other optional signal in this dataclass.
    technical_low_first_limit_up_signal: LowFirstLimitUpSignal | None = None

    # fundamentals — ANOTHER DISPLAY-ONLY signal, for the "基本面"
    # block: whether monthly revenue YoY growth has been sustained over
    # the trailing 3 known-available calendar months (see
    # app.domain.monthly_revenue_builder.build_revenue_growth_sustained_signal
    # for the exact rule). Independent of revenue_yoy above, which
    # feeds the "fundamental" SCORING FACTOR and only looks at the
    # single newest month. REVENUE ONLY — deliberately NOT redefined to
    # an OR-condition now that eps_growth_sustained (below) exists;
    # see that field's own docstring for where the OR-combination
    # actually lives (app.domain.eps_growth_builder.combine_fundamental_growth_signal,
    # called at the report-rendering layer, not baked into either
    # field here). None means insufficient trailing revenue history to
    # complete the 3-month window, same tri-state convention as
    # institutional_net_buy_3d_positive and
    # technical_low_with_rising_signal above.
    fundamental_growth_sustained: bool | None = None

    # fundamentals — SIBLING of fundamental_growth_sustained above, one
    # more DISPLAY-ONLY signal: whether quarterly EPS YoY growth has
    # been sustained over the trailing window (see
    # app.domain.eps_growth_builder.build_eps_growth_sustained_signal).
    # Sourced from TWSE/TPEx's t187ap06_{L,O}_ci general-industry
    # comprehensive-income-statement open data (see
    # app.ingestion.financial_statement_client), NOT FinMind — this
    # project's disclosure-date-attributed EPS source, distinct from
    # revenue's FinMind TaiwanStockMonthRevenue pipeline.
    #
    # Deliberately kept as its OWN field rather than merged into
    # fundamental_growth_sustained: the ORIGINAL spec this pair of
    # fields exists for is "營收或 EPS YoY >= 10%，且具持續性" (revenue
    # OR EPS), but per fundamental_growth_sustained's own docstring
    # above, that OR-combination belongs at the report-rendering
    # layer (via combine_fundamental_growth_signal's tri-state OR),
    # computed from these two independent fields on demand — never by
    # silently overwriting either field's own, narrower meaning. This
    # keeps both components independently inspectable (e.g. "was this
    # candidate flagged because of revenue, EPS, or both?") instead of
    # collapsing that distinction the moment either one becomes True.
    # None means insufficient trailing EPS history (fewer than the
    # required trailing quarters, or a missing previous-year-same-
    # quarter denominator) — same tri-state convention as every other
    # optional signal in this dataclass, never guessed at as False.
    eps_growth_sustained: bool | None = None

    risk_flags: tuple[str, ...] = field(default_factory=tuple)

    # RiskAssessment.missing_inputs (see app.domain.risk_policy), carried
    # through so downstream layers (scoring, report rendering) can explain
    # WHY risk_quality_raw is None instead of just knowing that it is.
    # risk_quality_raw=None alone only answers "can't be scored"; this
    # tuple answers "because these specific inputs (is_attention,
    # is_disposition, is_managed, consecutive_limit_up_days) are
    # unconfirmed" — without it, the report layer has no way to render an
    # accurate reason and previously fell back to a stale hardcoded
    # sentence that stopped matching reality once attention/disposition
    # were wired in.
    risk_missing_inputs: tuple[str, ...] = field(default_factory=tuple)

    # See app.domain.institutional_flow_builder.InstitutionalDataCutoff.
    # Carried through so the report layer (later steps 5/6) can state
    # an explicit data-cutoff date for every institutional-related
    # field — the "institutional" factor's Absolute Signal, its
    # candidate-pool relative score, the 3-day cumulative net-buy
    # check, and the 5-day net-buy ratio — instead of leaving a reader
    # to guess whether a figure like -6.3% already includes
    # target_date's own activity.
    #
    # None covers two cases: (1) no historical trading-day data exists
    # to anchor a T-1 date against, or (2) institutional-data
    # fetch/parse itself failed this run and returned no rows. Both
    # mean the same thing for reporting purposes ("cannot be
    # confirmed"), so they are intentionally not distinguished further
    # here.
    #
    # Intentionally last, defaulting to None: same dataclass convention
    # as institutional_net_buy_3d_positive etc. above — once one field
    # has a default, every field after it needs one too, so existing
    # StockFeatures(...) call sites (e.g. daily_ranking.py) keep
    # working unchanged. Wiring this into daily_ranking.py is step 7's
    # job; intentionally left untouched here.
    institutional_data_cutoff: InstitutionalDataCutoff | None = None
