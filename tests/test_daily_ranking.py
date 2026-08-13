import datetime as dt

import httpx
import pytest

from app.ingestion.market_data_client import FinMindClient, TpexClient, TwseClient
from app.jobs.daily_ranking import InMemoryRawPayloadRepository, run

TARGET_DATE = dt.date(2026, 8, 7)

# --- TWSE fixtures ---

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

# Positive candidate fixture (TWSE): close=44.65, price_change=4.05,
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

# --- TPEx fixtures ---

# Non-limit-up TPEx fixture: reference_price=45.21-1.41=43.80,
# 43.80*1.10=48.18, well above close=45.21, so this must NOT become a
# candidate either. Used whenever the test only cares about the TWSE
# side producing (or not producing) a candidate.
TPEX_JSON_NON_LIMIT_UP = [
    {
        "Date": "1150807",
        "SecuritiesCompanyCode": "6488",
        "CompanyName": "測試上櫃股",
        "Close": "45.21",
        "Change": "+1.41",
        "Open": "44.20",
        "High": "45.26",
        "Low": "44.20",
        "TradingShares": "508551",
        "TransactionAmount": "22841328",
    }
]

FINMIND_STOCK_INFO_TPEX_STOCK = {
    "data": [
        {
            "industry_category": "半導體業",
            "stock_id": "6488",
            "stock_name": "測試上櫃股",
            "type": "tpex",
            "date": "2026-08-07",
        }
    ]
}


def make_twse_client(
    repository: InMemoryRawPayloadRepository, csv_text: str
) -> TwseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=csv_text)

    return TwseClient(
        repository, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def make_tpex_client(
    repository: InMemoryRawPayloadRepository, json_rows: list[dict]
) -> TpexClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=json_rows)

    return TpexClient(
        repository, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def make_finmind_client(
    repository: InMemoryRawPayloadRepository, stock_info_data: dict
) -> FinMindClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=stock_info_data)

    return FinMindClient(
        repository,
        api_token="fake-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def make_all_clients(
    *,
    repository: InMemoryRawPayloadRepository,
    twse_csv: str,
    tpex_rows: list[dict],
    stock_info_data: dict,
) -> tuple[TwseClient, TpexClient, FinMindClient]:
    return (
        make_twse_client(repository, twse_csv),
        make_tpex_client(repository, tpex_rows),
        make_finmind_client(repository, stock_info_data),
    )


def _merged_stock_info(*infos: dict) -> dict:
    merged_rows = []
    for info in infos:
        merged_rows.extend(info["data"])
    return {"data": merged_rows}


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


def test_run_raises_when_client_injected_without_repository(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    other_repo = InMemoryRawPayloadRepository()
    twse_client = make_twse_client(other_repo, TWSE_CSV_NON_LIMIT_UP)

    with pytest.raises(ValueError):
        run(twse_client=twse_client)  # repository= deliberately omitted


def test_run_raises_when_injected_client_uses_different_repository(monkeypatch):
    """
    Regression test for the split-repository bug: a client built with
    a different repository than the one passed to run() must be
    rejected loudly, not silently split raw-snapshot bookkeeping.
    """
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository_a = InMemoryRawPayloadRepository()
    repository_b = InMemoryRawPayloadRepository()

    twse_client = make_twse_client(
        repository_b, TWSE_CSV_NON_LIMIT_UP
    )  # built with repository_b
    tpex_client = make_tpex_client(repository_a, TPEX_JSON_NON_LIMIT_UP)
    finmind_client = make_finmind_client(repository_a, FINMIND_STOCK_INFO_TSMC)

    with pytest.raises(ValueError):
        run(
            repository=repository_a,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )


def test_run_reports_waiting_for_data_when_twse_date_does_not_match(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    stale_csv = TWSE_CSV_NON_LIMIT_UP.replace("1150807", "1150806")
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=stale_csv,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=FINMIND_STOCK_INFO_TSMC,
    )

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )

    assert result == 2
    # TWSE was fetched; TPEx and FinMind should NOT be called after
    # the TWSE stale-date guard fires first.
    assert len(repository.saved) == 1
    assert repository.saved[0].source == "twse"


