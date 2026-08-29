"""
Fixed-template report rendering (Traditional Chinese).

Phase 4 does not use an LLM — the report is produced entirely from
rule-based templates. This renderer must also stay in place after an
LLM summarizer is added in a later phase, as the fallback for when the
LLM output fails schema/content validation (see Roadmap).

Output must never use promotional or advisory language ("必買",
"明牌", "保證獲利", "最佳買點" etc.) and must always include the
research disclaimer verbatim.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

DISCLAIMER = (
    "本清單依公開市場資料及固定量化規則產生，"
    "僅供研究與資料整理，不構成買進、賣出或持有建議。"
)

# LINE text messages are limited to 5000 characters, counted in
# UTF-16 code units (not Python's len(), which counts Unicode code
# points and would undercount characters outside the Basic
# Multilingual Plane). See:
# https://developers.line.biz/en/docs/partner-docs/development-guidelines/
MAX_LINE_TEXT_UTF16_UNITS = 5000


def utf16_length(text: str) -> int:
    """Length of `text` as LINE counts it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


# Display names for factor scores, used when rendering the report.
# Still imported by report_builder.py to build top_factor_names —
# kept even though the current template no longer renders a
# standalone "主要得分來源" section, so that call site doesn't need
# to change as part of this rollout.
FACTOR_DISPLAY_NAMES: dict[str, str] = {
    "liquidity": "流動性",
    "volume_price": "量價",
    "momentum": "動能",
    "institutional": "籌碼",
    "fundamental": "基本面",
    "risk_quality": "風險品質",
}

# Order the six factors appear in the "訊號" block. Deliberately
# includes risk_quality — its factor_scores entry is already a
# normalized 0-100 value (or None when risk_missing_inputs is
# non-empty), so it renders through the exact same emoji/word logic
# as every other factor; no separate special-casing needed here.
_SIGNAL_FACTOR_ORDER: tuple[tuple[str, str], ...] = (
    ("liquidity", "流動性"),
    ("volume_price", "量價"),
    ("momentum", "動能"),
    ("institutional", "籌碼"),
    ("fundamental", "基本面"),
    ("risk_quality", "風險品質"),
)

# Why a given (non-risk_quality) factor is missing, keyed by the raw
# factor name (see app.domain.scoring.FACTOR_WEIGHTS). risk_quality is
# deliberately NOT in this dict — its reason is computed dynamically
# from risk_missing_inputs by _risk_quality_missing_reason() below,
# since "why risk_quality is missing" can be any combination of four
# different underlying inputs, not a single fixed sentence.
MISSING_FACTOR_REASON: dict[str, str] = {
    "liquidity": "流動性（成交量/成交金額資料缺失）",
    "volume_price": "量價結構（歷史量價資料不足）",
    "momentum": "動能（近期報酬率資料不足）",
    "institutional": "法人籌碼（法人買賣超資料缺失）",
    "fundamental": "基本面（月營收資料缺失）",
}

# Display labels for RiskAssessment's missing_inputs entries (see
# app.domain.risk_policy.RiskAssessment.missing_inputs) — these are
# the FOUR underlying tri-state inputs RiskPolicy needs, distinct from
# the six scoring factors above. is_attention/is_disposition being
# regulatory-source-fetch failures is why this can appear even though
# TWSE/TPEx attention & disposition are wired in (see
# app.jobs.daily_ranking's Step 1d) — a source failure this run still
# leaves that specific input Unknown for this run.
_RISK_MISSING_INPUT_DISPLAY: dict[str, str] = {
    "is_attention": "注意股狀態",
    "is_disposition": "處置股狀態",
    "is_managed": "全額交割／變更交易方法狀態",
    "consecutive_limit_up_days": "連續漲停天數",
}


def _risk_quality_missing_reason(missing_inputs: tuple[str, ...]) -> str:
    """
    Builds the "風險品質" gap sentence FROM THE ACTUAL missing inputs
    for this stock, rather than a single hardcoded sentence — the
    bug this replaces: a static string that said "注意／處置狀態尚未
    串接官方資料源" even after attention/disposition were wired in,
    because it never looked at which inputs were actually still
    missing. See RiskAssessment.missing_inputs / RiskPolicy.assess()
    for where this tuple originates.
    """
    if not missing_inputs:
        return "風險品質（資料不足，暫無法評分）"
    labels = [_RISK_MISSING_INPUT_DISPLAY.get(name, name) for name in missing_inputs]
    return f"風險品質（{'、'.join(labels)}尚未確認，暫無法完整評分）"


