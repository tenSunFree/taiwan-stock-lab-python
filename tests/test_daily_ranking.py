import datetime as dt

import httpx
import pytest

from app.ingestion.market_data_client import FinMindClient, TwseClient
from app.jobs.daily_ranking import InMemoryRawPayloadRepository, run

TARGET_DATE = dt.date(2026, 8, 7)

# Non-limit-up fixture: reference_price=2370.00-5.0000=2365.00,
# legal limit-up is far above 2370, so this must NOT become a candidate.
TWSE_CSV_NON_LIMIT_UP = (
    "日期,證券代號,證券名稱,成交股數,成交金額,"
    "開盤價,最高價,最低價,收盤價,漲跌價差,成交筆數\n"
    '"1150807","2330","台積電","24414025","57947015347",'
    '"2390.00","2395.00","2355.00","2370.00","5.0000","64670"\n'
)

FINMIND_STOCK_INFO_TSMC = {
    "data": [
        {
            "industry_category": "半導體業",
            "stock_id": "2330",
            "stock_name": "台積電",
            "type": "twse",
            "date": "2026-08-07",
        }
    ]
}

# Positive candidate fixture: close=44.65, price_change=4.05,
# reference=40.60. 40.60*1.10=44.660, tick below 50 is 0.05, round
# down -> 44.65 == close. Matches the official TWSE worked example
# already verified elsewhere in this codebase.
TWSE_CSV_LIMIT_UP = (
    "日期,證券代號,證券名稱,成交股數,成交金額,"
    "開盤價,最高價,最低價,收盤價,漲跌價差,成交筆數\n"
    '"1150807","1101","測試水泥","3000000","100000000",'
    '"41.00","44.65","40.80","44.65","4.05","10000"\n'
)

FINMIND_STOCK_INFO_LIMIT_UP = {
    "data": [
        {
            "industry_category": "水泥工業",
            "stock_id": "1101",
            "stock_name": "測試水泥",
            "type": "twse",
            "date": "2026-08-07",
        }
    ]
}


def make_clients(
    *, repository: InMemoryRawPayloadRepository, twse_csv: str, stock_info_data: dict
) -> tuple[TwseClient, FinMindClient]:
    """Build both fake HTTP clients on the SAME raw repository —
    mirrors production and lets the orchestration test verify both
    source snapshots belong to the same ingestion run."""

    def twse_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=twse_csv)

    def finmind_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=stock_info_data)

    twse_http_client = httpx.Client(transport=httpx.MockTransport(twse_handler))
    finmind_http_client = httpx.Client(transport=httpx.MockTransport(finmind_handler))

    twse_client = TwseClient(repository, http_client=twse_http_client)
    finmind_client = FinMindClient(
        repository, api_token="fake-token", http_client=finmind_http_client
    )

    return twse_client, finmind_client


def test_run_skips_non_trading_day(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-08")  # Saturday
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    result = run()
    assert result == 0


def test_run_returns_1_when_finmind_token_missing_and_no_client_injected(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    result = run()
    assert result == 1


def test_run_reports_waiting_for_data_when_twse_date_does_not_match(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    stale_csv = TWSE_CSV_NON_LIMIT_UP.replace("1150807", "1150806")
    twse_client, finmind_client = make_clients(
        repository=repository,
        twse_csv=stale_csv,
        stock_info_data=FINMIND_STOCK_INFO_TSMC,
    )

    result = run(
        repository=repository, twse_client=twse_client, finmind_client=finmind_client
    )

    assert result == 2
    assert len(repository.saved) == 1
    assert repository.saved[0].source == "twse"


def test_run_produces_zero_candidates_when_stock_is_not_limit_up(monkeypatch, caplog):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client, finmind_client = make_clients(
        repository=repository,
        twse_csv=TWSE_CSV_NON_LIMIT_UP,
        stock_info_data=FINMIND_STOCK_INFO_TSMC,
    )

    with caplog.at_level("INFO", logger="daily_ranking"):
        result = run(
            repository=repository,
            twse_client=twse_client,
            finmind_client=finmind_client,
        )

    assert result == 0
    assert "CandidateBuilder produced 0 provisional candidates" in caplog.text
    assert len(repository.saved) == 2
    assert {p.source for p in repository.saved} == {"twse", "finmind"}


def test_run_produces_limit_up_candidate_end_to_end(monkeypatch, caplog):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client, finmind_client = make_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        stock_info_data=FINMIND_STOCK_INFO_LIMIT_UP,
    )

    with caplog.at_level("INFO", logger="daily_ranking"):
        result = run(
            repository=repository,
            twse_client=twse_client,
            finmind_client=finmind_client,
        )

    assert result == 0
    assert "CandidateBuilder produced 1 provisional candidates" in caplog.text
    assert "stock_id=1101" in caplog.text
    assert len(repository.saved) == 2


def test_run_returns_1_when_stock_info_has_no_usable_rows(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client, finmind_client = make_clients(
        repository=repository,
        twse_csv=TWSE_CSV_NON_LIMIT_UP,
        stock_info_data={"data": []},
    )

    result = run(
        repository=repository, twse_client=twse_client, finmind_client=finmind_client
    )

    assert result == 1
    assert len(repository.saved) == 2


def test_run_returns_1_when_twse_fetch_raises(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()

    def twse_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    twse_http_client = httpx.Client(transport=httpx.MockTransport(twse_handler))
    twse_client = TwseClient(repository, http_client=twse_http_client)

    _, finmind_client = make_clients(
        repository=repository,
        twse_csv=TWSE_CSV_NON_LIMIT_UP,
        stock_info_data=FINMIND_STOCK_INFO_TSMC,
    )

    result = run(
        repository=repository, twse_client=twse_client, finmind_client=finmind_client
    )

    assert result == 1
    assert repository.saved == []


def test_run_raises_when_client_injected_without_repository(monkeypatch):
    """
    Regression test for the split-repository bug: injecting a client
    without also injecting the repository it was built with would
    silently under-count raw snapshots in the completion log. This
    must fail loudly instead.
    """
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    other_repo = InMemoryRawPayloadRepository()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=TWSE_CSV_NON_LIMIT_UP)

    twse_client = TwseClient(
        other_repo, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    with pytest.raises(ValueError):
        run(twse_client=twse_client)  # repository= deliberately omitted
