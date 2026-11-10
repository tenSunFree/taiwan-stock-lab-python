import datetime as dt
from dataclasses import replace

from app.news.models import TAIPEI_TZ, ContentLevel, RawArticle
from app.news.preprocess import (
    canonical_url,
    cluster_candidates,
    collapse_repeats,
    dedup_exact,
    filter_window,
    normalize_title,
    trading_days_window_start,
)

NOW = dt.datetime(2026, 11, 10, 20, 0, tzinfo=TAIPEI_TZ)


def art(title, *, stock="2352", when=None, original_url=None, official=False, aid=None):
    when = when or dt.datetime(2026, 10, 29, 10, 0, tzinfo=TAIPEI_TZ)
    return RawArticle(
        article_id=aid or title,
        market="TWSE" if official else "MEDIA",
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


def test_dedup_media_url_is_scoped_to_each_stock():
    url = "https://news.com/shared-article"
    for_2352 = art("A", aid="1", stock="2352", original_url=url)
    for_2330 = art("A", aid="2", stock="2330", original_url=url)
    again_2352 = art("A copy", aid="3", stock="2352", original_url=url)
    kept = dedup_exact([for_2352, for_2330, again_2352])
    assert sorted((a.stock_id, a.article_id) for a in kept) == [
        ("2330", "2"),
        ("2352", "1"),
    ]


def _official(title, description, day, *, stock="2072", hour=7):
    base = art(
        title,
        stock=stock,
        when=dt.datetime(2026, 11, day, hour, 0, 3, tzinfo=TAIPEI_TZ),
        aid=f"{stock}-{title}-{day}-{hour}",
        official=True,
    )
    return replace(
        base,
        description=description,
        content_level=(
            ContentLevel.TITLE_DESCRIPTION if description else ContentLevel.TITLE_ONLY
        ),
    )


RENAME = "公告本公司名稱由「甲」更名為「乙」，公告期間：115年8月26日至115年11月25日。"
RENAME_DESC = (
    "1.事實發生日：民國115年08月24日\n7.其他應敘明事項：更名後須連續公告三個月。"
)


def test_collapse_merges_daily_republished_notice():
    rows = [_official(RENAME, RENAME_DESC, d) for d in (3, 4, 5, 6)]
    (only,) = collapse_repeats(rows)
    assert only.occurrences == 4
    assert only.article is rows[0]
    assert only.first_published_at.day == 3 and only.last_published_at.day == 6
    assert only.article_ids == tuple(r.article_id for r in rows)


def test_collapse_ignores_whitespace_and_fullwidth_in_description():
    a = _official(RENAME, "事實發生日：115年08月24日\n  連續公告", 3)
    b = _official(RENAME, "事實發生日：115年08月24日 連續公告", 4)
    assert len(collapse_repeats([a, b])) == 1


def test_collapse_keeps_same_title_with_different_description():
    # same title, different trades: must stay separate events
    a = _official("公告本公司處分有價證券", "1.交易標的:甲 2.金額:100", 3)
    b = _official("公告本公司處分有價證券", "1.交易標的:乙 2.金額:900", 4)
    assert len(collapse_repeats([a, b])) == 2


def test_collapse_never_merges_without_description():
    a = _official("公告本公司董事會重要決議事項", None, 3)
    b = _official("公告本公司董事會重要決議事項", "  ", 4)
    c = _official("公告本公司董事會重要決議事項", "", 5)
    assert len(collapse_repeats([a, b, c])) == 3


def test_collapse_is_scoped_to_each_stock():
    a = _official(RENAME, RENAME_DESC, 3, stock="2072")
    b = _official(RENAME, RENAME_DESC, 3, stock="3489")
    assert len(collapse_repeats([a, b])) == 2


def test_collapse_orders_by_first_seen_and_ignores_input_order():
    other = _official("公告本公司總經理異動", "1.發生變動日期:115/11/04", 4, hour=15)
    rows = [_official(RENAME, RENAME_DESC, d) for d in (5, 3, 4)]
    result = collapse_repeats([other, *rows])
    assert [r.first_published_at.day for r in result] == [3, 4]
    assert result[0].occurrences == 3 and result[1].occurrences == 1


def test_collapse_empty_input():
    assert collapse_repeats([]) == []


def test_collapse_does_not_double_count_identical_article_ids():
    a = _official(RENAME, RENAME_DESC, 3)
    b = _official(RENAME, RENAME_DESC, 4)
    (notice,) = collapse_repeats([b, a, b, a])
    assert notice.occurrences == 2
    assert notice.article_ids == (a.article_id, b.article_id)


def test_collapse_official_only_media_with_same_title_and_desc_stays_separate():
    a = replace(art(RENAME, aid="a"), description=RENAME_DESC)
    b = replace(art(RENAME, aid="b"), description=RENAME_DESC)
    assert len(collapse_repeats([a, b])) == 2


def test_collapse_requires_both_official_flag_and_supported_market():
    genuine = _official(RENAME, RENAME_DESC, 3)
    not_official = replace(_official(RENAME, RENAME_DESC, 4), is_official=False)
    not_supported_market = replace(_official(RENAME, RENAME_DESC, 5), market="MEDIA")
    result = collapse_repeats([genuine, not_official, not_supported_market])
    assert len(result) == 3


def test_collapse_keeps_distinct_punctuation_notices_apart():
    a = _official("公告本公司A-B交易", "說明一致", 3)
    b = _official("公告本公司AB交易", "說明一致", 4)
    assert len(collapse_repeats([a, b])) == 2


def test_collapse_normalizes_fullwidth_and_whitespace():
    a = _official("公告本公司ＡＢ更名", "1.事實發生日：115/11/03\n2. 原因： 改名", 3)
    b = _official("公告本公司AB更名", "1.事實發生日:115/11/03 2. 原因: 改名", 4)
    (notice,) = collapse_repeats([a, b])
    assert notice.occurrences == 2


def test_collapse_distinguishes_markets():
    a = _official(RENAME, RENAME_DESC, 3)
    b = replace(_official(RENAME, RENAME_DESC, 4), market="TPEX")
    assert len(collapse_repeats([a, b])) == 2


def test_collapse_accepts_generator_and_preserves_original_article():
    a = _official(RENAME, RENAME_DESC, 3)
    b = _official(RENAME, RENAME_DESC, 4)
    (notice,) = collapse_repeats(a for a in [b, a])
    assert notice.article is a
    assert notice.first_published_at == a.published_at
    assert notice.last_published_at == b.published_at


def test_repeated_notice_first_date_is_not_a_new_event_when_later_republished():
    a = _official(RENAME, RENAME_DESC, 3)
    b = _official(RENAME, RENAME_DESC, 8)
    (notice,) = collapse_repeats([b, a])
    assert notice.article.published_at == a.published_at
    assert notice.last_published_at == b.published_at
