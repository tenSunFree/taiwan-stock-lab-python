"""
News Event Module V1 — domain models. Pure dataclasses, no I/O.

Rules carried from the spec:
  * URLs ALWAYS come from the data source, never from an LLM.
  * published_at (never fetched_at) decides 盤中 / 盤後.
  * News is "近期焦點", never a causal explanation of a limit-up.

original_url vs source_url:
  TWSE/TPEx 每日重大訊息 open data has NO per-announcement URL, so we do
  not fake one.  original_url = the real single-article URL (media news
  only; None for official open data).  source_url = where the data came
  from (always present).  LINE shows 「查看原文 ↗」 for original_url and
  「查看官方來源 ↗」 for source_url only.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum

TAIPEI_TZ = dt.timezone(dt.timedelta(hours=8))
MARKET_CLOSE = dt.time(13, 30)  # published at/after this => 盤後

ALLOWED_CATEGORIES = frozenset({"公告", "財報", "法說", "產業", "法人"})


class ContentLevel(str, Enum):
    FULL_TEXT = "FULL_TEXT"  # title + full article
    TITLE_DESCRIPTION = "TITLE_DESCRIPTION"
    TITLE_ONLY = "TITLE_ONLY"  # never AI-summarised


class SummaryType(str, Enum):
    AI_SUMMARY = "AI_SUMMARY"
    ORIGINAL_TITLE = "ORIGINAL_TITLE"


def is_after_hours(published_at: dt.datetime) -> bool:
    return published_at.astimezone(TAIPEI_TZ).time() >= MARKET_CLOSE


@dataclass(frozen=True)
class RawArticle:
    article_id: str  # sha256(market|stock|published_at|title), see mapper
    market: str  # "TWSE" | "TPEX" | "MEDIA"
    stock_id: str
    title: str
    description: str | None
    content: str | None
    source: str  # display name, e.g. 公開資訊觀測站
    published_at: dt.datetime  # tz-aware
    fetched_at: dt.datetime  # tz-aware; never used for 盤中/盤後
    source_url: str  # data-source URL, always present
    original_url: str | None  # single-article URL, may be None
    is_official: bool
    content_level: ContentLevel

    def __post_init__(self) -> None:
        if self.published_at.tzinfo is None or self.fetched_at.tzinfo is None:
            raise ValueError("published_at / fetched_at must be timezone-aware")
        if not self.source_url:
            raise ValueError("source_url is required")
        if self.original_url == "":
            raise ValueError("original_url must be None or a real URL, not ''")

    @property
    def is_after_hours(self) -> bool:
        return is_after_hours(self.published_at)

    @property
    def link(self) -> tuple[str, str]:
        """(LINE label, url). Never pretends a source page is the article."""
        if self.original_url:
            return "查看原文 ↗", self.original_url
        # Only official open data may be labelled as the official source;
        # a media article that merely lacks a URL gets a neutral label.
        label = "查看官方來源 ↗" if self.is_official else "查看來源 ↗"
        return label, self.source_url


@dataclass(frozen=True)
class RankingBreakdown:
    event_importance: float
    direct_company_bonus: float
    official_source_bonus: float
    recency_bonus: float

    @property
    def total(self) -> float:
        return (
            self.event_importance
            + self.direct_company_bonus
            + self.official_source_bonus
            + self.recency_bonus
        )


@dataclass(frozen=True)
class StockEvent:
    event_id: str  # Python/DB UUID, never from the LLM
    stock_id: str
    category: str
    summary: str
    summary_type: SummaryType
    published_at: dt.datetime
    relevance_score: float
    ranking_breakdown: RankingBreakdown | None
    articles: tuple[RawArticle, ...]

    def __post_init__(self) -> None:
        if not self.articles:
            raise ValueError("StockEvent requires at least one article")

    @property
    def ranking_score(self) -> float:
        return 0.0 if self.ranking_breakdown is None else self.ranking_breakdown.total

    @property
    def primary_article(self) -> RawArticle:
        """Official first, then earliest — LINE V1 shows one source."""
        return min(self.articles, key=lambda a: (not a.is_official, a.published_at))
