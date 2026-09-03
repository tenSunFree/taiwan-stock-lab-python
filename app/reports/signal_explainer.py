"""
Explainable signal rules.

Turns each of the six scoring factors' RAW input value + its already-
computed 0-100 normalized score into 1-3 fixed-template sentences.
No LLM, no post-hoc rationalization — every sentence comes from an
if/elif against a number already present in this run's StockFeatures.

CRITICAL DISTINCTION this module enforces (do not blur it):

    reasons        -> facts that ACTUALLY drove factor_scores[key]
    supplemental   -> real, verifiable facts that do NOT affect
                      factor_scores[key], shown only as extra context

Per app.domain.scoring.FACTOR_WEIGHTS / _build_factor_frame, what
actually feeds each factor is:

    liquidity      <- turnover (today's absolute turnover amount)
                      NOT turnover / average_turnover_20d
    volume_price   <- volume_ratio_20d
    momentum       <- return_5d (via bounded_momentum_score, an
                      ABSOLUTE non-monotonic rule, NOT a percentile)
    institutional  <- institutional_net_buy_ratio_5d
    fundamental    <- revenue_yoy (the single newest month only;
                      fundamental_growth_sustained / eps_growth_sustained
                      are SEPARATE display-only signals, rendered in
                      their own "基本面" block — never folded in here)
    risk_quality   <- risk_quality_raw, itself built from
                      RiskAssessment.risk_flags weighted by
                      app.domain.risk_policy.RISK_FLAG_PENALTIES

Every factor except momentum is scored via
app.domain.normalization.percentile_score against TODAY'S CANDIDATE
POOL (see scoring.py's own module docstring: normalization population
is the day's candidate cross-section, not the whole market). Whether
a given factor's score is "候選池相對" (pool-relative percentile) or
"絕對規則" (an absolute rule) is rendered by text_renderer directly
next to the score itself (see _render_factor_block) — this module
does NOT repeat that qualifier as a separate reason bullet, to keep
each factor block short enough for the LINE message length budget.

🔴 vs ⚪ is decided entirely by text_renderer._signal_emoji (None ->
⚪, low score -> 🔴) and is NOT touched here — this module only adds
the "why" underneath that existing, already-correct distinction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.normalization import MOMENTUM_IDEAL_HIGH, MOMENTUM_IDEAL_LOW
from app.domain.risk_policy import RISK_FLAG_PENALTIES

_RISK_FLAG_LABELS: dict[str, str] = {
    "ATTENTION_STOCK": "今日注意股",
    "DISPOSITION_STOCK": "目前處置股",
    "MANAGED_STOCK": "全額交割／變更交易方法",
    "KY_STOCK": "KY 股",
    "ONE_PRICE_LIMIT_UP": "一字漲停",
    "EXCESSIVE_CONSECUTIVE_LIMIT_UP": "連續漲停天數偏高",
    "HIGH_FIVE_DAY_RETURN": "近 5 日漲幅過高",
}

_RISK_INPUT_LABELS: dict[str, str] = {
    "is_attention": "今日注意股公告狀態",
    "is_disposition": "目前處置生效狀態",
    "is_managed": "全額交割／變更交易方法狀態",
    "consecutive_limit_up_days": "連續漲停天數",
}
_CONFIRMED_INPUT_LABELS: dict[str, str] = {
    "is_attention": "今日注意股公告狀態已確認",
    "is_disposition": "目前處置生效狀態已確認",
    "is_managed": "全額交割／變更交易方法狀態已確認",
    "consecutive_limit_up_days": "連續漲停天數已確認",
}


@dataclass(frozen=True)
class FactorExplanation:
    # Reasons that actually explain factor_scores[key] (including numeric values)
    reasons: tuple[str, ...] = field(default_factory=tuple)
    # Supplementary facts that are valuable but did NOT affect this factor's score
    supplemental: tuple[str, ...] = field(default_factory=tuple)
    # Used only by risk quality: confirmed vs pending/missing inputs
    confirmed: tuple[str, ...] = field(default_factory=tuple)
    missing: tuple[str, ...] = field(default_factory=tuple)
    # data status: complete / partially missing / insufficient data
    data_status: str = "完整"


def _pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def _fmt_amount(value: float) -> str:
    """Turnover is in New Taiwan Dollars (TWD). Format into human-readable
    "ten-thousands" (萬元) or "hundreds of millions" (億元) units."""
    if value >= 1e8:
        return f"{value / 1e8:.2f} 億元"
    return f"{value / 1e4:.0f} 萬元"


# --- Liquidity: percentile_score(turnover) ---------------------------------


def explain_liquidity(
    *,
    turnover: float | None,
    average_turnover_20d: float | None,
    score: float | None,
) -> FactorExplanation:
    if turnover is None or score is None:
        return FactorExplanation(data_status="資料不足")

    reasons = [f"今日成交金額 {_fmt_amount(turnover)}"]

    supplemental: list[str] = []
    data_status = "完整"
    if average_turnover_20d is not None and average_turnover_20d > 0:
        ratio = turnover / average_turnover_20d
        supplemental.append(
            f"近 20 日平均成交金額 {_fmt_amount(average_turnover_20d)}，"
            f"今日約為其 {ratio:.2f} 倍（僅供參考，未參與本因子評分）"
        )
    else:
        data_status = "部分缺失"  # 20-day average turnover missing; only today's turnover available as evidence

    return FactorExplanation(
        reasons=tuple(reasons),
        supplemental=tuple(supplemental),
        data_status=data_status,
    )


# --- Volume & price: percentile_score(volume_ratio_20d) ---------------------------


def explain_volume_price(
    *, volume_ratio_20d: float | None, score: float | None
) -> FactorExplanation:
    if volume_ratio_20d is None or score is None:
        return FactorExplanation(data_status="資料不足")

    reasons = [f"今日量比 {volume_ratio_20d:.2f} 倍（今日成交量 ÷ 近 20 日均量）"]

    if volume_ratio_20d < 1.0:
        reasons.append("今日成交量低於近 20 日平均量")
    elif volume_ratio_20d >= 2.0:
        reasons.append("今日成交量達近 20 日平均量 2 倍以上")

    return FactorExplanation(reasons=tuple(reasons[:2]))


# --- Momentum: bounded_momentum_score(return_5d), absolute rule (not a percentile) ----


def explain_momentum(
    *,
    return_5d: float | None,
    return_20d: float | None,
    score: float | None,
    risk_flags: tuple[str, ...],
) -> FactorExplanation:
    if return_5d is None or score is None:
        return FactorExplanation(data_status="資料不足")

    reasons = [f"近 5 日累積報酬率 {_pct(return_5d)}"]

    # Use the exact same thresholds as text_renderer._momentum_signal_word to avoid
    # divergent logic or inconsistent messaging when thresholds change.
    if score < 40 and "HIGH_FIVE_DAY_RETURN" in risk_flags:
        reasons.append("短線漲幅過高，非單調動能模型已進入過熱扣分區")
    elif MOMENTUM_IDEAL_LOW <= return_5d <= MOMENTUM_IDEAL_HIGH:
        reasons.append(
            f"位於目前策略設定的理想動能區間"
            f"（{MOMENTUM_IDEAL_LOW * 100:.0f}%～{MOMENTUM_IDEAL_HIGH * 100:.0f}%）"
        )
    elif return_5d <= 0:
        reasons.append("近 5 日報酬未呈現正向動能")
    elif return_5d < MOMENTUM_IDEAL_LOW:
        reasons.append("正報酬仍低於目前理想動能區間")
    else:
        # return_5d > MOMENTUM_IDEAL_HIGH，且尚未觸發過熱旗標
        reasons.append("已高於理想動能區間，分數開始隨漲幅擴大反向下降")

    supplemental: list[str] = []
    if return_20d is not None:
        supplemental.append(f"近 20 日累積報酬率 {_pct(return_20d)}（僅供參考）")

    return FactorExplanation(
        reasons=tuple(reasons[:2]), supplemental=tuple(supplemental)
    )


# --- Institutional: percentile_score(institutional_net_buy_ratio_5d) -------------


def explain_institutional(
    *, institutional_net_buy_ratio_5d: float | None, score: float | None
) -> FactorExplanation:
    if institutional_net_buy_ratio_5d is None or score is None:
        return FactorExplanation(data_status="資料不足")

    ratio = institutional_net_buy_ratio_5d
    reasons = [f"近 5 個交易日法人淨買超佔成交量比重 {_pct(ratio)}"]

    if ratio < 0:
        reasons.append("近 5 日法人合計為淨賣超")
    elif ratio < 0.02:
        reasons.append("買超方向為正，但力度有限")

    return FactorExplanation(reasons=tuple(reasons[:2]))


# --- Fundamental: percentile_score(revenue_yoy) (latest month only, excluding EPS) -----


def explain_fundamental(
    *, revenue_yoy: float | None, score: float | None
) -> FactorExplanation:
    """Intentionally consumes only the raw revenue_yoy value: the "fundamental"
    factor in scoring.py uses the latest month's revenue_yoy for the percentile_score.
    It does NOT include fundamental_growth_sustained or eps_growth_sustained (those
    are separate display-only signals rendered in the dedicated fundamental growth
    block; see text_renderer._render_fundamental_growth_lines). Do NOT fold them
    into this factor's reasons, as that would imply EPS influenced this factor's score.
    """
    if revenue_yoy is None or score is None:
        return FactorExplanation(data_status="資料不足")

    reasons = [f"最新月營收 YoY {_pct(revenue_yoy)}"]

    if revenue_yoy < 0:
        reasons.append("最新月營收較去年同期衰退")

    return FactorExplanation(reasons=tuple(reasons[:2]))


# --- Risk quality: percentile_score(risk_quality_raw), raw computed from RISK_FLAG_PENALTIES ---


def explain_risk_quality(
    *,
    risk_quality_raw: float | None,
    score: float | None,
    risk_flags: tuple[str, ...],
    risk_missing_inputs: tuple[str, ...],
) -> FactorExplanation:
    # Check risk_missing_inputs (the causes) first, not by inferring from
    # risk_quality_raw is None (the effect) — in theory they should always match
    # (see build_risk_quality_raw docstring), but reasoning from causes is clearer
    # and safer if the two ever diverge unexpectedly.
    if risk_missing_inputs:
        confirmed = tuple(
            _CONFIRMED_INPUT_LABELS[name]
            for name in _RISK_INPUT_LABELS
            if name not in risk_missing_inputs
        )
        missing = tuple(
            _RISK_INPUT_LABELS[name]
            for name in risk_missing_inputs
            if name in _RISK_INPUT_LABELS
        )
        status = "部分缺失" if confirmed else "資料不足"
        return FactorExplanation(
            confirmed=confirmed, missing=missing, data_status=status
        )

    if risk_quality_raw is None or score is None:
        # Defensive branch: theoretically shouldn't reach here (when missing_inputs
        # is empty, risk_quality_raw should be present), but prefer to honestly
        # indicate "資料不足" rather than fabricate reasons when data is inconsistent.
        return FactorExplanation(data_status="資料不足")

    reasons = [f"原始風險品質 {risk_quality_raw:.2f}（1.00 為無任何風險旗標）"]

    # Flags that actually penalize vs. flags that are currently zero-penalty/display-only
    # must never be mixed in the same sentence — rely solely on risk_policy.RISK_FLAG_PENALTIES
    # as the single source of truth here to avoid drifting thresholds.
    penalized = [f for f in risk_flags if RISK_FLAG_PENALTIES.get(f, 0.0) > 0]
    for flag in penalized:
        penalty = RISK_FLAG_PENALTIES[flag]
        reasons.append(f"{_RISK_FLAG_LABELS.get(flag, flag)}：風險品質扣 {penalty:.2f}")

    supplemental = tuple(
        f"{_RISK_FLAG_LABELS.get(flag, flag)}（目前未影響風險品質評分）"
        for flag in risk_flags
        if RISK_FLAG_PENALTIES.get(flag, 0.0) == 0.0
    )

    return FactorExplanation(
        reasons=tuple(reasons[:2]), supplemental=supplemental, data_status="完整"
    )
