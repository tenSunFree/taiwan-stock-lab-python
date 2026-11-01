import datetime as dt

import pytest

from app.news.models import (
    TAIPEI_TZ,
    ContentLevel,
    RankingBreakdown,
    RawArticle,
    is_after_hours,
)

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


def test_after_hours_boundary():
    d = dt.datetime(2026, 10, 30, tzinfo=TAIPEI_TZ)
    assert not is_after_hours(d.replace(hour=13, minute=29))
    assert is_after_hours(d.replace(hour=13, minute=30))


def test_model_validation():
    with pytest.raises(ValueError):
        RawArticle(
            "i",
            "MEDIA",
            "2352",
            "t",
            None,
            None,
            "s",
            dt.datetime(2026, 1, 1),
            NOW,
            "https://s",
            None,
            False,
            ContentLevel.TITLE_ONLY,
        )
    with pytest.raises(ValueError):
        art("t", original_url="")
    a = art("t", original_url="https://news/1")
    assert a.link == ("查看原文 ↗", "https://news/1")


def test_ranking_breakdown_total():
    assert RankingBreakdown(30, 20, 10, 15).total == 75
