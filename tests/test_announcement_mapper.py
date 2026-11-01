import datetime as dt

import pytest

from app.ingestion.announcement_mapper import (
    AnnouncementFormatError,
    build_announcement_articles,
    parse_date,
    parse_time,
)
from app.news.models import TAIPEI_TZ, ContentLevel

NOW = dt.datetime(2026, 10, 31, 20, 0, tzinfo=TAIPEI_TZ)


def make_row(**kw):
    row = {
        "出表日期": "1151031",
        "發言日期": "1151030",
        "發言時間": "154210",
        "公司代號": "2352",
        "公司名稱": "佳世達",
        "主旨 ": "公告本公司董事會決議配息",
        "符合條款": "第51款",
        "事實發生日": "1151030",
        "說明": "董事會通過",
    }
    row.update(kw)
    return row


def test_parse_time_empty_is_none_not_midnight():
    assert parse_time("") is None
    assert parse_time("   ") is None
    assert parse_time("154210") == dt.time(15, 42, 10)
    assert parse_time("15:42:10") == dt.time(15, 42, 10)
    assert parse_time("1542") == dt.time(15, 42, 0)
    assert parse_time("93005") == dt.time(9, 30, 5)
    assert parse_time("991399") is None


def test_parse_date_roc():
    assert parse_date("1151030") == dt.date(2026, 10, 30)
    assert parse_date("115/10/30") == dt.date(2026, 10, 30)
    assert parse_date("") is None and parse_date("1151330") is None


def test_mapper_basic_fields():
    res = build_announcement_articles([make_row()], market="TWSE", fetched_at=NOW)
    [a] = res.articles
    assert a.published_at == dt.datetime(2026, 10, 30, 15, 42, 10, tzinfo=TAIPEI_TZ)
    assert a.is_after_hours and a.is_official and a.market == "TWSE"
    assert a.original_url is None and a.source_url.endswith("t187ap04_L")
    assert a.link == ("查看官方來源 ↗", a.source_url)
    assert a.fetched_at == NOW and len(a.article_id) == 64
    assert res.stock_names == {"2352": "佳世達"}


def test_mapper_tpex_source_url():
    [a] = build_announcement_articles(
        [make_row()], market="TPEX", fetched_at=NOW
    ).articles
    assert a.source_url.endswith("mopsfin_t187ap04_O")


def test_mapper_same_title_different_market_different_id():
    a = build_announcement_articles(
        [make_row()], market="TWSE", fetched_at=NOW
    ).articles[0]
    b = build_announcement_articles(
        [make_row()], market="TPEX", fetched_at=NOW
    ).articles[0]
    assert a.article_id != b.article_id


def test_mapper_title_only_without_description():
    [a] = build_announcement_articles(
        [make_row(說明="")], market="TWSE", fetched_at=NOW
    ).articles
    assert a.content_level == ContentLevel.TITLE_ONLY


def test_mapper_missing_column_raises():
    bad = make_row()
    del bad["發言時間"]
    with pytest.raises(AnnouncementFormatError):
        build_announcement_articles([bad], market="TWSE", fetched_at=NOW)


def test_mapper_drops_and_counts_bad_rows():
    rows = [
        make_row(發言時間=""),
        make_row(發言日期="x"),
        make_row(公司代號="9999"),
        make_row(),
    ]
    res = build_announcement_articles(
        rows, market="TWSE", fetched_at=NOW, allowed_stock_ids={"2352"}
    )
    assert len(res.articles) == 1 and res.dropped == 3


def test_mapper_non_list_and_empty():
    with pytest.raises(AnnouncementFormatError):
        build_announcement_articles({"x": 1}, market="TWSE", fetched_at=NOW)
    assert build_announcement_articles([], market="TWSE", fetched_at=NOW).articles == []


def test_mapper_rejects_unknown_market():
    with pytest.raises(ValueError):
        build_announcement_articles([make_row()], market="NYSE", fetched_at=NOW)
