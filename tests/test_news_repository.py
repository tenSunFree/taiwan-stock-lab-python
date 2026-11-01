import datetime as dt

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
