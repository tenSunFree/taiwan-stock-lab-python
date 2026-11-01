import datetime as dt

from app.news.models import TAIPEI_TZ, ContentLevel, RawArticle
from app.news.preprocess import (
    canonical_url,
    cluster_candidates,
    dedup_exact,
    filter_window,
    normalize_title,
    trading_days_window_start,
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


def test_trading_day_window_skips_weekend():
    is_td = lambda d: d.weekday() < 5
    assert trading_days_window_start(dt.date(2026, 11, 2), 3, is_td) == dt.date(
        2026, 10, 29
    )


def test_filter_window_uses_published_at():
    old = art("old", when=dt.datetime(2026, 10, 20, 9, 0, tzinfo=TAIPEI_TZ))
    new = art("new")
    assert filter_window(
        [old, new], start=dt.date(2026, 10, 27), end=dt.date(2026, 10, 30)
    ) == [new]


def test_dedup_official_by_id_never_by_shared_source_url():
    a, b = art("董事會決議配息", official=True), art("法說會通知", official=True)
    assert len(dedup_exact([a, b])) == 2  # same source_url, different ids
    assert len(dedup_exact([a, a])) == 1


def test_dedup_media_by_canonical_url():
    a = art("A", aid="1", original_url="https://News.com/x/?utm_source=line#top")
    b = art("B", aid="2", original_url="https://news.com/x")
    assert canonical_url(a.original_url) == canonical_url(b.original_url)
    assert len(dedup_exact([a, b])) == 1


def test_normalize_strips_name_id_punct():
    assert (
        normalize_title(
            "佳世達(2352)：Q3 獲利成長!", stock_id="2352", stock_name="佳世達"
        )
        == "q3獲利成長"
    )


def test_cluster_groups_similar_same_stock_only():
    a, b = art("佳世達Q3獲利成長"), art("佳世達第三季財報出爐 Q3獲利成長")
    c, other = art("佳世達宣布新董事"), art("佳世達Q3獲利成長", stock="2330")
    groups = cluster_candidates(
        [a, b, c, other], stock_names={"2352": "佳世達", "2330": "台積電"}
    )
    assert sorted(len(g) for g in groups) == [1, 1, 2]