def test_run_reports_waiting_for_data_when_tpex_date_does_not_match(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    stale_tpex_rows = [{**row, "Date": "1150806"} for row in TPEX_JSON_NON_LIMIT_UP]
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_NON_LIMIT_UP,
        tpex_rows=stale_tpex_rows,
        stock_info_data=FINMIND_STOCK_INFO_TSMC,
    )

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )

    assert result == 2
    # TWSE succeeded, TPEx failed its own date check — FinMind should
    # NOT be called.
    assert len(repository.saved) == 2
    assert {p.source for p in repository.saved} == {"twse", "tpex"}


def test_run_returns_1_when_tpex_row_is_not_a_dict(monkeypatch):
    """
    Regression test: TPEx's raw payload can pass the outer
    isinstance(..., list) check while still containing non-dict
    elements (e.g. ["error"] or [None]). Every row must be filtered
    before .get() is called on it, or this crashes with an
    uncaught exception instead of returning a clean status code.
    """
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client = make_twse_client(repository, TWSE_CSV_NON_LIMIT_UP)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not a dict", None])

    tpex_client = TpexClient(
        repository, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    finmind_client = make_finmind_client(repository, FINMIND_STOCK_INFO_TSMC)

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )

    assert (
        result == 2
    )  # every row filtered out -> treated as WAITING_FOR_DATA, not a crash


def test_run_reports_waiting_for_data_when_tpex_rows_are_all_filtered_out_by_mapper(
    monkeypatch,
):
    """
    Regression test: TPEx's readiness check (date match) can pass
    while build_tpex_daily_prices() still filters out every row for
    other reasons (e.g. missing SecuritiesCompanyCode) — this must
    not silently fall through to a TWSE-only candidate pool.
    """
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client = make_twse_client(repository, TWSE_CSV_LIMIT_UP)

    tpex_rows_missing_code = [
        {**row, "SecuritiesCompanyCode": ""} for row in TPEX_JSON_NON_LIMIT_UP
    ]
    tpex_client = make_tpex_client(repository, tpex_rows_missing_code)
    finmind_client = make_finmind_client(repository, FINMIND_STOCK_INFO_LIMIT_UP)

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )

    assert result == 2


def test_run_produces_zero_candidates_when_no_stock_is_limit_up(monkeypatch, caplog):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_NON_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_TSMC, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
    )

    with caplog.at_level("INFO", logger="daily_ranking"):
        result = run(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )

    assert result == 0
    assert "CandidateBuilder produced 0 provisional candidates" in caplog.text
    assert "twse=1, tpex=1" in caplog.text
    assert len(repository.saved) == 3
    assert {p.source for p in repository.saved} == {"twse", "tpex", "finmind"}


def test_run_produces_limit_up_candidate_from_twse_with_tpex_also_present(
    monkeypatch, caplog
):
    """
    Positive orchestration regression: TWSE has one real limit-up
    candidate, TPEx has one non-limit-up stock present in the same
    run — verifies both markets are merged into a single candidate
    pool and TPEx presence doesn't interfere with a TWSE candidate.
    """
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
    )

    with caplog.at_level("INFO", logger="daily_ranking"):
        result = run(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )

    assert result == 0
    assert "CandidateBuilder produced 1 provisional candidates" in caplog.text
    assert "stock_id=1101" in caplog.text
    # 3 base snapshots (twse + tpex + finmind stock_info) + 1 per-candidate
    # FinMind history snapshot (this test has exactly 1 candidate) = 4.
    assert len(repository.saved) == 4
    assert "Built StockFeatures for 1 candidates" in caplog.text


def test_run_returns_1_when_stock_info_has_no_usable_rows(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_NON_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data={"data": []},
    )

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )

    assert result == 1
    assert len(repository.saved) == 3


def test_run_returns_1_when_twse_fetch_raises(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    twse_client = TwseClient(
        repository, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    tpex_client = make_tpex_client(repository, TPEX_JSON_NON_LIMIT_UP)
    finmind_client = make_finmind_client(repository, FINMIND_STOCK_INFO_TSMC)

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )

    assert result == 1
    # fetch_and_snapshot() only saves after a successful HTTP fetch —
    # TWSE failed first, so nothing should be saved at all.
    assert repository.saved == []


def test_run_returns_1_when_tpex_fetch_raises(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client = make_twse_client(repository, TWSE_CSV_NON_LIMIT_UP)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    tpex_client = TpexClient(
        repository, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    finmind_client = make_finmind_client(repository, FINMIND_STOCK_INFO_TSMC)

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )

    assert result == 1
    # TWSE succeeded and was saved; TPEx failed, so only the TWSE
    # snapshot should be present.
    assert len(repository.saved) == 1
    assert repository.saved[0].source == "twse"
