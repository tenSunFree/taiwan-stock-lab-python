"""Idempotent raw-article persistence. Re-fetching never duplicates a row
and never rewrites the ORIGINAL fetched_at."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.news_models import RawArticleRow
from app.news.models import TAIPEI_TZ, ContentLevel, RawArticle


def _aware(d: dt.datetime) -> dt.datetime:  # SQLite drops tzinfo
    return d if d.tzinfo else d.replace(tzinfo=TAIPEI_TZ)


class NewsRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_new(self, articles: list[RawArticle]) -> int:
        """Insert unseen article_ids only; return the number inserted."""
        if not articles:
            return 0
        ids = list({a.article_id for a in articles})
        existing = set(
            self.session.scalars(
                select(RawArticleRow.article_id).where(
                    RawArticleRow.article_id.in_(ids)
                )
            )
        )
        new = 0
        for a in articles:
            if a.article_id in existing:
                continue
            existing.add(a.article_id)
            self.session.add(
                RawArticleRow(
                    article_id=a.article_id,
                    market=a.market,
                    stock_id=a.stock_id,
                    title=a.title,
                    description=a.description,
                    content=a.content,
                    source=a.source,
                    published_at=a.published_at,
                    fetched_at=a.fetched_at,
                    source_url=a.source_url,
                    original_url=a.original_url,
                    is_official=a.is_official,
                    content_level=a.content_level.value,
                )
            )
            new += 1
        self.session.commit()
        return new

    def load_window(
        self, stock_ids: set[str], *, start: dt.date, end: dt.date
    ) -> list[RawArticle]:
        lo = dt.datetime.combine(start, dt.time.min, tzinfo=TAIPEI_TZ)
        hi = dt.datetime.combine(
            end + dt.timedelta(days=1), dt.time.min, tzinfo=TAIPEI_TZ
        )
        rows = self.session.scalars(
            select(RawArticleRow).where(
                RawArticleRow.stock_id.in_(stock_ids),
                RawArticleRow.published_at >= lo,
                RawArticleRow.published_at < hi,
            )
        )
        out = [
            RawArticle(
                article_id=r.article_id,
                market=r.market,
                stock_id=r.stock_id,
                title=r.title,
                description=r.description,
                content=r.content,
                source=r.source,
                published_at=_aware(r.published_at),
                fetched_at=_aware(r.fetched_at),
                source_url=r.source_url,
                original_url=r.original_url,
                is_official=r.is_official,
                content_level=ContentLevel(r.content_level),
            )
            for r in rows
        ]
        return sorted(out, key=lambda a: a.published_at)
