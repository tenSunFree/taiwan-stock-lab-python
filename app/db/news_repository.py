"""Idempotent raw-article persistence. Re-fetching never duplicates a row
and never rewrites the ORIGINAL fetched_at.

Inserts use INSERT ... ON CONFLICT DO NOTHING (PostgreSQL in production,
SQLite in tests) so two concurrent writers saving the same article_id both
succeed and only one row is created. The database's primary key is the
single source of truth for "already stored", not a prior SELECT.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db.news_models import RawArticleRow
from app.news.models import TAIPEI_TZ, ContentLevel, RawArticle

# 13 columns per row; keeps one statement far below PostgreSQL's
# 65,535 bind-parameter limit.
_INSERT_CHUNK_SIZE = 500


def _aware(d: dt.datetime) -> dt.datetime:  # SQLite drops tzinfo
    return d if d.tzinfo else d.replace(tzinfo=TAIPEI_TZ)


def _row_values(a: RawArticle) -> dict:
    # Normalise to Taipei BEFORE writing. PostgreSQL timestamptz keeps the
    # instant either way, but SQLite stores only the clock time, and
    # _aware() reads it back as Taipei, so a UTC value written as-is would
    # silently shift by 8 hours and land on the wrong Taipei date.
    return {
        "article_id": a.article_id,
        "market": a.market,
        "stock_id": a.stock_id,
        "title": a.title,
        "description": a.description,
        "content": a.content,
        "source": a.source,
        "published_at": a.published_at.astimezone(TAIPEI_TZ),
        "fetched_at": a.fetched_at.astimezone(TAIPEI_TZ),
        "source_url": a.source_url,
        "original_url": a.original_url,
        "is_official": a.is_official,
        "content_level": a.content_level.value,
    }


class NewsRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _insert(self):
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            return postgresql_insert
        if dialect == "sqlite":
            return sqlite_insert
        raise NotImplementedError(
            f"NewsRepository supports PostgreSQL and SQLite, not {dialect!r}"
        )

    def save_new(self, articles: list[RawArticle]) -> int:
        """Insert articles whose article_id is not stored yet; return how
        many rows were actually inserted. An already-stored article is left
        completely unchanged (its original fetched_at is preserved)."""
        # First occurrence wins within the batch itself.
        unique: dict[str, RawArticle] = {}
        for a in articles:
            unique.setdefault(a.article_id, a)
        if not unique:
            return 0

        insert = self._insert()
        values = [_row_values(a) for a in unique.values()]
        inserted = 0
        for start in range(0, len(values), _INSERT_CHUNK_SIZE):
            chunk = values[start : start + _INSERT_CHUNK_SIZE]
            stmt = (
                insert(RawArticleRow)
                .values(chunk)
                .on_conflict_do_nothing(index_elements=["article_id"])
                .returning(RawArticleRow.article_id)
            )
            inserted += len(self.session.execute(stmt).scalars().all())
        self.session.commit()
        return inserted

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
