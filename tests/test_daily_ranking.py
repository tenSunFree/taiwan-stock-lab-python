import datetime as dt

import httpx
import pytest

from app.ingestion.market_data_client import (
    FinMindClient,
    RawSourcePayload,
    TpexClient,
    TwseClient,
)
from app.domain.risk_policy import RiskPolicy
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
    # 3 base snapshots (twse + tpex + finmind stock_info) + 3 per-candidate
    # FinMind enrichment snapshots (price history + institutional +
    # monthly revenue; this test has exactly 1 candidate) = 6.
    assert len(repository.saved) == 6
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


# --- build_stock_features() unit tests ---


class FakeHistoryFinMindClient:
    """
    Duck-typed fake covering fetch_stock_price_history(),
    fetch_stock_institutional_investors(), and
    fetch_stock_monthly_revenue() — build_stock_features() calls all
    three independently, so a fake standing in for finmind_client must
    support all three. Deliberately NOT built on top of
    httpx.MockTransport, since this layer's contract is "does
    build_stock_features() call the client correctly and handle its
    result/failure correctly", not "is the HTTP request well-formed"
    (that's already covered by tests/test_market_data_client.py).
    """

    def __init__(
        self,
        *,
        rows_by_stock: dict[str, list[dict]],
        failing_stock_ids: set[str] | None = None,
        institutional_rows_by_stock: dict[str, list[dict]] | None = None,
        institutional_failing_stock_ids: set[str] | None = None,
        revenue_rows_by_stock: dict[str, list[dict]] | None = None,
        revenue_failing_stock_ids: set[str] | None = None,
    ) -> None:
        self.rows_by_stock = rows_by_stock
        self.failing_stock_ids = failing_stock_ids or set()
        self.institutional_rows_by_stock = institutional_rows_by_stock or {}
        self.institutional_failing_stock_ids = institutional_failing_stock_ids or set()
        self.revenue_rows_by_stock = revenue_rows_by_stock or {}
        self.revenue_failing_stock_ids = revenue_failing_stock_ids or set()
        self.calls: list[str] = []
        self.institutional_calls: list[str] = []
        self.revenue_calls: list[str] = []

    def fetch_stock_price_history(
        self, *, ingestion_run_id, stock_id, start_date, end_date, target_date
    ):
        self.calls.append(stock_id)
        if stock_id in self.failing_stock_ids:
            raise RuntimeError(f"simulated history failure: {stock_id}")

        return RawSourcePayload(
            ingestion_run_id=ingestion_run_id,
            source="finmind",
            target_date=target_date,
            requested_at=dt.datetime.now(dt.timezone.utc),
            source_updated_at=None,
            request_parameters={
                "dataset": "TaiwanStockPrice",
                "data_id": stock_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            schema_version="v1",
            payload_hash="test",
            raw_payload={"data": self.rows_by_stock.get(stock_id, [])},
            ingested_at=dt.datetime.now(dt.timezone.utc),
        )

    def fetch_stock_institutional_investors(
        self, *, ingestion_run_id, stock_id, start_date, end_date, target_date
    ):
        self.institutional_calls.append(stock_id)
        if stock_id in self.institutional_failing_stock_ids:
            raise RuntimeError(f"simulated institutional failure: {stock_id}")

        return RawSourcePayload(
            ingestion_run_id=ingestion_run_id,
            source="finmind",
            target_date=target_date,
            requested_at=dt.datetime.now(dt.timezone.utc),
            source_updated_at=None,
            request_parameters={
                "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
                "data_id": stock_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            schema_version="v1",
            payload_hash="test",
            raw_payload={"data": self.institutional_rows_by_stock.get(stock_id, [])},
            ingested_at=dt.datetime.now(dt.timezone.utc),
        )

    def fetch_stock_monthly_revenue(
        self, *, ingestion_run_id, stock_id, start_date, end_date, target_date
    ):
        self.revenue_calls.append(stock_id)
        if stock_id in self.revenue_failing_stock_ids:
            raise RuntimeError(f"simulated revenue failure: {stock_id}")

        return RawSourcePayload(
            ingestion_run_id=ingestion_run_id,
            source="finmind",
            target_date=target_date,
            requested_at=dt.datetime.now(dt.timezone.utc),
            source_updated_at=None,
            request_parameters={
                "dataset": "TaiwanStockMonthRevenue",
                "data_id": stock_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            schema_version="v1",
            payload_hash="test",
            raw_payload={"data": self.revenue_rows_by_stock.get(stock_id, [])},
            ingested_at=dt.datetime.now(dt.timezone.utc),
        )


def _make_candidate(
    stock_id: str,
    *,
    close="44.65",
    reference="40.60",
    turnover="100000000",
    volume=3_000_000,
    stock_name=None,
    is_attention=None,
    is_disposition=None,
    is_managed=None,
    one_price_limit_up=False,
):
    from decimal import Decimal

    from app.domain.candidate_builder import Candidate
    from app.domain.limit_up import LimitUpResult, LimitUpSource
    from app.domain.models import DailyPrice, Market, SecurityType, StockMaster

    stock = StockMaster(
        stock_id=stock_id,
        stock_name=stock_name or f"測試{stock_id}",
        market=Market.TWSE,
        security_type=SecurityType.COMMON_STOCK,
        is_attention=is_attention,
        is_disposition=is_disposition,
        is_managed=is_managed,
    )
    price = DailyPrice(
        trading_date=TARGET_DATE,
        stock_id=stock_id,
        reference_price=Decimal(reference),
        open_price=Decimal(close) if one_price_limit_up else Decimal(reference),
        high_price=Decimal(close),
        low_price=Decimal(close) if one_price_limit_up else Decimal(reference),
        close_price=Decimal(close),
        volume=volume,
        turnover=Decimal(turnover),
    )
    limit_up = LimitUpResult(
        is_close_limit_up=True,
        has_touched_limit_up=True,
        limit_up_price=Decimal(close),
        limit_up_source=LimitUpSource.CALCULATED,
        reason="test fixture",
    )
    return Candidate(stock=stock, price=price, limit_up=limit_up)


def _make_history_rows(
    count: int = 20,
    *,
    start=dt.date(2026, 6, 1),
    close="100",
    volume="1000000",
    turnover="100000000",
):
    return [
        {
            "date": (start + dt.timedelta(days=i)).isoformat(),
            "close": close,
            "Trading_Volume": volume,
            "Trading_money": turnover,
        }
        for i in range(count)
    ]


def _make_institutional_rows(
    count: int = 5, *, start=dt.date(2026, 6, 1), buy=1000, sell=200, stock_id="1101"
):
    return [
        {
            "date": (start + dt.timedelta(days=i)).isoformat(),
            "stock_id": stock_id,
            "buy": buy,
            "sell": sell,
        }
        for i in range(count)
    ]


def _make_monthly_revenue_rows(
    *,
    stock_id: str = "1101",
    current_year: int = 2026,
    current_month: int = 7,
    current_revenue: int = 1_300_000_000,
    previous_revenue: int = 1_000_000_000,
    create_time: str = "2026-08-05 09:00:00",  # 早於 TARGET_DATE(2026-08-07）
):
    return [
        {
            "stock_id": stock_id,
            "revenue_year": current_year,
            "revenue_month": current_month,
            "revenue": current_revenue,
            "create_time": create_time,
        },
        {
            "stock_id": stock_id,
            "revenue_year": current_year - 1,
            "revenue_month": current_month,
            "revenue": previous_revenue,
            "create_time": "",  # 舊資料沒有 create_time，只能當分母用
        },
    ]


def test_build_stock_features_empty_candidates_makes_no_finmind_calls():
    from app.jobs.daily_ranking import build_stock_features

    client = FakeHistoryFinMindClient(rows_by_stock={})
    features = build_stock_features(
        candidates=[],
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
    )
    assert features == []
    assert client.calls == []
    assert client.institutional_calls == []
    assert client.revenue_calls == []


def test_build_stock_features_computes_real_technical_factors_on_success():
    from app.jobs.daily_ranking import build_stock_features

    candidates = [
        _make_candidate("1101", close="44.65", turnover="100000000", volume=3_000_000)
    ]
    history_rows = _make_history_rows(20, close="100", volume="1000000")
    client = FakeHistoryFinMindClient(
        rows_by_stock={"1101": history_rows},
        institutional_rows_by_stock={
            "1101": _make_institutional_rows(
                5, start=dt.date(2026, 6, 16), buy=1000, sell=200
            )
        },
        revenue_rows_by_stock={"1101": _make_monthly_revenue_rows(stock_id="1101")},
    )

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
    )

    assert len(features) == 1
    feature = features[0]
    assert feature.stock_id == "1101"
    assert feature.turnover == 100000000.0
    assert feature.average_turnover_20d == pytest.approx(100000000.0)
    assert feature.volume_ratio_20d == pytest.approx(3.0)  # 3,000,000 / 1,000,000
    assert feature.return_5d is not None
    assert feature.return_20d is not None
    assert feature.institutional_net_buy_ratio_5d is not None
    assert feature.revenue_yoy is not None
    assert feature.revenue_yoy == pytest.approx(0.30)  # 1,300,000,000/1,000,000,000-1
    assert client.calls == ["1101"]
    assert client.institutional_calls == ["1101"]
    assert client.revenue_calls == ["1101"]


def test_build_stock_features_single_history_failure_does_not_abort_batch(caplog):
    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101"), _make_candidate("2330")]
    client = FakeHistoryFinMindClient(
        rows_by_stock={"1101": _make_history_rows(20)},
        failing_stock_ids={"2330"},
        institutional_rows_by_stock={
            "1101": _make_institutional_rows(5, stock_id="1101"),
            "2330": _make_institutional_rows(5, stock_id="2330"),
        },
    )

    with caplog.at_level("INFO", logger="daily_ranking"):
        features = build_stock_features(
            candidates=candidates,
            target_date=TARGET_DATE,
            finmind_client=client,
            ingestion_run_id="run-1",
            risk_policy=RiskPolicy(),
        )

    assert len(features) == 2
    by_stock = {f.stock_id: f for f in features}

    assert by_stock["1101"].average_turnover_20d is not None
    assert by_stock["1101"].return_5d is not None

    assert by_stock["2330"].average_turnover_20d is None
    assert by_stock["2330"].volume_ratio_20d is None
    assert by_stock["2330"].return_5d is None
    assert by_stock["2330"].return_20d is None
    # turnover itself still comes from today's real TWSE/TPEx data,
    # unaffected by the FinMind history failure
    assert by_stock["2330"].turnover == 100000000.0

    assert client.calls == ["1101", "2330"]
    assert "failed for stock_id=2330" in caplog.text
    assert client.revenue_calls == ["1101", "2330"]


def test_build_stock_features_empty_history_rows_leaves_factors_none():
    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101")]
    client = FakeHistoryFinMindClient(rows_by_stock={"1101": []})

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
    )

    assert features[0].average_turnover_20d is None
    assert features[0].return_5d is None


def test_build_stock_features_institutional_failure_does_not_clear_price_factors():
    """
    Regression test for the independence requirement: a stock's
    institutional-flow fetch failing must not clear technical factors
    that were already successfully computed from a working
    price-history fetch for the SAME stock.
    """
    from app.jobs.daily_ranking import build_stock_features

    candidates = [
        _make_candidate("1101", close="44.65", turnover="100000000", volume=3_000_000)
    ]
    client = FakeHistoryFinMindClient(
        rows_by_stock={"1101": _make_history_rows(20, close="100", volume="1000000")},
        institutional_failing_stock_ids={"1101"},
    )

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
    )

    assert len(features) == 1
    feature = features[0]
    # price-history factors still computed normally
    assert feature.average_turnover_20d is not None
    assert feature.return_5d is not None
    # institutional factor is None because ITS OWN fetch failed
    assert feature.institutional_net_buy_ratio_5d is None