@dataclass(frozen=True)
class ReportStockView:
    """Minimal data needed to render a report entry. Kept separate
    from ScoredStock so the report layer never depends directly on the
    scoring module's internal structure."""

    rank: int
    stock_id: str
    stock_name: str
    total_score: float
    data_completeness: float
    top_factor_names: tuple[str, ...]  # kept for callers/tests; not rendered directly
    risk_flags: tuple[str, ...]
    close_price: Decimal | None = None
    change_percent: float | None = None  # vs. reference (previous close) price
    missing_factor_names: tuple[str, ...] = field(default_factory=tuple)
    is_one_price_limit_up: bool = False

    # Official regulatory risk detail (see app.domain.models.
    # RegulatoryRiskStatus) — deliberately just the SHORT fields here,
    # not disposition_measure's full legal-text paragraph. A real
    # disposition_measure can run several hundred characters (verified
    # against real TWSE/TPEx fixtures), and this view can hold many
    # stocks in one LINE message capped at
    # text_renderer.MAX_LINE_TEXT_UTF16_UNITS — reproducing the full
    # legal text per flagged stock risks blowing that budget on a day
    # with several disposition stocks. attention_reason and
    # disposition_reason are the SHORT trigger-condition fields
    # (observed ~20-40 characters in real fixtures), safe to include
    # directly; the reader is pointed to the official announcement for
    # the full text instead of it being reproduced here.
    attention_reason: str | None = None
    disposition_start_date: dt.date | None = None
    disposition_end_date: dt.date | None = None
    disposition_reason: str | None = None

    # Every factor's own 0-100 normalized score (or None when that
    # factor couldn't be scored) — same dict ScoredStock.factor_scores
    # carries. Drives the "訊號" block; deliberately the FULL dict, not
    # just the top 1-2 names, so volume_price and risk_quality (which
    # may not be "top factors") are still visible.
    factor_scores: dict[str, float | None] = field(default_factory=dict)

    # From StockFeatures — used for the "漲停結構" block's volume-ratio
    # line. None means "not computed" (e.g. insufficient trailing
    # history), rendered as an explicit "資料不足", never silently
    # omitted (see _render_limit_up_structure_lines).
    volume_ratio_20d: float | None = None

    # From StockFeatures — DISPLAY-ONLY signal for the "法人籌碼"
    # block: whether cumulative institutional net-buy shares over the
    # trailing 3 sessions is strictly positive (see
    # app.domain.institutional_flow_builder.build_institutional_net_buy_positive).
    # Tri-state: True/False are confirmed answers, None means
    # insufficient data — always rendered explicitly (never silently
    # omitted), matching this module's convention for every other
    # optional signal. Independent of the "institutional" scoring
    # factor already carried via factor_scores above (a different
    # window and a different calculation — see that module's
    # docstring for why the two must not be conflated).
    institutional_net_buy_3d_positive: bool | None = None

    # From StockFeatures — ANOTHER DISPLAY-ONLY signal, for the
    # "技術面" block: whether today's close is both near the low end
    # of its own trailing 20-session trading range AND just crossed
    # above its own 5-session moving average today (see
    # app.domain.technical_signal_builder.build_low_with_rising_signal
    # for the exact thresholds and crossover definition). Tri-state,
    # same rendering convention as institutional_net_buy_3d_positive
    # above. Independent of the "momentum" scoring factor already
    # carried via factor_scores (a completely different calculation —
    # a price-range/moving-average check, not a return-based one).
    technical_low_with_rising_signal: bool | None = None

    # RiskAssessment.missing_inputs, carried all the way through
    # StockFeatures -> ScoredStock -> here. This is what lets the
    # renderer distinguish "officially confirmed clean" (False, not in
    # this tuple) from "genuinely unknown" (the underlying input is
    # None, present in this tuple) for is_attention/is_disposition/
    # is_managed — and is also what _risk_quality_missing_reason uses
    # to build an accurate sentence instead of a stale hardcoded one.
    risk_missing_inputs: tuple[str, ...] = field(default_factory=tuple)


