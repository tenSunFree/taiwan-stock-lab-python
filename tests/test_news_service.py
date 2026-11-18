import datetime as dt
import random

import pytest

from app.news.models import TAIPEI_TZ, ContentLevel, RawArticle
from app.news.select import Tier
from app.news.service import (
    NewsStatus,
    history_start,
    previous_trading_day,
    select_news,
    snapshot_fetch_cutoff,
)

REPORT = "公告本公司115年第三季財務報告"  # MEDIUM
CONTRACT = "公告本公司簽訂重大契約"  # HIGH
PERSONNEL = "公告本公司研發主管異動"  # LOW
ATTENTION = "公告本公司股票列為注意交易資訊"


def weekdays(day: dt.date) -> bool:
    return day.weekday() < 5


def at(day, hour=15, minute=0):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=TAIPEI_TZ)


def art(
    title,
    *,
    stock="2330",
    pub=None,
    fetched=None,
    desc="d",
    official=True,
    market="TWSE",
):
    pub = pub or at(17)
    fetched = fetched or pub + dt.timedelta(days=1)
    return RawArticle(
        article_id=f"{stock}-{title}-{pub.isoformat()}",
        market=market,
        stock_id=stock,
        title=title,
        description=desc,
        content=None,
        source="MOPS",
        published_at=pub,
        fetched_at=fetched.replace(hour=9, minute=17),
        source_url="https://example.test",
        original_url=None,
        is_official=official,
        content_level=ContentLevel.TITLE_DESCRIPTION,
    )


# Wed 2026-11-18 is the report day; its last data day is Tue 11/17.
REPORT_DAY = dt.date(2026, 11, 18)


def run(articles, stocks=("2330",), **kw):
    kw.setdefault("report_date", REPORT_DAY)
    kw.setdefault("is_trading_day", weekdays)
    return select_news(articles, list(stocks), **kw)


def test_event_inside_three_day_window_is_selected():
    snap = run([art(REPORT, pub=at(17))])
    r = snap.results["2330"]
    assert r.status is NewsStatus.HAS_EVENT and r.window_trading_days == 3
    assert r.candidate.notice.article.title == REPORT


def test_window_counts_trading_days_before_the_report_day():
    # last data day Tue 11/17 -> 3 trading days = Tue, Mon, Fri 11/13
    inside = run([art(REPORT, pub=at(13))]).results["2330"]
    outside = run([art(REPORT, pub=at(12))]).results["2330"]
    assert inside.window_trading_days == 3
    assert outside.window_trading_days == 5  # widened, still found


def test_widens_to_five_days_only_for_stocks_without_an_event():
    arts = [
        art(REPORT, stock="2330", pub=at(17)),
        art(REPORT, stock="2317", pub=at(11)),
    ]
    snap = run(arts, stocks=("2330", "2317"))
    assert snap.results["2330"].window_trading_days == 3
    assert snap.results["2317"].window_trading_days == 5


def test_nothing_within_five_days_is_no_suitable_event():
    snap = run([art(REPORT, pub=at(5))])
    r = snap.results["2330"]
    assert r.status is NewsStatus.NO_SUITABLE_EVENT and r.candidate is None


def test_low_tier_notice_is_not_a_suitable_event_by_default():
    snap = run([art(PERSONNEL, pub=at(17))])
    assert snap.results["2330"].status is NewsStatus.NO_SUITABLE_EVENT
    allowed = run([art(PERSONNEL, pub=at(17))], min_tier=Tier.LOW)
    assert allowed.results["2330"].status is NewsStatus.HAS_EVENT


def test_attention_notice_never_counts():
    snap = run([art(ATTENTION, pub=at(17))], min_tier=Tier.LOW)
    assert snap.results["2330"].status is NewsStatus.NO_SUITABLE_EVENT


def test_higher_tier_wins_over_newer_lower_tier_in_the_window():
    snap = run([art(CONTRACT, pub=at(16)), art(REPORT, pub=at(17))])
    assert snap.results["2330"].candidate.tier is Tier.HIGH