def test_build_stock_features_price_failure_does_not_prevent_institutional_attempt():
    """
    The reverse direction: price-history fetch failing must not skip
    the institutional-flow attempt for the same stock. (The
    institutional ratio will still end up None here because there's
    no volume data to divide by — but the call itself must still
    happen, and that absence must come from insufficient data, not
    from the code skipping the block.)
    """
    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101")]
    client = FakeHistoryFinMindClient(
        rows_by_stock={},
        failing_stock_ids={"1101"},
        institutional_rows_by_stock={
            "1101": _make_institutional_rows(
                5, start=dt.date(2026, 6, 16), buy=1000, sell=200
            )
        },
    )

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
    )

    assert client.institutional_calls == ["1101"]  # the attempt happened
    assert client.revenue_calls == ["1101"]  # the revenue attempt also happened
    assert features[0].average_turnover_20d is None  # price side failed
    assert (
        features[0].institutional_net_buy_ratio_5d is None
    )  # no volume data to divide by


def test_build_stock_features_revenue_failure_does_not_clear_other_factors():
    """
    Regression test for the independence requirement: a stock's
    monthly-revenue fetch failing must not clear technical or
    institutional factors already successfully computed for the SAME
    stock.
    """
    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101")]
    client = FakeHistoryFinMindClient(
        rows_by_stock={"1101": _make_history_rows(20)},
        institutional_rows_by_stock={
            "1101": _make_institutional_rows(
                5, start=dt.date(2026, 6, 16), buy=1000, sell=200
            )
        },
        revenue_failing_stock_ids={"1101"},
    )

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
    )

    assert len(features) == 1
    feature = features[0]
    assert feature.average_turnover_20d is not None
    assert feature.institutional_net_buy_ratio_5d is not None
    assert feature.revenue_yoy is None  # 只有 revenue 這個 block 失敗
    assert client.calls == ["1101"]
    assert client.institutional_calls == ["1101"]
    assert client.revenue_calls == ["1101"]