def top_factors(
    factor_scores: dict[str, float | None], limit: int = 2
) -> tuple[str, ...]:
    """Pick the N highest-scoring factor names (display form). Not
    used by the current per-stock template (superseded by the "訊號"
    block, which shows every factor), but kept as a standalone utility
    since report_builder.py still calls it to populate
    ReportStockView.top_factor_names."""
    available = [
        (name, value) for name, value in factor_scores.items() if value is not None
    ]
    available.sort(key=lambda item: item[1], reverse=True)
    return tuple(FACTOR_DISPLAY_NAMES.get(name, name) for name, _ in available[:limit])


# --- 訊號 (factor signal lights) ---------------------------------------------


def _signal_emoji(score: float | None) -> str:
    """0-100 的因子分數轉成燈號。None（尚未評分）用 ⚪，不用紅燈——
    紅燈代表「已知表現差」，None 代表「不知道」，兩者語意不同，不該
    混在一起顯示成同一個顏色。"""
    if score is None:
        return "⚪"
    if score >= 70:
        return "🟢"
    if score >= 40:
        return "🟡"
    return "🔴"


# _signal_word 的「普通／強」分界值。_momentum_signal_word 的過熱覆寫
# 只在分數落在這個分界值以下（即會被標成「偏弱」的區間）時才生效，
# 所以兩處共用同一個常數，避免未來各自改動、產生分界不一致的 bug。
_WEAK_SIGNAL_THRESHOLD = 40


def _signal_word(score: float | None) -> str:
    if score is None:
        return "資料不足"
    if score >= 70:
        return "強"
    if score >= _WEAK_SIGNAL_THRESHOLD:
        return "普通"
    return "偏弱"


def _momentum_signal_word(score: float | None, risk_flags: tuple[str, ...]) -> str:
    """
    Momentum is intentionally non-monotonic (see
    app.domain.normalization.bounded_momentum_score's own docstring:
    3%-15% cumulative 5-day return is the ideal/full-score band, and
    the score is deliberately penalized above that — an already
    extended rally is chase-in risk, not automatically a stronger
    signal). A LOW momentum score can therefore mean either
    (1) recent price momentum is genuinely weak, or (2) the stock has
    already rallied too much and is being penalized for overheating —
    these are very different situations for a reader to act on, and
    the generic "偏弱" wording collapses them into one misleading label.

    HIGH_FIVE_DAY_RETURN (RiskPolicy's own excessive-5-day-return flag,
    threshold configurable via RiskPolicyConfig.excessive_return_5d) is
    reused here purely as a REPORT-LEVEL display hint to distinguish
    case (2) from case (1) — it is not introduced as a new scoring
    factor, and does not change rule-v1.2.0's FACTOR_WEIGHTS or
    bounded_momentum_score in any way. The two signals are correlated
    but not identical (their thresholds don't line up exactly), so
    this is a display-only proxy, not a formal redefinition of what
    "momentum" measures.

    IMPORTANT — the override only fires when the score itself is
    already in the "偏弱" range (score < _WEAK_SIGNAL_THRESHOLD).
    RiskPolicyConfig.excessive_return_5d is independently configurable
    from bounded_momentum_score's own thresholds, so HIGH_FIVE_DAY_RETURN
    can in principle be raised on a stock whose momentum score is still
    40+ ("普通") or even 70+ ("強") — e.g. if excessive_return_5d is
    tuned lower than the return level that actually tanks the score.
    In that case the momentum reading is genuinely fine and must keep
    showing its real "普通"/"強" word; blindly overwriting a decent or
    strong score with "漲多過熱" would itself be a misleading label,
    which is the exact failure mode this function exists to avoid.
    """
    if score is None:
        return "資料不足"
    if score < _WEAK_SIGNAL_THRESHOLD and "HIGH_FIVE_DAY_RETURN" in risk_flags:
        return "漲多過熱"
    return _signal_word(score)


def _render_signal_lines(
    factor_scores: dict[str, float | None], risk_flags: tuple[str, ...]
) -> list[str]:
    lines = ["訊號"]
    for key, label in _SIGNAL_FACTOR_ORDER:
        score = factor_scores.get(key)
        word = (
            _momentum_signal_word(score, risk_flags)
            if key == "momentum"
            else _signal_word(score)
        )
        lines.append(f"{_signal_emoji(score)} {label}：{word}")
    return lines


