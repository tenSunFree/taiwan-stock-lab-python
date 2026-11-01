"""
Map TWSE t187ap04_L / TPEx mopsfin_t187ap04_O rows -> RawArticle.

Columns (per the open-data catalogue; app.jobs.news_spike confirms with real
data): 出表日期 發言日期 發言時間 公司代號 公司名稱 主旨 符合條款
事實發生日 說明.  Real headers can carry stray spaces/BOM ('主旨 '), so
keys are stripped.

Failure policy: a payload missing required columns RAISES (every row may
be misread); an individual bad row is DROPPED and counted.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from typing import Any

from app.ingestion.announcement_client import TPEX_URL, TWSE_URL
from app.news.models import TAIPEI_TZ, ContentLevel, RawArticle

SOURCE_NAME = "公開資訊觀測站"
SOURCE_URLS = {"TWSE": TWSE_URL, "TPEX": TPEX_URL}
REQUIRED_COLUMNS = ("發言日期", "發言時間", "公司代號", "公司名稱", "主旨")


class AnnouncementFormatError(RuntimeError):
    pass


@dataclass(frozen=True)
class MappingResult:
    articles: list[RawArticle]
    dropped: int  # rows skipped (bad date/time, no id/title, filtered out)
    stock_names: dict[str, str]  # stock_id -> 公司名稱, for title normalisation


def parse_date(text: str) -> dt.date | None:
    """ROC '1151030' / '115/10/30' -> date."""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) not in (6, 7):
        return None
    try:
        return dt.date(int(digits[:-4]) + 1911, int(digits[-4:-2]), int(digits[-2:]))
    except ValueError:
        return None


def parse_time(text: str) -> dt.time | None:
    """'154210' / '15:42:10' / '1542' -> time. Empty => None, NEVER 00:00:00
    (a fabricated midnight would turn a missing time into a fake 盤中)."""
    digits = re.sub(r"\D", "", text or "")
    if not digits:
        return None
    if len(digits) == 4:  # HHMM
        digits += "00"
    elif len(digits) == 5:  # HMMSS
        digits = "0" + digits
    if len(digits) != 6:
        return None
    try:
        return dt.time(int(digits[:2]), int(digits[2:4]), int(digits[4:6]))
    except ValueError:
        return None


def make_article_id(
    market: str, stock_id: str, published_at: dt.datetime, title: str
) -> str:
    raw = f"{market}|{stock_id}|{published_at.isoformat()}|{title.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean(row: dict[str, Any]) -> dict[str, str]:
    return {
        str(k).replace("\ufeff", "").strip(): ("" if v is None else str(v)).strip()
        for k, v in row.items()
    }


def build_announcement_articles(
    rows: Any,
    *,
    market: str,
    fetched_at: dt.datetime,
    allowed_stock_ids: set[str] | None = None,
) -> MappingResult:
    if market not in SOURCE_URLS:
        raise ValueError(f"market must be one of {sorted(SOURCE_URLS)}")
    if not isinstance(rows, list):
        raise AnnouncementFormatError("payload is not a list of rows")
    if not rows:
        return MappingResult([], 0, {})

    cleaned = [_clean(r) for r in rows if isinstance(r, dict)]
    if not cleaned:
        raise AnnouncementFormatError("no object rows in payload")
    missing = [c for c in REQUIRED_COLUMNS if c not in cleaned[0]]
    if missing:
        raise AnnouncementFormatError(
            f"missing columns {missing}; got {sorted(cleaned[0])}"
        )

    articles: list[RawArticle] = []
    names: dict[str, str] = {}
    dropped = len(rows) - len(cleaned)
    for row in cleaned:
        stock_id, title = row.get("公司代號", ""), row.get("主旨", "")
        if not stock_id or not title:
            dropped += 1
            continue
        names.setdefault(stock_id, row.get("公司名稱", ""))
        if allowed_stock_ids is not None and stock_id not in allowed_stock_ids:
            dropped += 1
            continue
        date, time = (
            parse_date(row.get("發言日期", "")),
            parse_time(row.get("發言時間", "")),
        )
        if date is None or time is None:
            dropped += 1
            continue
        published_at = dt.datetime.combine(date, time, tzinfo=TAIPEI_TZ)
        description = row.get("說明") or None
        articles.append(
            RawArticle(
                article_id=make_article_id(market, stock_id, published_at, title),
                market=market,
                stock_id=stock_id,
                title=title,
                description=description,
                content=None,
                source=SOURCE_NAME,
                published_at=published_at,
                fetched_at=fetched_at,
                source_url=SOURCE_URLS[market],
                original_url=None,  # open data has no per-announcement URL
                is_official=True,
                content_level=(
                    ContentLevel.TITLE_DESCRIPTION
                    if description
                    else ContentLevel.TITLE_ONLY
                ),
            )
        )
    return MappingResult(articles, dropped, names)
