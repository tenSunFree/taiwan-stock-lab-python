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