def test_build_stock_features_revenue_empty_rows_leaves_revenue_yoy_none():
    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101")]
    client = FakeHistoryFinMindClient(
        rows_by_stock={"1101": _make_history_rows(20)},
        revenue_rows_by_stock={"1101": []},
    )

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
    )

    assert features[0].revenue_yoy is None
    assert client.revenue_calls == ["1101"]


# --- Risk assessment (Step 2) ---


def test_build_stock_features_disposition_stock_is_excluded_from_features():
    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101", is_disposition=True)]
    client = FakeHistoryFinMindClient(rows_by_stock={"1101": _make_history_rows(20)})

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
    )

    assert features == []


def test_build_stock_features_ky_stock_flagged_with_known_clean_status():
    from app.jobs.daily_ranking import build_stock_features

    candidates = [
        _make_candidate(
            "1101",
            stock_name="測試-KY",
            is_attention=False,
            is_disposition=False,
            is_managed=False,
        )
    ]
    client = FakeHistoryFinMindClient(rows_by_stock={"1101": _make_history_rows(20)})

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
    )

    assert len(features) == 1
    assert "KY_STOCK" in features[0].risk_flags
    # attention/disposition/managed are all known False here, but
    # consecutive_limit_up_days is always None -> still incomplete
    assert features[0].risk_quality_raw is None


