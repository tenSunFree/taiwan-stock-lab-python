import datetime as dt

import pytest

from app.news.models import (
    TAIPEI_TZ,
    ContentLevel,
    MarketSession,
    RankingBreakdown,
    RawArticle,
    StockEvent,
    SummaryType,
    is_after_hours,
    market_session,
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


def test_market_session_boundaries():
    d = dt.datetime(2026, 10, 30, tzinfo=TAIPEI_TZ)
    cases = [
        ((8, 59), MarketSession.PRE_MARKET),
        ((9, 0), MarketSession.INTRADAY),
        ((13, 29), MarketSession.INTRADAY),
        ((13, 30), MarketSession.AFTER_HOURS),
    ]
    for (h, m), expected in cases:
        assert market_session(d.replace(hour=h, minute=m)) == expected
    assert [s.value for s in MarketSession] == ["盤前", "盤中", "盤後"]


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


def test_link_label_is_official_only_for_official_articles():
    official = art("o", official=True)
    media_without_url = art("m", official=False)
    assert official.link[0] == "查看官方來源 ↗"
    assert media_without_url.link == ("查看來源 ↗", "https://src")


def _event(*articles):
    return StockEvent(
        event_id="e1",
        stock_id="2352",
        category="公告",
        summary="s",
        summary_type=SummaryType.ORIGINAL_TITLE,
        published_at=NOW,
        relevance_score=1.0,
        ranking_breakdown=None,
        articles=tuple(articles),
    )


def test_stock_event_requires_at_least_one_article():
    with pytest.raises(ValueError):
        _event()


def test_primary_article_prefers_official_then_earliest():
    early_media = art("m", when=dt.datetime(2026, 10, 28, 9, 0, tzinfo=TAIPEI_TZ))
    late_official = art(
        "o",
        official=True,
        when=dt.datetime(2026, 10, 29, 9, 0, tzinfo=TAIPEI_TZ),
    )
    assert _event(early_media, late_official).primary_article is late_official
