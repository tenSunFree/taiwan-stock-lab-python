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
from dataclasses import dataclass

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
) -> str:
    """
    candidate_count: how many stocks entered CandidateBuilder's pool
        (already filtered by minimum_turnover and capped at
        maximum_candidates — NOT a raw "every limit-up common stock
        in the whole market today" count).
    eligible_count: how many of those cleared the RiskPolicy hard
        exclusions and the scoring completeness gate
        (data_completeness >= minimum_data_completeness), i.e. how
        many were actually eligible to be considered for the Top 5.
    """
    lines = [
        f"【每日漲停股量化觀察｜{trading_date:%Y/%m/%d}】",
        "",
        f"資料更新：{data_updated_at}",
        f"進入候選池：{candidate_count} 檔",
        f"通過資料完整度門檻：{eligible_count} 檔",
        f"策略版本：{strategy_version}",
        "",
    ]

    for stock in ranked_stocks:
        lines.append(f"{stock.rank}. {stock.stock_name}（{stock.stock_id}）")
        lines.append(f"綜合分數：{stock.total_score:.2f}")
        lines.append(f"資料完整度：{stock.data_completeness:.0%}")
        lines.append("")

        if stock.top_factor_names:
            lines.append("主要優勢：")
            lines.extend(f"・{name}" for name in stock.top_factor_names)
            lines.append("")

        lines.append("風險：")
        if stock.risk_flags:
            lines.extend(
                f"・{RISK_FLAG_DISPLAY.get(flag, flag)}" for flag in stock.risk_flags
            )
        else:
            lines.append("・今日收盤漲停，隔日仍有追價與成交風險")
        lines.append("")

    lines.append(DISCLAIMER)
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
) -> str:
    """Some days will have limit-up stocks but none passing the
    data-completeness bar. This still needs to be pushed so the reader
    can confirm the pipeline ran normally — never fail silently."""
    return "\n".join(
        [
            f"【每日漲停股量化觀察｜{trading_date:%Y/%m/%d}】",
            "",
            f"資料更新：{data_updated_at}",
            f"進入候選池：{candidate_count} 檔",
            "今日無符合資料完整度門檻的候選股，暫無 Top 5 名單。",
            f"策略版本：{strategy_version}",
            "",
            DISCLAIMER,
        ]
    )
