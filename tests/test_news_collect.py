import datetime as dt
import logging

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.news_models import RawArticleRow
from app.ingestion.announcement_client import AnnouncementClient
from app.jobs.news_collect import MemoryRepository, run
from app.news.models import TAIPEI_TZ

NOW = dt.datetime(2026, 11, 3, 9, 0, tzinfo=TAIPEI_TZ)

TWSE_ROW = {
    "出表日期": "1151103",
    "發言日期": "1151102",
    "發言時間": "133806",
    "公司代號": "2352",
    "公司名稱": "佳世達",
    "主旨 ": "公告本公司董事會決議配息",
    "符合條款": "第51款",
    "事實發生日": "1151102",
    "說明": "董事會通過",
}
TPEX_ROW = {
    "Date": "1151103",
    "發言日期": "1151102",
    "發言時間": "070003",
    "SecuritiesCompanyCode": "6949",
    "CompanyName": "某公司",
    "主旨": "公告本公司股票面額變更",
    "符合條款": "第51款",
    "事實發生日": "1151102",
    "說明": "",
}


def make_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def make_client(handler):
    return AnnouncementClient(
        MemoryRepository(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def count(engine):
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(RawArticleRow))


def ok_handler(request):
    rows = [TWSE_ROW] if "twse" in str(request.url) else [TPEX_ROW]
    return httpx.Response(200, json=rows)


def test_collect_stores_both_markets():
    engine = make_engine()
    assert run(make_client(ok_handler), engine, NOW) == 0
    assert count(engine) == 2


def test_collect_is_idempotent_across_runs():
    engine = make_engine()
    assert run(make_client(ok_handler), engine, NOW) == 0
    later = NOW + dt.timedelta(hours=6)
    assert run(make_client(ok_handler), engine, later) == 0
    assert count(engine) == 2
    with Session(engine) as s:
        fetched = {
            r.fetched_at.replace(tzinfo=None) for r in s.scalars(select(RawArticleRow))
        }
    assert fetched == {NOW.replace(tzinfo=None)}  # first fetch time is kept


def test_one_source_failing_still_stores_the_other_and_returns_1(caplog):
    def handler(request):
        if "twse" in str(request.url):
            return httpx.Response(200, json=[TWSE_ROW])
        return httpx.Response(404)

    engine = make_engine()
    with caplog.at_level(logging.INFO):
        assert run(make_client(handler), engine, NOW) == 1
    assert count(engine) == 1
    assert "news_collect_failed market=TPEX" in caplog.text


def test_unexpected_columns_fail_that_source_only():
    def handler(request):
        if "twse" in str(request.url):
            return httpx.Response(200, json=[{"foo": "bar"}])
        return httpx.Response(200, json=[TPEX_ROW])

    engine = make_engine()
    assert run(make_client(handler), engine, NOW) == 1
    assert count(engine) == 1


def test_main_requires_database_url(monkeypatch):
    from app.jobs.news_collect import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main() == 1


def test_empty_batch_is_success_not_failure():
    def handler(request):
        return httpx.Response(200, json=[])

    engine = make_engine()
    assert run(make_client(handler), engine, NOW) == 0
    assert count(engine) == 0


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        run(make_client(ok_handler), make_engine(), NOW.replace(tzinfo=None))


def test_both_markets_share_one_run_id_and_taipei_date():
    client = make_client(ok_handler)
    utc_now = dt.datetime(2026, 11, 2, 18, 0, tzinfo=dt.timezone.utc)  # 11/03 02:00 TPE
    assert run(client, make_engine(), utc_now) == 0
    saved = client.repository.saved
    assert len(saved) == 2
    assert len({p.ingestion_run_id for p in saved}) == 1
    assert {p.target_date for p in saved} == {dt.date(2026, 11, 3)}


def test_log_reports_published_and_report_date_distribution(caplog):
    with caplog.at_level(logging.INFO):
        assert run(make_client(ok_handler), make_engine(), NOW) == 0
    # rows were published 11/02 but the dataset itself is dated 11/03
    assert "published_dates=2026-11-02:1" in caplog.text
    assert "report_dates=2026-11-03:1" in caplog.text
    assert "raw=1 mapped=1" in caplog.text


def runs(engine):
    from app.db.news_models import NewsCollectionRunRow

    with Session(engine) as s:
        return {r.market: r for r in s.scalars(select(NewsCollectionRunRow))}


def fixed_clock(*times):
    it = iter(times)
    return lambda: next(it)


def test_each_source_records_its_run():
    engine = make_engine()
    t1 = NOW + dt.timedelta(seconds=5)
    t2 = NOW + dt.timedelta(seconds=9)
    assert run(make_client(ok_handler), engine, NOW, clock=fixed_clock(t1, t2)) == 0
    rec = runs(engine)
    assert set(rec) == {"TWSE", "TPEX"}
    twse = rec["TWSE"]
    assert twse.success is True and twse.error is None
    assert (twse.raw_rows, twse.mapped_rows, twse.inserted_rows) == (1, 1, 1)
    assert twse.first_published_date == twse.last_published_date == dt.date(2026, 11, 2)
    assert twse.started_at.replace(tzinfo=None) == NOW.replace(tzinfo=None)
    assert twse.finished_at.replace(tzinfo=None) == t1.replace(tzinfo=None)
    assert rec["TPEX"].finished_at.replace(tzinfo=None) == t2.replace(tzinfo=None)
    assert twse.run_id == rec["TPEX"].run_id


def test_finished_at_is_taken_after_the_articles_are_committed():
    engine = make_engine()
    seen = []

    def clock():
        seen.append(count(engine))
        return NOW + dt.timedelta(seconds=len(seen))

    assert run(make_client(ok_handler), engine, NOW, clock=clock) == 0
    assert seen == [1, 2]  # each clock read already sees that source's row


def test_failed_source_is_recorded_as_failed_without_counts_for_it():
    def handler(request):
        if "twse" in str(request.url):
            return httpx.Response(200, json=[TWSE_ROW])
        return httpx.Response(404)

    engine = make_engine()
    assert run(make_client(handler), engine, NOW) == 1
    rec = runs(engine)
    assert rec["TWSE"].success is True
    tpex = rec["TPEX"]
    assert tpex.success is False
    assert tpex.error == "HTTPStatusError"
    assert tpex.mapped_rows is None and tpex.inserted_rows is None
    assert tpex.first_published_date is None


def test_bad_columns_are_recorded_as_a_failed_run():
    def handler(request):
        return httpx.Response(200, json=[{"foo": "bar"}])

    engine = make_engine()
    assert run(make_client(handler), engine, NOW) == 1
    assert all(r.success is False and r.error for r in runs(engine).values())


def test_empty_batch_is_a_successful_run_with_no_dates():
    engine = make_engine()
    assert run(make_client(lambda r: httpx.Response(200, json=[])), engine, NOW) == 0
    twse = runs(engine)["TWSE"]
    assert twse.success is True
    assert (twse.raw_rows, twse.mapped_rows, twse.inserted_rows) == (0, 0, 0)
    assert twse.first_published_date is None and twse.last_published_date is None


def test_failure_to_write_the_record_returns_1_but_keeps_the_articles(monkeypatch):
    from app.db.news_repository import CollectionRunRepository

    def boom(self, record):
        raise RuntimeError("db down")

    monkeypatch.setattr(CollectionRunRepository, "record", boom)
    engine = make_engine()
    assert run(make_client(ok_handler), engine, NOW) == 1
    assert count(engine) == 2 and runs(engine) == {}


def test_every_run_adds_new_records():
    engine = make_engine()
    assert run(make_client(ok_handler), engine, NOW) == 0
    assert run(make_client(ok_handler), engine, NOW + dt.timedelta(hours=6)) == 0
    from app.db.news_models import NewsCollectionRunRow

    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(NewsCollectionRunRow)) == 4


def test_clock_behind_now_never_makes_finished_before_started():
    engine = make_engine()
    behind = NOW - dt.timedelta(minutes=1)
    assert run(make_client(ok_handler), engine, NOW, clock=lambda: behind) == 0
    twse = runs(engine)["TWSE"]
    assert twse.finished_at >= twse.started_at
