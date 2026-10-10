"""
LINE Flex builder — one bubble per stock, bubbles grouped into carousels.

Responsibility boundary (LINE chart spec §26-§35, §40):
    * This module only LAYS OUT values the Domain already decided. It
      never recomputes a signal: 低檔首板 / 漲多過熱 are read from the
      three-state ReportStockView fields and shown as ✅ 是 / ❌ 否 /
      ⚪ 資料不足 (INSUFFICIENT_DATA is never turned into "否").
    * It never renders a chart and never uploads anything. Hero image
      URLs are INPUT (stock_id -> https URL) prepared elsewhere
      (app.reports.chart_images); a stock without an entry simply gets a
      bubble without a hero image.
    * It does not talk to LINE. It returns plain dicts + a fingerprint;
      app.delivery.service sends them.

Bubble layout (per stock):
    hero    4:3 image, aspectMode "fit", URI action -> the HTTPS original
            (only when a valid hero URL exists)
    body    name+code / close｜change / 綜合｜rank / 量比｜5D / 低檔首板 /
            漲多過熱 / 資料完整度 / [K-line note] / risks
    footer  disclaimer (+ the 低檔首板 approximation note)

Idempotency fingerprint:
    DeliveryRepository hashes a string per message to catch "same key,
    different content". For Flex that string is `fingerprint`, built from
    the text rows that carry meaning. It deliberately EXCLUDES the hero
    URLs and the K-line status note: whether a chart could be drawn or
    uploaded is a property of one particular run (a transient GCS error
    on a rerun must not turn into DeliveryContentConflict for data that
    is otherwise identical). Everything else — stocks, scores, signals,
    wording — is covered, so a real content change without a
    MESSAGE_VERSION bump is still caught.

LINE limits enforced here: <= 12 bubbles per carousel (more stocks are
split into several carousel messages), <= 50 KB per Flex message
(otherwise FlexBuildError -> the caller falls back to the text report),
hero action URI <= 1000 chars and https.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from app.domain.signal_status import SignalStatus
from app.reports.text_renderer import (
    DISCLAIMER,
    FIRST_BOARD_APPROXIMATION_NOTE,
    ReportStockView,
)

MAX_BUBBLES_PER_CAROUSEL = 12
MAX_FLEX_BYTES = 50_000
MAX_ALT_TEXT_CHARS = 400
MAX_ACTION_URI_CHARS = 1000

NOTE_CHART_UNAVAILABLE = "⚪ K 線圖暫時無法產生"
NOTE_CHART_INSUFFICIENT = "⚪ K 線資料不足"

_STATUS_LABEL: dict[SignalStatus, str] = {
    SignalStatus.TRUE: "✅ 是",
    SignalStatus.FALSE: "❌ 否",
    SignalStatus.INSUFFICIENT_DATA: "⚪ 資料不足",
}

# Compact, bubble-sized labels for the risk line. The long wording stays
# in the text report. HIGH_FIVE_DAY_RETURN is described factually rather
# than as "過熱": 漲多過熱 is a separate Domain decision shown on its own
# line, and the two must not read like competing verdicts.
_RISK_FLAG_LABELS: dict[str, str] = {
    "ATTENTION_STOCK": "注意股",
    "DISPOSITION_STOCK": "處置股",
    "MANAGED_STOCK": "全額交割或變更交易",
    "KY_STOCK": "KY 股",
    "ONE_PRICE_LIMIT_UP": "一字漲停",
    "EXCESSIVE_CONSECUTIVE_LIMIT_UP": "連續漲停偏高",
    "HIGH_FIVE_DAY_RETURN": "近 5 日漲幅偏高",
}
_BASE_RISK_LABELS = ("追價風險", "開板風險")
_REGULATORY_INPUTS = ("is_attention", "is_disposition", "is_managed")

_COLOR_UP = "#D32F2F"  # Taiwan convention: red = up
_COLOR_DOWN = "#2E7D32"
_COLOR_FLAT = "#666666"
_COLOR_TEXT = "#333333"
_COLOR_MUTED = "#888888"
_COLOR_RISK = "#B26A00"


class FlexBuildError(Exception):
    """A valid Flex message cannot be built. The caller falls back to
    the plain-text report."""


@dataclass(frozen=True)
class FlexCarousel:
    alt_text: str
    contents: dict  # the carousel container (type == "carousel")
    fingerprint: str  # idempotency content, see module docstring
    stock_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Row:
    kind: str  # title | price | normal | risk
    text: str
    color: str | None = None


def status_label(status: SignalStatus) -> str:
    return _STATUS_LABEL[status]


def _format_close(stock: ReportStockView) -> str:
    return "—" if stock.close_price is None else f"{stock.close_price:f}"


def _price_color(change_percent: float | None) -> str:
    if change_percent is None or change_percent == 0:
        return _COLOR_FLAT
    return _COLOR_UP if change_percent > 0 else _COLOR_DOWN


def _risk_labels(stock: ReportStockView) -> list[str]:
    labels = list(_BASE_RISK_LABELS)
    for flag in stock.risk_flags:
        label = _RISK_FLAG_LABELS.get(flag)
        if label and label not in labels:
            labels.append(label)
    return labels


def _summary_rows(stock: ReportStockView, *, total_shown: int) -> list[_Row]:
    """Every row that carries meaning, in display order. This list IS the
    fingerprint source, so anything run-specific must not be added here."""
    price_text = _format_close(stock)
    if stock.change_percent is not None:
        price_text += f"｜{stock.change_percent:+.2f}%"

    volume_ratio = (
        "資料不足"
        if stock.volume_ratio_20d is None
        else f"{stock.volume_ratio_20d:.2f}×"
    )
    return_5d = "資料不足" if stock.return_5d is None else f"{stock.return_5d:+.1%}"

    rows = [
        _Row("title", f"{stock.stock_name} {stock.stock_id}"),
        _Row("price", price_text, _price_color(stock.change_percent)),
        _Row("normal", f"綜合 {stock.total_score:.2f}｜{stock.rank}/{total_shown}"),
        _Row("normal", f"量比 {volume_ratio}｜5D {return_5d}"),
        _Row(
            "normal",
            f"低檔首板：{status_label(stock.low_level_first_limit_up_status)}",
        ),
        _Row(
            "normal",
            f"漲多過熱：{status_label(stock.momentum_overheated_status)}",
        ),
        _Row("normal", f"資料完整度：{stock.data_completeness:.0%}"),
    ]
    if any(name in stock.risk_missing_inputs for name in _REGULATORY_INPUTS):
        rows.append(_Row("normal", "⚪ 注意／處置／全額交割狀態未確認"))
    rows.append(_Row("risk", "⚠️ " + "／".join(_risk_labels(stock))))
    return rows


def _chart_note(stock: ReportStockView) -> str:
    """Why a bubble has no hero image (spec §33/§34)."""
    if stock.chart_data is None:
        return NOTE_CHART_INSUFFICIENT
    return NOTE_CHART_UNAVAILABLE


def _valid_hero_url(url: str | None) -> bool:
    return bool(url) and url.startswith("https://") and len(url) <= MAX_ACTION_URI_CHARS


def _text_component(row: _Row) -> dict:
    component: dict = {
        "type": "text",
        "text": row.text,
        "wrap": True,
        "color": row.color or _COLOR_TEXT,
    }
    if row.kind == "title":
        component.update(size="lg", weight="bold")
    elif row.kind == "price":
        component.update(size="xl", weight="bold")
    elif row.kind == "risk":
        component.update(size="sm", color=_COLOR_RISK)
    else:
        component.update(size="sm")
    return component


def build_stock_bubble(
    stock: ReportStockView, *, total_shown: int, hero_url: str | None
) -> dict:
    """One bubble. `hero_url` None / invalid -> no hero + a K-line note."""
    rows = _summary_rows(stock, total_shown=total_shown)

    contents: list[dict] = []
    has_hero = _valid_hero_url(hero_url)
    for row in rows:
        if row.kind == "risk":
            if not has_hero:
                contents.append(
                    {
                        "type": "text",
                        "text": _chart_note(stock),
                        "wrap": True,
                        "size": "sm",
                        "color": _COLOR_MUTED,
                    }
                )
            contents.append({"type": "separator", "margin": "md"})
        contents.append(_text_component(row))

    bubble: dict = {
        "type": "bubble",
        "size": "mega",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": contents,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "xs",
            "contents": [
                {
                    "type": "text",
                    "text": FIRST_BOARD_APPROXIMATION_NOTE,
                    "wrap": True,
                    "size": "xxs",
                    "color": _COLOR_MUTED,
                },
                {
                    "type": "text",
                    "text": DISCLAIMER,
                    "wrap": True,
                    "size": "xxs",
                    "color": _COLOR_MUTED,
                },
            ],
        },
    }
    if has_hero:
        bubble["hero"] = {
            "type": "image",
            "url": hero_url,
            "size": "full",
            "aspectRatio": "4:3",
            "aspectMode": "fit",
            "backgroundColor": "#FFFFFF",
            "action": {"type": "uri", "uri": hero_url},
        }
    return bubble


def build_alt_text(trading_date: dt.date, count: int) -> str:
    return f"每日漲停股量化觀察 {trading_date:%m/%d}｜{count} 檔"


def _fingerprint(
    *, alt_text: str, stocks: Sequence[ReportStockView], total_shown: int
) -> str:
    document = {
        "shape": "flex-carousel",
        "alt": alt_text,
        "bubbles": [
            {
                "id": stock.stock_id,
                "rows": [
                    [row.kind, row.text]
                    for row in _summary_rows(stock, total_shown=total_shown)
                ],
            }
            for stock in stocks
        ],
        "footer": [FIRST_BOARD_APPROXIMATION_NOTE, DISCLAIMER],
    }
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def build_flex_carousels(
    stocks: Sequence[ReportStockView],
    *,
    trading_date: dt.date,
    hero_urls: Mapping[str, str] | None = None,
) -> list[FlexCarousel]:
    """Carousel message(s) for today's stocks, in rank order.

    More than MAX_BUBBLES_PER_CAROUSEL stocks are split into several
    carousels (each is its own LINE message / delivery part). Raises
    FlexBuildError when nothing can be built or a message is over LINE's
    size limit; a missing/invalid hero URL is NOT an error.
    """
    if not stocks:
        raise FlexBuildError("no stocks to build a carousel from")
    hero_urls = hero_urls or {}
    total_shown = len(stocks)

    carousels: list[FlexCarousel] = []
    for start in range(0, total_shown, MAX_BUBBLES_PER_CAROUSEL):
        chunk = stocks[start : start + MAX_BUBBLES_PER_CAROUSEL]
        alt_text = build_alt_text(trading_date, len(chunk))
        if len(alt_text) > MAX_ALT_TEXT_CHARS:
            raise FlexBuildError("altText is over LINE's 400 character limit")
        contents = {
            "type": "carousel",
            "contents": [
                build_stock_bubble(
                    stock,
                    total_shown=total_shown,
                    hero_url=hero_urls.get(stock.stock_id),
                )
                for stock in chunk
            ],
        }
        size = len(json.dumps(contents, ensure_ascii=False).encode("utf-8"))
        if size > MAX_FLEX_BYTES:
            raise FlexBuildError(
                f"Flex message is {size} bytes, over LINE's {MAX_FLEX_BYTES} byte limit"
            )
        carousels.append(
            FlexCarousel(
                alt_text=alt_text,
                contents=contents,
                fingerprint=_fingerprint(
                    alt_text=alt_text, stocks=chunk, total_shown=total_shown
                ),
                stock_ids=tuple(stock.stock_id for stock in chunk),
            )
        )
    return carousels
