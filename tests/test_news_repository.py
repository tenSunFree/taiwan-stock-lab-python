import dataclasses
import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.news_models import ensure_news_tables
from app.db.news_repository import NewsRepository

from app.news.models import TAIPEI_TZ, ContentLevel, RawArticle

NOW = dt.datetime(2026, 10, 31, 20, 0, tzinfo=TAIPEI_TZ)


def art(title, *, stock="2352", when=None, original_url=None, official=False, aid=None):
    when = when or dt.datetime(2026, 10, 29, 10, 0, tzinfo=TAIPEI_TZ)
    return RawArticle(
        article_id=aid or title,
        market="MEDIA",
        stock_id=stock,
        title=title,
        description=None,
        content=None,
        source="s",
        published_at=when,
        fetched_at=NOW,
        source_url="https://src",
        original_url=original_url,
        is_official=official,
        content_level=ContentLevel.TITLE_ONLY,
    )


def test_repository_idempotent_roundtrip_keeps_none_original_url():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.db.news_models import ensure_news_tables
    from app.db.news_repository import NewsRepository

    engine = create_engine("sqlite://")
    ensure_news_tables(engine)
    ensure_news_tables(engine)  # checkfirst: second call is a no-op
    a, b = art("A", official=True), art("B", original_url="https://n/1")
    with Session(engine) as s:
        repo = NewsRepository(s)
        assert repo.save_new([a, a, b]) == 2
        assert repo.save_new([a, b]) == 0
        got = {
            x.article_id: x
            for x in repo.load_window(
                {"2352"}, start=dt.date(2026, 10, 29), end=dt.date(2026, 10, 29)
            )
        }
        assert got["A"].original_url is None and got["B"].original_url == "https://n/1"
        assert got["A"].published_at.tzinfo is not None


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    ensure_news_tables(engine)
    with Session(engine) as s:
        yield s


def test_repository_normalises_utc_to_taipei_before_storing(session):
    # 2026-10-29 16:30 UTC is 2026-10-30 00:30 in Taipei.
    utc_article = dataclasses.replace(
        art("utc"),
        published_at=dt.datetime(2026, 10, 29, 16, 30, tzinfo=dt.timezone.utc),
    )
    repo = NewsRepository(session)
    repo.save_new([utc_article])
    on_29 = repo.load_window(
        {"2352"}, start=dt.date(2026, 10, 29), end=dt.date(2026, 10, 29)
    )
    on_30 = repo.load_window(
        {"2352"}, start=dt.date(2026, 10, 30), end=dt.date(2026, 10, 30)
    )
    assert on_29 == []
    assert [a.article_id for a in on_30] == ["utc"]
    assert on_30[0].published_at == utc_article.published_at  # same instant


def test_repository_keeps_original_row_when_article_is_seen_again(session):
    first = art("A")
    later = dataclasses.replace(
        first, fetched_at=dt.datetime(2026, 11, 5, 9, 0, tzinfo=TAIPEI_TZ)
    )
    repo = NewsRepository(session)
    assert repo.save_new([first]) == 1
    assert repo.save_new([later]) == 0  # nothing inserted, nothing rewritten
    [stored] = repo.load_window(
        {"2352"}, start=dt.date(2026, 10, 29), end=dt.date(2026, 10, 29)
    )
    assert stored.fetched_at == first.fetched_at


def test_repository_counts_only_rows_actually_inserted(session):
    repo = NewsRepository(session)
    repo.save_new([art("old")])
    batch = [art("old"), art("new"), art("new")]  # one stored, one duplicated
    assert repo.save_new(batch) == 1


def test_repository_handles_empty_input_and_large_batches(session):
    repo = NewsRepository(session)
    assert repo.save_new([]) == 0
    many = [art(f"title-{i}") for i in range(1203)]  # spans several chunks
    assert repo.save_new(many) == 1203
    assert repo.save_new(many) == 0
