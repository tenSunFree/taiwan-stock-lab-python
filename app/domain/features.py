"""
Feature input model used for scoring. Kept separate from DailyPrice
because features usually require additional historical price,
institutional, and revenue data combined together — not something a
single day's price record alone provides.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
