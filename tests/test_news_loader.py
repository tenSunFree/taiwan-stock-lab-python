import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.news_models import ensure_news_tables
from app.db.news_repository import CollectionRunRepository, NewsRepository
from app.news.loader import load_news_snapshot
from app.news.models import TAIPEI_TZ, ContentLevel, RawArticle
from app.news.runs import CollectionRun
from app.news.service import NewsStatus

REPORT_DAY = dt.date(2026, 11, 18)  # Wednesday; data day is Tuesday 11/17
TITLE = "公告本公司115年第三季財務報告"  # MEDIUM


def weekdays(day):
    return day.weekday() < 5


def t(day, hour, minute=0, second=0):
    return dt.datetime(2026, 11, day, hour, minute, second, tzinfo=TAIPEI_TZ)


def article(*, stock="2330", market="TWSE", pub=None, fetched=None, title=TITLE):
    pub = pub or t(17, 10)
    return RawArticle(
        article_id=f"{market}-{stock}-{title}-{pub.isoformat()}",
        market=market,
        stock_id=stock,
        title=title,
        description="d",
        content=None,
        source="MOPS",
        published_at=pub,
        fetched_at=fetched or t(18, 9, 17),
        source_url="https://example.test",
        original_url=None,
        is_official=True,
        content_level=ContentLevel.TITLE_DESCRIPTION,
    )


def run_(market, start, *, secs=40, ok=True, run_id=None):
    return CollectionRun(
        run_id=run_id or f"r-{start:%d%H%M}",
        market=market,
        started_at=start,
        finished_at=start + dt.timedelta(seconds=secs),
        success=ok,
        error=None if ok else "X",
    )


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    ensure_news_tables(engine)
    with Session(engine) as s:
        yield s


def both(session, start, **kw):
    repo = CollectionRunRepository(session)
    for market in ("TWSE", "TPEX"):
        repo.record(run_(market, start, **kw))


def load(session, stocks=("2330",), **kw):
    return load_news_snapshot(
        session, stocks, report_date=REPORT_DAY, is_trading_day=weekdays, **kw
    )


def test_fresh_runs_give_a_normal_result(session):
    both(session, t(18, 15, 17))
    NewsRepository(session).save_new([article(fetched=t(18, 15, 17))])
    snap = load(session)
    assert snap.unavailable_reason is None
    assert snap.results["2330"].status is NewsStatus.HAS_EVENT


def test_no_runs_at_all_is_unavailable_for_every_stock(session):
    snap = load(session, ["2330", "2317"])
    assert {r.status for r in snap.results.values()} == {NewsStatus.DATA_UNAVAILABLE}
    assert set(snap.results) == {"2330", "2317"}
    assert "no successful TWSE" in snap.unavailable_reason


def test_one_market_without_a_run_makes_everything_unavailable(session):
    CollectionRunRepository(session).record(run_("TWSE", t(18, 15, 17)))
    snap = load(session)
    assert snap.results["2330"].status is NewsStatus.DATA_UNAVAILABLE
    assert "TPEX" in snap.unavailable_reason


def test_failed_runs_do_not_count(session):
    both(session, t(18, 15, 17), ok=False)
    assert load(session).results["2330"].status is NewsStatus.DATA_UNAVAILABLE


def test_a_stale_horizon_is_unavailable_not_no_event(session):
    # TWSE fine at 15:17, but TPEx's 15:17 run failed: its newest success is 09:17
    repo = CollectionRunRepository(session)
    repo.record(run_("TWSE", t(18, 15, 17)))
    repo.record(run_("TPEX", t(18, 9, 17)))
    repo.record(run_("TPEX", t(18, 15, 17), ok=False, run_id="bad"))
    snap = load(session)
    assert snap.results["2330"].status is NewsStatus.DATA_UNAVAILABLE
    assert "TPEX" in snap.unavailable_reason and "before" in snap.unavailable_reason


def test_rows_fetched_after_the_horizon_are_left_out_and_stay_out(session):
    both(session, t(18, 15, 17))
    straddle = t(18, 16, 59)  # starts before 17:00, records finish at 17:01:29
    repo = CollectionRunRepository(session)
    for market in ("TWSE", "TPEX"):
        repo.record(run_(market, straddle, secs=150, run_id="late"))
    NewsRepository(session).save_new([article(fetched=straddle)])
    first = load(session)
    assert first.results["2330"].status is NewsStatus.NO_SUITABLE_EVENT
    # a rebuild hours later, after more runs, gives the same answer
    both(session, t(18, 21, 17))
    assert load(session).results["2330"].status is NewsStatus.NO_SUITABLE_EVENT


def test_rows_of_a_run_with_no_record_join_the_next_cutoff_that_covers_them(session):
    # Tue 11/17: 09:17 ok, 15:17 committed rows but its record was never written
    both(session, t(17, 9, 17))
    NewsRepository(session).save_new([article(pub=t(14, 10), fetched=t(17, 15, 17))])
    # Tuesday's own report cannot use them (stale horizon is unavailable)
    tue = load_news_snapshot(
        session, ["2330"], report_date=dt.date(2026, 11, 17), is_trading_day=weekdays
    )
    assert tue.results["2330"].status is NewsStatus.DATA_UNAVAILABLE
    # Wednesday, after a later successful 15:17 run, admits them
    both(session, t(18, 15, 17))
    wed = load(session)
    assert wed.results["2330"].status is NewsStatus.HAS_EVENT


def test_notice_published_on_the_report_day_is_never_used(session):
    both(session, t(18, 15, 17))
    NewsRepository(session).save_new([article(pub=t(18, 10), fetched=t(18, 11))])
    assert load(session).results["2330"].status is NewsStatus.NO_SUITABLE_EVENT


def test_no_stocks_gives_an_empty_snapshot_without_looking_at_runs(session):
    snap = load(session, [])  # no runs stored at all
    assert snap.results == {} and snap.unavailable_reason is None


def test_arguments_are_validated_before_availability_is_judged(session):
    with pytest.raises(ValueError):
        load(session, ["2330"], base_days=5, max_days=3)  # no runs stored
    with pytest.raises(ValueError):
        load(session, ["2330"], max_days=999)


def test_a_failed_twse_afternoon_run_does_not_hide_the_report(session):
    # TWSE notices arrive in the 09:17 run, so only TPEx needs the 15:17 one
    repo = CollectionRunRepository(session)
    repo.record(run_("TWSE", t(18, 9, 17)))
    repo.record(run_("TWSE", t(18, 15, 17), ok=False, run_id="bad"))
    repo.record(run_("TPEX", t(18, 15, 17)))
    NewsRepository(session).save_new([article(fetched=t(18, 9, 17))])
    assert load(session).results["2330"].status is NewsStatus.HAS_EVENT


def test_floors_can_be_overridden_per_market(session):
    repo = CollectionRunRepository(session)
    repo.record(run_("TWSE", t(18, 9, 17)))
    repo.record(run_("TPEX", t(18, 9, 17)))
    assert load(session).results["2330"].status is NewsStatus.DATA_UNAVAILABLE
    relaxed = {"TWSE": dt.time(9, 0), "TPEX": dt.time(9, 0)}
    got = load(session, min_run_start=relaxed)
    assert got.results["2330"].status is NewsStatus.NO_SUITABLE_EVENT


def test_other_markets_are_ignored(session):
    both(session, t(18, 15, 17))
    NewsRepository(session).save_new([article(market="MEDIA")])
    assert load(session).results["2330"].status is NewsStatus.NO_SUITABLE_EVENT