# --- 監管狀態 (tri-state regulatory status) -----------------------------------


def _is_risk_input_missing(stock: ReportStockView, name: str) -> bool:
    return name in stock.risk_missing_inputs


def _attention_status_line(stock: ReportStockView) -> str:
    """
    Labeled "今日公布注意" (announced TODAY), not "注意股：正常" —
    attention is a per-day announcement (see
    twse_regulatory_mapper.build_twse_attention_statuses's own
    docstring: matched by EXACT announcement date, not an active
    range), so the correct question this line answers is "was this
    stock announced as an attention security today," not "is this
    stock currently an attention stock" — the latter isn't even a
    well-defined question for a per-day announcement. This wording
    also prevents readers from misreading "今日公布注意：否" alongside
    an active "🚨 目前處置：是" as a contradiction — the two lines
    deliberately answer different-shaped questions (today's
    announcement vs. an active period), so they can disagree without
    either being wrong.
    """
    if "ATTENTION_STOCK" in stock.risk_flags:
        if stock.attention_reason:
            return f"⚠️ 今日公布注意：是（{stock.attention_reason}）"
        return "⚠️ 今日公布注意：是"
    if _is_risk_input_missing(stock, "is_attention"):
        return "⚪ 今日公布注意：待確認"
    return "✅ 今日公布注意：否"


def _disposition_status_lines(stock: ReportStockView) -> list[str]:
    """
    Labeled "目前處置" (currently active), not "處置股：處置中" —
    disposition is evaluated against an ACTIVE PERIOD (see
    build_twse_disposition_statuses's own docstring: start <=
    target_date <= end), a genuinely different time semantics from
    attention's per-day announcement above. The concrete trading
    measures (matching mechanism, credit-trading restrictions,
    advance-deposit requirements) are deliberately NOT rendered here,
    even though disposition_measure carries the full official text —
    those measures vary by announcement round and by the exchange's
    own rule revisions (e.g. TWSE's disposition-measure overhaul
    effective 2026-08-10), so hardcoding any specific measure text
    risks describing rules that no longer apply to this particular
    announcement. Pointing the reader to the official announcement is
    safer than a plausible-looking but potentially wrong summary.
    """
    if "DISPOSITION_STOCK" in stock.risk_flags:
        lines = ["🚨 目前處置：是"]
        if stock.disposition_start_date and stock.disposition_end_date:
            lines.append(
                "　處置期間："
                f"{stock.disposition_start_date:%Y/%m/%d}"
                f"～{stock.disposition_end_date:%Y/%m/%d}"
            )
        if stock.disposition_reason:
            lines.append(f"　處置原因：{stock.disposition_reason}")
        lines.append("　處置措施：請依交易所該次公告為準")
        return lines
    if _is_risk_input_missing(stock, "is_disposition"):
        return ["⚪ 目前處置：待確認"]
    return ["✅ 目前處置：否"]


def _managed_status_line(stock: ReportStockView) -> str:
    if "MANAGED_STOCK" in stock.risk_flags:
        return "🚨 全額交割／變更交易方法：是"
    if _is_risk_input_missing(stock, "is_managed"):
        return "⚪ 全額交割／變更交易方法：待確認"
    return "✅ 全額交割／變更交易方法：否"


def _render_regulatory_status_lines(stock: ReportStockView) -> list[str]:
    lines = ["監管狀態", _attention_status_line(stock)]
    lines.extend(_disposition_status_lines(stock))
    lines.append(_managed_status_line(stock))
    return lines


# --- 法人籌碼 (institutional net-buy, display-only tri-state) ------------------
#
# This is intentionally NOT the same thing as the "institutional"
# entry in factor_scores/訊號 above: that is a 0-100 normalized score
# built from a 5-session net-buy/volume ratio, used in the weighted
# total score. This block instead answers a much narrower, literal
# question — "did institutions net-buy in aggregate over the last 3
# sessions" — as a plain yes/no/unknown fact, independent of scoring.
# See app.domain.institutional_flow_builder.build_institutional_net_buy_positive
# for the calculation and its no-look-ahead / strict-window rules.