def test_weekend_announcements_are_inside_a_monday_report_window():
    # Mon 11/16 report: last data day Fri 11/13; window Wed..Fri, plus the
    # weekend published after it is inside [start, ...] too.
    sat = art(REPORT, pub=at(14, 10))
    snap = run([sat], report_date=dt.date(2026, 11, 16))
    assert snap.results["2330"].status is NewsStatus.HAS_EVENT


def test_a_holiday_does_not_count_as_a_trading_day():
    holiday = dt.date(2026, 11, 16)
    cal = lambda d: d.weekday() < 5 and d != holiday  # noqa: E731
    assert previous_trading_day(dt.date(2026, 11, 17), cal) == dt.date(2026, 11, 13)


def test_rows_fetched_after_the_cutoff_are_ignored_and_result_is_stable():
    early = art(REPORT, pub=at(17))
    late_fetch = art(CONTRACT, pub=at(17), fetched=at(18))
    late_fetch = RawArticle(**{**late_fetch.__dict__, "fetched_at": at(18, 21, 17)})
    base = run([early])
    with_late = run([early, late_fetch])
    assert with_late.results == base.results
    assert with_late.fetched_before == snapshot_fetch_cutoff(REPORT_DAY)


def test_cutoff_is_seventeen_taipei_on_the_report_day():
    assert snapshot_fetch_cutoff(REPORT_DAY) == dt.datetime(
        2026, 11, 18, 17, 0, tzinfo=TAIPEI_TZ
    )


def test_result_is_independent_of_input_order():
    arts = [art(REPORT, stock=str(2000 + i), pub=at(11 + i % 6)) for i in range(12)]
    stocks = [a.stock_id for a in arts]
    one = run(arts, stocks=stocks).results
    shuffled = arts[:]
    random.Random(1).shuffle(shuffled)
    assert run(shuffled, stocks=stocks).results == one


def test_republished_old_notice_is_not_news_when_history_is_supplied():
    rename = [
        art("公告本公司更名", pub=at(d), desc="same")
        for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17)
    ]
    snap = run(rename, min_tier=Tier.LOW)
    assert snap.results["2330"].status is NewsStatus.NO_SUITABLE_EVENT


def test_without_history_the_same_republication_looks_new():
    # documents the limit: only the window was loaded
    in_window = [art("公告本公司更名", pub=at(d), desc="same") for d in (13, 16, 17)]
    snap = run(in_window, min_tier=Tier.LOW)
    assert snap.results["2330"].status is NewsStatus.HAS_EVENT


def test_history_start_reaches_back_before_the_widest_window():
    start = history_start(REPORT_DAY, weekdays, max_days=5, history_days=90)
    # widest window starts Wed 11/11, minus 90 days
    assert start == dt.date(2026, 11, 11) - dt.timedelta(days=90)


def test_other_stocks_and_media_are_ignored():
    arts = [
        art(REPORT, stock="9999", pub=at(17)),
        art(REPORT, stock="2330", pub=at(17), official=False, market="MEDIA"),
    ]
    snap = run(arts)
    assert set(snap.results) == {"2330"}
    assert snap.results["2330"].status is NewsStatus.NO_SUITABLE_EVENT


def test_latest_published_date_is_reported():
    snap = run(
        [art(REPORT, pub=at(13)), art(REPORT, stock="2317", pub=at(17))],
        stocks=("2330", "2317"),
    )
    assert snap.latest_published_date == dt.date(2026, 11, 17)
    assert run([]).latest_published_date is None


def test_every_requested_stock_has_a_result():
    snap = run([], stocks=("2330", "2317", "2454"))
    assert set(snap.results) == {"2330", "2317", "2454"}
    assert all(r.status is NewsStatus.NO_SUITABLE_EVENT for r in snap.results.values())


def test_invalid_arguments_are_rejected():
    with pytest.raises(ValueError):
        run([], base_days=0)
    with pytest.raises(ValueError):
        run([], base_days=5, max_days=3)
    with pytest.raises(ValueError):
        run([], fetched_before=dt.datetime(2026, 11, 18, 17, 0))
