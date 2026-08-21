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


# Display names for factor scores, used when rendering the report
FACTOR_DISPLAY_NAMES: dict[str, str] = {
    "liquidity": "流動性",
    "volume_price": "量價結構",
    "momentum": "動能",
    "institutional": "法人籌碼",
    "fundamental": "基本面",
    "risk_quality": "風險品質",
}

RISK_FLAG_DISPLAY: dict[str, str] = {
    "ATTENTION_STOCK": "注意股",
    "KY_STOCK": "KY 股",
    "ONE_PRICE_LIMIT_UP": "一字漲停，隔日追價風險偏高",
    "EXCESSIVE_CONSECUTIVE_LIMIT_UP": "連續漲停天數偏高",
    "HIGH_FIVE_DAY_RETURN": "近 5 日累積漲幅偏高",
}

# Why a given factor is missing, keyed by the raw factor name (see
# app.domain.scoring.FACTOR_WEIGHTS). Shown in the "缺失資料" section
# so a reader can see *why* data_completeness isn't 100% instead of
# just seeing an unexplained percentage.
MISSING_FACTOR_REASON: dict[str, str] = {
    "risk_quality": ("風險品質（注意／處置狀態尚未串接官方資料源，暫無法評分）"),
    "liquidity": "流動性（成交量/成交金額資料缺失）",
    "volume_price": "量價結構（歷史量價資料不足）",
    "momentum": "動能（近期報酬率資料不足）",
    "institutional": "法人籌碼（法人買賣超資料缺失）",
    "fundamental": "基本面（月營收資料缺失）",
}


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
    top_factor_names: tuple[str, ...]  # the 1-2 highest-scoring factors
    risk_flags: tuple[str, ...]
    close_price: Decimal | None = None
    change_percent: float | None = None  # vs. reference (previous close) price
    missing_factor_names: tuple[str, ...] = field(default_factory=tuple)
    is_one_price_limit_up: bool = False


def top_factors(
    factor_scores: dict[str, float | None], limit: int = 2
) -> tuple[str, ...]:
    """Pick the N highest-scoring factor names (display form) for the
    report's "主要優勢" line."""
    available = [
        (name, value) for name, value in factor_scores.items() if value is not None
    ]
    available.sort(key=lambda item: item[1], reverse=True)
    return tuple(FACTOR_DISPLAY_NAMES.get(name, name) for name, _ in available[:limit])


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
        at most — purely for the "展示範圍" display line below; it
        does not itself limit len(ranked_stocks) (the caller already
        did that upstream via select_top_n).
    """
    lines = [
        f"【每日漲停股量化觀察｜{trading_date:%Y/%m/%d}】",
        "",
        "📌 功能進度",
        f"✅ 顯示 Top {ranking_limit}",
        "⬜ 估值：0 < P/E ≤ 20",
        "⬜ 注意／處置有價證券官方風控",
        "⬜ 法人籌碼：近 3 個交易日累積買超 > 0",
        "⬜ 技術面：低檔且具起漲訊號",
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

    for stock in ranked_stocks:
        lines.append(f"{stock.rank}. {stock.stock_name}（{stock.stock_id}）")

        if stock.close_price is not None:
            lines.append(f"收盤價：{stock.close_price} 元")

        if stock.change_percent is not None:
            lines.append(f"漲幅：{stock.change_percent:+.2f}%")

        lines.append(f"綜合分數：{stock.total_score:.2f}")
        lines.append(f"資料完整度：{stock.data_completeness:.0%}")

        if stock.missing_factor_names:
            missing_display = "、".join(
                MISSING_FACTOR_REASON.get(name, name)
                for name in stock.missing_factor_names
            )
            lines.append(f"缺失資料：{missing_display}")

        lines.append("")

        if stock.top_factor_names:
            lines.append("主要得分來源：")
            lines.extend(f"・{name}" for name in stock.top_factor_names)
            lines.append("")

        if stock.is_one_price_limit_up:
            lines.append("型態特徵：")
            lines.append("・一字漲停")
            lines.append("")

        lines.append("風險提示：")
        if stock.risk_flags:
            lines.extend(
                f"・{RISK_FLAG_DISPLAY.get(flag, flag)}" for flag in stock.risk_flags
            )
        else:
            lines.append("・今日收盤漲停，隔日追高、開板及價格波動風險偏高")

        lines.append("")

    lines.extend(
        [
            "模型說明",
            (
                f"綜合分數為各量化因子依 {strategy_version} "
                "加權計算後的相對評分，用於候選標的之間的排序，"
                "不代表預測報酬率、上漲機率或目標價。"
            ),
            (
                "「主要得分來源」代表對該標的綜合分數貢獻較高的"
                "量化因子，並非對個股基本面或未來股價的主觀評價。"
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
            "⬜ 估值：0 < P/E ≤ 20",
            "⬜ 注意／處置有價證券官方風控",
            "⬜ 法人籌碼：近 3 個交易日累積買超 > 0",
            "⬜ 技術面：低檔且具起漲訊號",
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