def test_build_stock_features_one_price_limit_up_flagged():
    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101", one_price_limit_up=True)]
    client = FakeHistoryFinMindClient(rows_by_stock={"1101": _make_history_rows(20)})

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
    )

    assert len(features) == 1
    assert "ONE_PRICE_LIMIT_UP" in features[0].risk_flags


def test_build_stock_features_unknown_status_never_yields_full_risk_quality():
    """
    Core regression for Step 2: a stock with no risk flags raised must
    NOT get risk_quality_raw=1.0 when its underlying status is unknown
    (the default/realistic case right now for every stock, since
    is_attention/is_disposition/is_managed have no wired-in data
    source). "No flags" here means "we didn't check," not "confirmed
    clean" — build_risk_quality_raw() must return None, not 1.0.
    """
    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101")]  # attention/disposition/managed all None
    client = FakeHistoryFinMindClient(rows_by_stock={"1101": _make_history_rows(20)})

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
    )

    assert len(features) == 1
    assert features[0].risk_flags == ()
    assert features[0].risk_quality_raw is None  # NOT 1.0


def test_build_stock_features_risk_assessment_failure_excludes_defensively(caplog):
    """
    If RiskPolicy.assess() itself raises for some reason, the stock is
    excluded defensively (same "fail closed, never score with unknown
    risk" posture as a hard exclusion) rather than silently getting a
    None/1.0 risk_quality_raw.
    """
    from unittest.mock import patch

    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101")]
    client = FakeHistoryFinMindClient(rows_by_stock={"1101": _make_history_rows(20)})

    with patch(
        "app.jobs.daily_ranking.is_one_price_limit_up",
        side_effect=RuntimeError("simulated risk-input failure"),
    ):
        with caplog.at_level("ERROR", logger="daily_ranking"):
            features = build_stock_features(
                candidates=candidates,
                target_date=TARGET_DATE,
                finmind_client=client,
                ingestion_run_id="run-1",
                risk_policy=RiskPolicy(),
            )

    assert features == []
    assert "Risk assessment failed for stock_id=1101" in caplog.text


def test_build_stock_features_risk_gap_warning_logged(caplog):
    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101")]
    client = FakeHistoryFinMindClient(rows_by_stock={"1101": _make_history_rows(20)})

    with caplog.at_level("WARNING", logger="daily_ranking"):
        build_stock_features(
            candidates=candidates,
            target_date=TARGET_DATE,
            finmind_client=client,
            ingestion_run_id="run-1",
            risk_policy=RiskPolicy(),
        )

    assert "RiskPolicy input gap" in caplog.text


def test_build_stock_features_risk_gap_warning_logged_even_with_no_candidates(caplog):
    """The warning must fire unconditionally, before the empty-candidates
    early return, so the gap stays visible on every call regardless of
    candidate count."""
    from app.jobs.daily_ranking import build_stock_features

    client = FakeHistoryFinMindClient(rows_by_stock={})

    with caplog.at_level("WARNING", logger="daily_ranking"):
        features = build_stock_features(
            candidates=[],
            target_date=TARGET_DATE,
            finmind_client=client,
            ingestion_run_id="run-1",
            risk_policy=RiskPolicy(),
        )

    assert features == []
    assert "RiskPolicy input gap" in caplog.text