def _render_institutional_flow_lines(stock: ReportStockView) -> list[str]:
    value = stock.institutional_net_buy_3d_positive
    if value is None:
        line = "⚪ 近 3 個交易日累積買超 > 0：資料不足"
    elif value:
        line = "✅ 近 3 個交易日累積買超 > 0：是"
    else:
        line = "❌ 近 3 個交易日累積買超 > 0：否"
    return ["法人籌碼", line]


# --- 技術面 (low-position + early-rally, display-only tri-state) --------------
#
# This is intentionally NOT the same thing as the "momentum" entry in
# factor_scores/訊號 above: that is a 0-100 normalized score built from
# 5-day/20-day cumulative RETURNS, used in the weighted total score.
# This block instead answers a much narrower, literal question — "is
# today's close both near the bottom of its own recent trading range
# AND has it just crossed above its own 5-day moving average today" —
# as a plain yes/no/unknown fact, independent of scoring. See
# app.domain.technical_signal_builder.build_low_with_rising_signal for
# the exact thresholds, crossover definition, and no-look-ahead /
# strict-window rules.


def _render_technical_signal_lines(stock: ReportStockView) -> list[str]:
    value = stock.technical_low_with_rising_signal
    if value is None:
        line = "⚪ 低檔且具起漲訊號：資料不足"
    elif value:
        line = "✅ 低檔且具起漲訊號：是"
    else:
        line = "❌ 低檔且具起漲訊號：否"
    return ["技術面", line]


# --- 漲停結構 (Phase A subset — no intraday data yet) --------------------------


def _render_limit_up_structure_lines(stock: ReportStockView) -> list[str]:
    lines = ["漲停結構"]
    lines.append("・一字漲停" if stock.is_one_price_limit_up else "・非一字漲停")
    if stock.volume_ratio_20d is not None:
        lines.append(f"・成交量 {stock.volume_ratio_20d:.1f}×20 日均量")
    else:
        lines.append("・20 日量比：資料不足")
    return lines


# --- 主要風險 ------------------------------------------------------------------

# Regulatory flags (ATTENTION_STOCK/DISPOSITION_STOCK/MANAGED_STOCK)
# are deliberately NOT in this dict — they're already covered in full
# detail by the 監管狀態 block above; listing them again here would be
# redundant.
_PRIMARY_RISK_FLAG_LABELS: dict[str, str] = {
    "ONE_PRICE_LIMIT_UP": "一字漲停，流動性與成交機會風險",
    "EXCESSIVE_CONSECUTIVE_LIMIT_UP": "連續漲停天數偏高",
    "HIGH_FIVE_DAY_RETURN": "短線過熱",
    "KY_STOCK": "KY 股制度與資訊揭露差異風險",
}


def _render_primary_risk_lines(stock: ReportStockView) -> list[str]:
    # Base risks every limit-up stock carries, always shown first —
    # flag-driven risks are ADDITIONAL to these, never a replacement
    # for them (a stock with HIGH_FIVE_DAY_RETURN still also carries
    # ordinary next-day chase-in / opening risk).
    labels = ["隔日追價風險", "開板風險"]
    for flag in stock.risk_flags:
        label = _PRIMARY_RISK_FLAG_LABELS.get(flag)
        if label and label not in labels:
            labels.append(label)
    return ["主要風險", *(f"・{label}" for label in labels)]


# --- 資料缺口 -------------------------------------------------------------------


def _render_data_gap_line(stock: ReportStockView) -> str | None:
    gaps: list[str] = []
    for factor_name in stock.missing_factor_names:
        if factor_name == "risk_quality":
            continue  # handled separately below, using risk_missing_inputs
        reason = MISSING_FACTOR_REASON.get(factor_name)
        if reason:
            gaps.append(reason)
    if "risk_quality" in stock.missing_factor_names:
        gaps.append(_risk_quality_missing_reason(stock.risk_missing_inputs))
    if not gaps:
        return None
    return "資料缺口：" + "、".join(gaps)


# --- Per-stock block ------------------------------------------------------------


