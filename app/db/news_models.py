"""
News tables (V1). Project bootstrap convention: ensure_*_table with
checkfirst=True (no Alembic yet). No JSONB so tests run on SQLite.
Currently only raw_articles; event and snapshot tables are added with
the event-ranking work.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class RawArticleRow(Base):
    __tablename__ = "raw_articles"

    article_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    stock_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    original_url: Mapped[str | None] = mapped_column(Text)
    is_official: Mapped[bool] = mapped_column(Boolean, nullable=False)
    content_level: Mapped[str] = mapped_column(String(24), nullable=False)


def ensure_news_tables(engine) -> None:
    RawArticleRow.__table__.create(engine, checkfirst=True)