def _render_stock_block(stock: ReportStockView, *, total_shown: int) -> list[str]:
    lines = [
        f"{stock.rank}. {stock.stock_name}（{stock.stock_id}）",
    ]

    if stock.close_price is not None:
        lines.append(f"收盤價：{stock.close_price} 元")
    if stock.change_percent is not None:
        lines.append(f"漲幅：{stock.change_percent:+.2f}%")

    lines.append(f"綜合分數：{stock.total_score:.2f}")
    # Denominator is the number of stocks ACTUALLY appearing in today's
    # report (len(ranked_stocks) at the call site), not the configured
    # ranking_limit ("Top N" display cap) — those are two different
    # numbers. If fewer stocks cleared the completeness gate than
    # ranking_limit allows for, today's list is shorter than the cap,
    # and the rank line must reflect the list the reader is actually
    # looking at (e.g. "1 / 5" when only 5 stocks qualified), not the
    # unrelated configured maximum (e.g. "1 / 10"), which would read as
    # a contradiction next to a 5-stock list.
    lines.append(f"今日排名：{stock.rank} / {total_shown}")
    lines.append(f"資料完整度：{stock.data_completeness:.0%}")

    gap_line = _render_data_gap_line(stock)
    if gap_line is not None:
        lines.append(gap_line)

    lines.append("")
    lines.extend(_render_limit_up_structure_lines(stock))
    lines.append("")
    lines.extend(_render_signal_lines(stock.factor_scores, stock.risk_flags))
    lines.append("")
    lines.extend(_render_regulatory_status_lines(stock))
    lines.append("")
    lines.extend(_render_institutional_flow_lines(stock))
    lines.append("")
    lines.extend(_render_technical_signal_lines(stock))
    lines.append("")
    lines.extend(_render_primary_risk_lines(stock))
    lines.append("")

    return lines


def render_daily_report(
    *,
    trading_date: dt.date,
    data_updated_at: str,
    candidate_count: int,
    eligible_count: int,
    strategy_version: str,
    ranked_stocks: list[ReportStockView],
    ranking_limit: int = 10,
) -> str:
    """
    candidate_count: how many stocks entered CandidateBuilder's pool
        (already filtered by minimum_turnover and capped at
        maximum_candidates — NOT a raw "every limit-up common stock
        in the whole market today" count).
    eligible_count: how many of those cleared the RiskPolicy hard
        exclusions and the scoring completeness gate
        (data_completeness >= minimum_data_completeness), i.e. how
        many were actually eligible to be considered for the ranking.
    ranking_limit: how many stocks select_top_n() was asked to return
        AT MOST — used for the "顯示 Top N" checklist line and the
        "展示範圍：綜合分數 Top N" line, both of which describe the
        CONFIGURED display cap, not how many stocks actually made the
        list today.

        Deliberately NOT used as the denominator in each stock's
        "今日排名：N / ?" line — that denominator is len(ranked_stocks)
        instead, i.e. how many stocks are ACTUALLY in today's report.
        The two numbers legitimately differ whenever fewer stocks
        clear the completeness gate than ranking_limit allows for
        (e.g. only 5 stocks qualified today even though ranking_limit
        is 10): showing "1 / 10" next to a 5-stock list would read as
        a contradiction, since there is no stock ranked 6th through
        10th anywhere in the report to justify that denominator.
    """
    lines = [
        f"【每日漲停股量化觀察｜{trading_date:%Y/%m/%d}】",
        "",
        "📌 功能進度",
        f"✅ 顯示 Top {ranking_limit}",
        "✅ 估值：0 < P/E ≤ 20",
        "✅ 注意／處置有價證券官方風控",
        "✅ 六大因子訊號燈號 ＋ 監管狀態明細（含注意／處置／全額交割 True／False／未知）",
        "✅ 注意／處置時間語意區分（今日公告 vs 目前生效）＋ 動能過熱識別",
        "✅ 法人籌碼：近 3 個交易日累積買超 > 0",
        "✅ 技術面：低檔且具起漲訊號",
        "⬜ 基本面：營收或 EPS YoY ≥ 10%，且具持續性",
        "⬜ 產業題材：電子業且具 AI 相關性",
        "",
        "資料概況",
        f"資料更新：{data_updated_at}",
        f"進入候選池：{candidate_count} 檔",
        f"通過資料完整度門檻：{eligible_count} 檔",
        f"展示範圍：綜合分數 Top {ranking_limit}",
        f"策略版本：{strategy_version}",
        "",
    ]

    total_shown = len(ranked_stocks)
    for stock in ranked_stocks:
        lines.extend(_render_stock_block(stock, total_shown=total_shown))

    lines.extend(
        [
            "模型說明",
            (
                f"綜合分數為各量化因子依 {strategy_version} "
                "加權計算後的相對評分，用於當日候選標的之間排序，"
                "不代表預測報酬率、上漲機率或目標價。"
            ),
            (
                "「訊號」依各因子的標準化分數區間呈現（🟢強／🟡普通／🔴偏弱）；"
                "⚪ 代表該因子目前資料不足，並不代表負面訊號。"
            ),
            (
                "動能因子採非單調評分；顯示「漲多過熱」時，"
                "代表近期累積漲幅已達短線過熱門檻，"
                "反映追價風險升高，並非代表近期沒有上漲動能。"
            ),
            (
                "「法人籌碼」區塊顯示近 3 個交易日法人累積買超是否 > 0，"
                "為獨立於綜合分數之外的參考訊號，"
                "不會改變「訊號」區塊中籌碼因子的評分結果。"
            ),
            (
                "「技術面」區塊顯示今日收盤是否同時符合"
                "「位於近 20 個交易日價格區間下緣」及"
                "「今日剛站上 5 日均線」兩項條件，"
                "同樣為獨立於綜合分數之外的參考訊號，"
                "不會改變「訊號」區塊中動能因子的評分結果。"
            ),
            (
                "歷史分位及 T+1／T+5 統計尚未納入目前版本，"
                "待累積足夠歷史樣本及建立回測流程後提供。"
            ),
            DISCLAIMER,
        ]
    )

    result = "\n".join(lines)

    if utf16_length(result) > MAX_LINE_TEXT_UTF16_UNITS:
        raise ValueError(
            f"Rendered report exceeds LINE's {MAX_LINE_TEXT_UTF16_UNITS}-UTF16-unit "
            f"text message limit ({utf16_length(result)} units). Consider trimming "
            f"the number of reasons/risk flags per stock, or splitting into multiple "
            f"messages."
        )
    return result


def render_no_qualified_stock_report(
    *,
    trading_date: dt.date,
    data_updated_at: str,
    candidate_count: int,
    strategy_version: str,
    ranking_limit: int = 10,
) -> str:
    """Some days will have limit-up stocks but none passing the
    data-completeness bar. This still needs to be pushed so the reader
    can confirm the pipeline ran normally — never fail silently."""
    result = "\n".join(
        [
            f"【每日漲停股量化觀察｜{trading_date:%Y/%m/%d}】",
            "",
            "📌 功能進度",
            f"✅ 顯示 Top {ranking_limit}",
            "✅ 估值：0 < P/E ≤ 20",
            "✅ 注意／處置有價證券官方風控",
            "✅ 六大因子訊號燈號 ＋ 監管狀態明細（含注意／處置／全額交割 True／False／未知）",
            "✅ 注意／處置時間語意區分（今日公告 vs 目前生效）＋ 動能過熱識別",
            "✅ 法人籌碼：近 3 個交易日累積買超 > 0",
            "✅ 技術面：低檔且具起漲訊號",
            "⬜ 基本面：營收或 EPS YoY ≥ 10%，且具持續性",
            "⬜ 產業題材：電子業且具 AI 相關性",
            "",
            "資料概況",
            f"資料更新：{data_updated_at}",
            f"進入候選池：{candidate_count} 檔",
            f"今日無符合資料完整度門檻的候選股，暫無 Top {ranking_limit} 名單。",
            f"策略版本：{strategy_version}",
            "",
            "模型說明",
            ("本清單依固定量化規則篩選候選標的；今日沒有標的通過資料完整度門檻。"),
            DISCLAIMER,
        ]
    )

    if utf16_length(result) > MAX_LINE_TEXT_UTF16_UNITS:
        raise ValueError(
            "Rendered no-qualified-stock report exceeds LINE's "
            f"{MAX_LINE_TEXT_UTF16_UNITS}-UTF16-unit text message limit "
            f"({utf16_length(result)} units)."
        )

    return result
