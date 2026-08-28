import datetime as dt

import httpx
import pytest

from app.ingestion.market_data_client import (
    FinMindClient,
    RawSourcePayload,
    TpexClient,
    TwseClient,
)
from app.domain.risk_policy import RiskPolicy, RiskPolicyConfig
from app.ingestion.twse_mapper import parse_stock_day_all_csv
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


def _default_twse_valuation_rows(csv_text: str, *, pe_ratio: str = "15") -> list[dict]:
    """
    Auto-derive a passing-P/E BWIBBU_ALL row (same Date each row
    already carries) for every stock_id present in a TWSE
    STOCK_DAY_ALL fixture, so tests that don't care about the P/E
    eligibility filter don't need to know it exists. Tests that
    specifically exercise the filter pass their own valuation_rows to
    make_twse_client/make_all_clients instead of relying on this.
    """
    rows = parse_stock_day_all_csv(csv_text)
    result: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        stock_id = (row.get("證券代號") or "").strip()
        date = (row.get("日期") or "").strip()
        if not stock_id or stock_id in seen:
            continue
        seen.add(stock_id)
        result.append({"Date": date, "Code": stock_id, "PEratio": pe_ratio})
    return result


def _default_tpex_valuation_rows(
    json_rows: list[dict], *, pe_ratio: str = "15"
) -> list[dict]:
    """Same idea as _default_twse_valuation_rows, for TPEx's
    tpex_mainboard_peratio_analysis shape."""
    result: list[dict] = []
    seen: set[str] = set()
    for row in json_rows:
        stock_id = str(row.get("SecuritiesCompanyCode") or "").strip()
        date = str(row.get("Date") or "").strip()
        if not stock_id or stock_id in seen:
            continue
        seen.add(stock_id)
        result.append(
            {
                "Date": date,
                "SecuritiesCompanyCode": stock_id,
                "PriceEarningRatio": pe_ratio,
            }
        )
    return result


def _default_twse_attention_html() -> str:
    """
    Structurally valid, empty attention list, with a deliberately WIDE
    title date range (2001~2110) so it satisfies
    build_twse_attention_statuses's title-window-covers-target_date
    check regardless of which target_date a given test happens to use
    — most tests in this file don't care about regulatory data at all
    and shouldn't need to track target_date just to get a passing
    default fixture.
    """
    return (
        "<!doctype html><html><body><div><table><thead>"
        "<tr><th colspan='8'><div>公布注意有價證券資訊 "
        "(90年01月01日 至 199年12月31日 全部上市有價證券)</div></th></tr>"
        "<tr><th>編號</th><th>證券代號</th><th>證券名稱</th><th>累計次數</th>"
        "<th>注意交易資訊</th><th>日期</th><th>收盤價</th><th>本益比</th></tr>"
        "</thead><tbody></tbody></table></div></body></html>"
    )


def _default_twse_disposition_html() -> str:
    """Structurally valid, empty disposition list. Disposition only
    needs a PARSEABLE title (not one covering target_date — see
    build_twse_disposition_statuses's own docstring), so the exact
    date range here doesn't matter as long as it parses."""
    return (
        "<!doctype html><html><body><div><table><thead>"
        "<tr><th colspan='10'><div>公布處置有價證券資訊 "
        "(115/01/01 至 115/12/31)</div></th></tr>"
        "<tr><th>編號</th><th>公布日期</th><th>證券代號</th><th>證券名稱</th>"
        "<th>累計</th><th>處置條件</th><th>處置起迄時間</th><th>處置措施</th>"
        "<th>處置內容</th><th>備註</th></tr>"
        "</thead><tbody></tbody></table></div></body></html>"
    )


_DEFAULT_TPEX_ATTENTION_PAYLOAD = {
    "tables": [
        {
            "fields": [
                "編號",
                "證券代號",
                "證券名稱",
                "累計",
                "注意交易資訊",
                "公告日期",
                "收盤價",
                "本益比",
                "link",
            ],
            "data": [],
        }
    ]
}

_DEFAULT_TPEX_DISPOSITION_PAYLOAD = {
    "tables": [
        {
            "fields": [
                "編號",
                "公布日期",
                "證券代號",
                "證券名稱",
                "累計",
                "處置起訖時間",
                "處置原因",
                "處置內容",
                "收盤價",
                "本益比",
                " ",
            ],
            "data": [],
        }
    ]
}


def make_twse_client(
    repository: InMemoryRawPayloadRepository,
    csv_text: str,
    *,
    valuation_rows: list[dict] | None = None,
    attention_html: str | None = None,
    disposition_html: str | None = None,
) -> TwseClient:
    """
    valuation_rows: BWIBBU_ALL rows to serve from fetch_valuation().
        Defaults to a permissive P/E (15) for every stock_id found in
        csv_text, so callers that don't care about the P/E filter
        don't have to construct valuation fixtures by hand. Pass an
        empty list explicitly to simulate "no usable valuation data"
        (WAITING_FOR_DATA), or a list with specific P/E values to
        exercise the filter's pass/fail boundary.
    attention_html / disposition_html: raw HTML to serve from
        fetch_attention()/fetch_disposition(). Default to a
        structurally valid, empty (zero currently-flagged stocks)
        response, so callers that don't care about regulatory risk
        data don't have to construct HTML fixtures by hand. Pass a
        deliberately malformed string to exercise
        RegulatorySourceFormatError handling.
    """
    if valuation_rows is None:
        valuation_rows = _default_twse_valuation_rows(csv_text)
    if attention_html is None:
        attention_html = _default_twse_attention_html()
    if disposition_html is None:
        disposition_html = _default_twse_disposition_html()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/exchangeReport/BWIBBU_ALL":
            return httpx.Response(200, json=valuation_rows)
        if request.url.path == "/announcement/notice":
            return httpx.Response(200, text=attention_html)
        if request.url.path == "/announcement/punish":
            return httpx.Response(200, text=disposition_html)
        return httpx.Response(200, text=csv_text)

    return TwseClient(
        repository, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def make_tpex_client(
    repository: InMemoryRawPayloadRepository,
    json_rows: list[dict],
    *,
    valuation_rows: list[dict] | None = None,
    attention_payload: dict | None = None,
    disposition_payload: dict | None = None,
) -> TpexClient:
    """valuation_rows: same idea as make_twse_client's, but for
    tpex_mainboard_peratio_analysis, which is a BARE JSON array — same
    shape as fetch_daily_price()'s, not wrapped in any envelope (an
    earlier version of this fixture wrongly wrapped it in
    {"value": [...], "Count": N}; see
    market_data_client.TpexClient.fetch_valuation's docstring for why
    that was wrong — confirmed only via a raw HTTP body dump, not
    PowerShell's Invoke-RestMethod/ConvertTo-Json, which can silently
    reshape a bare array into something that looks wrapped).
    attention_payload / disposition_payload: same idea, for
    fetch_attention()/fetch_disposition() — default to a structurally
    valid, empty {"tables": [...]} response."""
    if valuation_rows is None:
        valuation_rows = _default_tpex_valuation_rows(json_rows)
    if attention_payload is None:
        attention_payload = _DEFAULT_TPEX_ATTENTION_PAYLOAD
    if disposition_payload is None:
        disposition_payload = _DEFAULT_TPEX_DISPOSITION_PAYLOAD

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi/v1/tpex_mainboard_peratio_analysis":
            return httpx.Response(200, json=valuation_rows)
        if request.url.path == "/www/zh-tw/bulletin/attention":
            return httpx.Response(200, json=attention_payload)
        if request.url.path == "/www/zh-tw/bulletin/disposal":
            return httpx.Response(200, json=disposition_payload)
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
    twse_valuation_rows: list[dict] | None = None,
    tpex_valuation_rows: list[dict] | None = None,
    twse_attention_html: str | None = None,
    twse_disposition_html: str | None = None,
    tpex_attention_payload: dict | None = None,
    tpex_disposition_payload: dict | None = None,
) -> tuple[TwseClient, TpexClient, FinMindClient]:
    return (
        make_twse_client(
            repository,
            twse_csv,
            valuation_rows=twse_valuation_rows,
            attention_html=twse_attention_html,
            disposition_html=twse_disposition_html,
        ),
        make_tpex_client(
            repository,
            tpex_rows,
            valuation_rows=tpex_valuation_rows,
            attention_payload=tpex_attention_payload,
            disposition_payload=tpex_disposition_payload,
        ),
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
    # 9 base snapshots: twse price + tpex price + twse valuation +
    # tpex valuation + twse attention + twse disposition + tpex
    # attention + tpex disposition + finmind stock_info.
    assert len(repository.saved) == 9
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
    # 9 base snapshots (twse price + tpex price + twse valuation +
    # tpex valuation + twse attention + twse disposition + tpex
    # attention + tpex disposition + finmind stock_info) + 3
    # per-candidate FinMind enrichment snapshots (price history +
    # institutional + monthly revenue; this test has exactly 1
    # candidate) = 12.
    assert len(repository.saved) == 12
    assert "Built StockFeatures for 1 of 1 candidates after RiskPolicy" in caplog.text


# --- P/E eligibility filter (Step 4.5) integration tests --------------------


def test_run_excludes_candidate_with_pe_above_maximum_before_finmind_enrichment(
    monkeypatch, caplog
):
    """
    The core regression this filter exists for: a candidate whose P/E
    exceeds MAXIMUM_PE_RATIO must be excluded BEFORE FinMind
    enrichment, not just left out of the final ranking. Asserting on
    the raw-snapshot count is what actually proves "before enrichment"
    — if the filter ran after Step 5 instead, the FinMind
    enrichment snapshots would still show up here.
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
        # override the default permissive P/E: stock 1101 gets 25.00,
        # which must fail the 0 < P/E <= 20 rule
        twse_valuation_rows=[{"Date": "1150807", "Code": "1101", "PEratio": "25.00"}],
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
    assert "P/E eligibility filter: before=1 after=0" in caplog.text
    assert "Built StockFeatures for 0 of 0 candidates after RiskPolicy" in caplog.text
    # Exactly the 9 base snapshots — zero per-candidate FinMind
    # enrichment snapshots, proving the excluded candidate never
    # reached Step 5 at all.
    assert len(repository.saved) == 9


def test_run_keeps_candidate_with_pe_exactly_at_maximum(monkeypatch, caplog):
    """Boundary test: P/E == MAXIMUM_PE_RATIO (20) is inclusive —
    "不高於 20 倍" means <= 20, not < 20."""
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
        twse_valuation_rows=[{"Date": "1150807", "Code": "1101", "PEratio": "20"}],
    )

    with caplog.at_level("INFO", logger="daily_ranking"):
        result = run(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )

    assert result == 0
    assert "P/E eligibility filter: before=1 after=1" in caplog.text
    assert "Built StockFeatures for 1 of 1 candidates after RiskPolicy" in caplog.text


def test_run_excludes_candidate_with_no_pe_available(monkeypatch, caplog):
    """A candidate present in the price data but ABSENT from the
    valuation snapshot (e.g. TWSE didn't publish a P/E for it — zero
    or negative trailing EPS) must fail-close, the same as an
    explicit above-threshold P/E."""
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
        # stock 1101 has no valuation row at all
        twse_valuation_rows=[],
    )

    with caplog.at_level("INFO", logger="daily_ranking"):
        result = run(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )

    # No usable TWSE valuation rows at all -> WAITING_FOR_DATA, per
    # this file's own failure policy (whole-source valuation gaps are
    # NOT treated the same as one stock's missing P/E — see the
    # Step 1c comment in daily_ranking.py). This test's fixture
    # (empty twse_valuation_rows) exercises exactly that whole-source
    # case, not the narrower "this one stock has no row" case.
    assert result == 2
    assert (
        "WAITING_FOR_DATA: TWSE BWIBBU_ALL returned no usable valuation rows"
        in caplog.text
    )


def test_run_waits_for_data_when_valuation_snapshot_is_implausibly_stale(
    monkeypatch, caplog
):
    """
    A short lag (e.g. 1 day, matching a real observed BWIBBU_ALL
    delay) is expected and accepted — see
    build_twse_valuations's docstring. But a valuation snapshot dated
    far behind target_date (here: 10 days, past
    MAXIMUM_VALUATION_STALENESS_DAYS=5) must NOT be silently accepted
    just because build_twse_valuations found *some* date <=
    target_date — that's indistinguishable from a genuinely stalled
    source, not a normal short lag.
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
        # target_date is 2026-08-07; this valuation row is dated
        # 2026-07-28 (ROC 1150728) — 10 calendar days behind.
        twse_valuation_rows=[{"Date": "1150728", "Code": "1101", "PEratio": "15"}],
    )

    with caplog.at_level("INFO", logger="daily_ranking"):
        result = run(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )

    assert result == 2
    assert "WAITING_FOR_DATA: TWSE BWIBBU_ALL's newest available date" in caplog.text
    assert "10d behind target_date" in caplog.text


def test_run_accepts_valuation_snapshot_within_staleness_bound(monkeypatch, caplog):
    """The boundary just inside MAXIMUM_VALUATION_STALENESS_DAYS (5)
    must still be accepted — this bound rejects genuinely stale data,
    it isn't a stricter re-implementation of the same-day check we
    deliberately moved away from."""
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
        # 2026-08-02 (ROC 1150802) is exactly 5 days before 2026-08-07.
        twse_valuation_rows=[{"Date": "1150802", "Code": "1101", "PEratio": "15"}],
    )

    with caplog.at_level("INFO", logger="daily_ranking"):
        result = run(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )

    assert result == 0
    assert "Valuation snapshot ready" in caplog.text
    assert "P/E eligibility filter: before=1 after=1" in caplog.text


def test_run_excludes_single_stock_missing_from_an_otherwise_populated_snapshot(
    monkeypatch, caplog
):
    """The narrower case test_run_excludes_candidate_with_no_pe_available's
    docstring points to: the valuation snapshot itself is fine (has
    rows), but this ONE candidate's stock_id isn't among them —
    should fail-close for that stock only, not WAITING_FOR_DATA."""
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
        # valuation snapshot has data, just not for stock 1101
        twse_valuation_rows=[{"Date": "1150807", "Code": "9999", "PEratio": "12"}],
    )

    with caplog.at_level("INFO", logger="daily_ranking"):
        result = run(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )

    assert result == 0
    assert "P/E eligibility filter: before=1 after=0" in caplog.text
    assert "Built StockFeatures for 0 of 0 candidates after RiskPolicy" in caplog.text


def test_strategy_version_bumped_to_rule_v1_2_0():
    """
    Regression test for the STRATEGY_VERSION bump — both the P/E
    filter (rule-v1.1.0) and the disposition/managed-stock policy
    change (rule-v1.2.0: hard exclusion -> configurable, default
    allowed-but-flagged; ATTENTION_STOCK penalty 0.15 -> 0.0) changed
    which candidates are eligible and what score they get, which per
    this file's own versioning rule (see the yaml's comment: "when
    tuning, create a new strategy_version instead of overwriting this
    one") each require their own version rather than reusing a prior
    one.

    Asserted directly on the constant rather than via a run()+caplog
    round trip: STRATEGY_VERSION is only ever logged when
    LINE_DELIVERY_MODE != "off" (see the LINE delivery branch further
    down this file), so an integration-style assertion here would
    pass or fail for reasons unrelated to what this test actually
    means to check.
    """
    from app.jobs.daily_ranking import STRATEGY_VERSION

    assert STRATEGY_VERSION == "rule-v1.2.0"


def test_run_report_candidate_count_reflects_pre_pe_filter_pool(capsys, monkeypatch):
    """
    Regression test for a bug CodeRabbit review caught: Step 4.5
    reassigns `candidates` to the P/E-eligible subset, so a later
    len(candidates) no longer means what render_daily_report's
    candidate_count parameter is documented and labeled ("進入候選
    池" / "entered CandidateBuilder's pool") to mean. Every OTHER test
    in this file uses fixtures where the default permissive P/E (15)
    lets 100% of candidates through, so pre-filter and post-filter
    counts happened to always be equal — this test deliberately uses
    TWO candidates where only ONE passes the P/E filter, so the two
    counts differ and the bug can't hide behind a coincidence.
    """
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")
    monkeypatch.setenv("REPORT_DRY_RUN", "true")

    twse_csv_two_candidates = (
        "日期,證券代號,證券名稱,成交股數,成交金額,"
        "開盤價,最高價,最低價,收盤價,漲跌價差,成交筆數\n"
        '"1150807","1101","測試水泥","3000000","100000000",'
        '"41.00","44.65","40.80","44.65","4.05","10000"\n'
        '"1150807","2330","測試台積電","2000000","80000000",'
        '"41.00","44.65","40.80","44.65","4.05","8000"\n'
    )
    finmind_stock_info_two_candidates = {
        "data": [
            {
                "industry_category": "水泥工業",
                "stock_id": "1101",
                "stock_name": "測試水泥",
                "type": "twse",
                "date": "2026-08-07",
            },
            {
                "industry_category": "半導體業",
                "stock_id": "2330",
                "stock_name": "測試台積電",
                "type": "twse",
                "date": "2026-08-07",
            },
        ]
    }

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=twse_csv_two_candidates,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            finmind_stock_info_two_candidates, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
        # 1101 passes (P/E 15 <= 20), 2330 fails (P/E 25 > 20)
        twse_valuation_rows=[
            {"Date": "1150807", "Code": "1101", "PEratio": "15"},
            {"Date": "1150807", "Code": "2330", "PEratio": "25"},
        ],
    )

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )

    assert result == 0
    captured = capsys.readouterr()
    # 2 candidates entered CandidateBuilder's pool, even though only 1
    # survived the P/E filter — the report must say "2 檔" here, not
    # "1 檔" (which would silently redefine what "進入候選池" means).
    assert "進入候選池：2 檔" in captured.out


# --- Step 1d / Step 5: official regulatory risk data end-to-end ------------


def test_run_surfaces_twse_attention_flag_through_the_whole_pipeline(
    monkeypatch, caplog
):
    """
    End-to-end regression: a stock hit in TWSE's real HTML attention
    response must actually flow through fetch_attention() ->
    twse_regulatory_mapper's HTML parsing -> the OR-merge into
    regulatory_by_stock — proves the real ingestion path is wired
    correctly, not just each piece in isolation (fetch tested at the
    client level, parsing tested in test_twse_regulatory_mapper.py,
    RiskPolicy resolution tested in
    test_build_stock_features_disposition_stock_allowed_but_flagged's
    siblings — this is the piece connecting all three).
    """
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")

    attention_html = (
        "<!doctype html><html><body><div><table><thead>"
        "<tr><th colspan='8'><div>公布注意有價證券資訊 "
        "(115年08月07日 至 115年08月07日 全部上市有價證券)</div></th></tr>"
        "<tr><th>編號</th><th>證券代號</th><th>證券名稱</th><th>累計次數</th>"
        "<th>注意交易資訊</th><th>日期</th><th>收盤價</th><th>本益比</th></tr>"
        "</thead><tbody>"
        "<tr><td>1</td><td>1101</td><td>測試水泥</td><td>3</td>"
        "<td>近期異常</td><td>115/08/07</td><td>44.65</td><td>15.0</td></tr>"
        "</tbody></table></div></body></html>"
    )

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
        twse_attention_html=attention_html,
    )

    with caplog.at_level("INFO", logger="daily_ranking"):
        result = run(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )

    assert result == 0
    assert "Regulatory risk snapshot: twse_attention_ok=True(1)" in caplog.text
    assert "merged=1" in caplog.text


def test_run_surfaces_twse_disposition_period_in_the_rendered_report(
    capsys, monkeypatch
):
    """
    Step 6 end-to-end regression: a disposition hit from TWSE's real
    HTML must flow all the way through fetch -> mapper -> merge ->
    RiskPolicy -> ScoredStock.risk_flags -> report_builder's
    regulatory_by_stock merge -> the rendered report text, showing the
    actual disposition period — not just the bare DISPOSITION_STOCK
    flag name. Patches score_candidates() at the boundary (same
    pattern as test_run_report_dry_run_prints_ranked_report) since
    FinMind enrichment internals are already covered elsewhere; this
    test's job is to prove the regulatory data specifically reaches
    the final report text.

    text-v7 update: disposition status label changed from "🚨 處置股：
    處置中" (text-v6) to "🚨 目前處置：是" to clarify disposition is an
    active-period status ("目前處置"), not a persistent "type" of
    stock — attention and disposition answer genuinely different
    time-axis questions (per-day announcement vs. active-period
    match), so parallel wording like "注意股：正常" + "處置股：處置中"
    could misleadingly read as a contradiction. A closing
    "處置措施：請依交易所該次公告為準" line was also added — see
    text_renderer.py's _disposition_status_lines docstring for why
    concrete trading measures are deliberately never hardcoded here.
    """
    from unittest.mock import patch

    from app.domain.scoring import ScoredStock

    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")
    monkeypatch.setenv("REPORT_DRY_RUN", "true")

    disposition_html = (
        "<!doctype html><html><body><div><table><thead>"
        "<tr><th colspan='10'><div>公布處置有價證券資訊 "
        "(115/08/01 至 115/08/07)</div></th></tr>"
        "<tr><th>編號</th><th>公布日期</th><th>證券代號</th><th>證券名稱</th>"
        "<th>累計</th><th>處置條件</th><th>處置起迄時間</th><th>處置措施</th>"
        "<th>處置內容</th><th>備註</th></tr>"
        "</thead><tbody>"
        "<tr><td>1</td><td>115/08/07</td><td>1101</td><td>測試水泥</td>"
        "<td>3</td><td>連續三次</td><td>115/08/07～115/08/13</td>"
        "<td>人工管制撮合</td><td>依交易所公告執行撮合作業</td><td></td></tr>"
        "</tbody></table></div></body></html>"
    )

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
        twse_disposition_html=disposition_html,
    )

    fake_scored = [
        ScoredStock(
            stock_id="1101",
            total_score=82.5,
            data_completeness=0.90,
            factor_scores={
                "liquidity": 90.0,
                "volume_price": 80.0,
                "momentum": 75.0,
                "institutional": 70.0,
                "fundamental": 85.0,
                "risk_quality": None,
            },
            risk_flags=("DISPOSITION_STOCK",),
        )
    ]

    with patch("app.jobs.daily_ranking.score_candidates", return_value=fake_scored):
        result = run(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )

    assert result == 0
    captured = capsys.readouterr()
    assert "🚨 目前處置：是" in captured.out
    assert "處置期間：2026/08/07～2026/08/13" in captured.out
    assert "處置原因：連續三次" in captured.out
    assert "處置措施：請依交易所該次公告為準" in captured.out
    # the full legal-text "處置內容" must never appear verbatim
    assert "依交易所公告執行撮合作業" not in captured.out


def test_run_treats_regulatory_source_failure_as_non_fatal(monkeypatch, caplog):
    """
    Core regression for this feature's failure policy: unlike Step 1c's
    P/E valuation check (which returns WAITING_FOR_DATA on a source
    failure), a regulatory source failure must NOT block the whole
    run — this data is display-only in v1, so there's no "cannot
    verify eligibility" reason to withhold the entire ranking. Uses a
    deliberately malformed TWSE attention response (missing the
    expected table structure) to trigger the failure path.
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
        twse_attention_html="<html><body>not a valid report table</body></html>",
    )

    with caplog.at_level("INFO", logger="daily_ranking"):
        result = run(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )

    # The run must still succeed overall (0), not fail (1) or wait (2).
    assert result == 0
    assert "TWSE announcement/notice fetch/parse failed" in caplog.text
    assert "twse_attention_ok=False" in caplog.text


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
    # 9 base snapshots: twse price + tpex price + twse valuation +
    # tpex valuation + twse attention + twse disposition + tpex
    # attention + tpex disposition + finmind stock_info (saved even
    # though its rows turned out unusable — "save raw, parse later").
    assert len(repository.saved) == 9


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
    assert feature.institutional_net_buy_3d_positive is True
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
    assert feature.institutional_net_buy_3d_positive is None


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
    assert (
        features[0].institutional_net_buy_3d_positive is None
    )  # same reason — no volume data to establish the 3-session window


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
    assert feature.institutional_net_buy_3d_positive is True
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


def test_build_stock_features_institutional_net_buy_3d_positive_is_false_when_negative():
    """近 3 個交易日累積買超為負（或加總 <= 0）時，
    institutional_net_buy_3d_positive 必須是 False，不是 None——
    這條路徑資料是齊全的，只是賣超，跟「資料不足」是完全不同的狀態，
    不能混為一談。"""
    from app.jobs.daily_ranking import build_stock_features

    candidates = [
        _make_candidate("1101", close="44.65", turnover="100000000", volume=3_000_000)
    ]
    client = FakeHistoryFinMindClient(
        rows_by_stock={"1101": _make_history_rows(20, close="100", volume="1000000")},
        institutional_rows_by_stock={
            "1101": _make_institutional_rows(
                5, start=dt.date(2026, 6, 16), buy=200, sell=1000
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

    assert len(features) == 1
    feature = features[0]
    # 5-day ratio factor is still computed (still real data, just negative)
    assert feature.institutional_net_buy_ratio_5d is not None
    assert feature.institutional_net_buy_ratio_5d < 0
    assert feature.institutional_net_buy_3d_positive is False


# --- Risk assessment (Step 2) ---


def test_build_stock_features_disposition_stock_allowed_by_default_but_flagged():
    """
    Regression test for the v1 "官方風控" rollout: RiskPolicy's default
    allow_disposition_stock=True means a disposition stock must NOT be
    silently dropped from features — it must still reach the report
    with a DISPOSITION_STOCK flag so the reader sees the official
    warning, not just a smaller ranking with no explanation. See
    RiskPolicyConfig.allow_disposition_stock's own docstring for why
    v1 defaults to NOT excluding, unlike attention stocks which were
    already display-only-by-default before this change.

    Regulatory status is supplied via regulatory_by_stock (the real
    integration path — see _resolve_regulatory_flags), not via
    _make_candidate's is_disposition kwarg: that kwarg only sets
    StockMaster.is_disposition, which build_stock_features's
    RiskPolicy.assess() call no longer reads (that field is always
    None in production — FinMind never populates it — the real
    signal now comes from regulatory_by_stock, populated by
    app.ingestion.regulatory_mapper / twse_regulatory_mapper).
    """
    from app.domain.models import RegulatoryRiskStatus
    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101")]
    client = FakeHistoryFinMindClient(rows_by_stock={"1101": _make_history_rows(20)})

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
        regulatory_by_stock={
            "1101": RegulatoryRiskStatus(
                trading_date=TARGET_DATE,
                stock_id="1101",
                is_disposition=True,
                disposition_reason="連續三次",
                disposition_measure="人工管制撮合",
            )
        },
        twse_attention_ok=True,
        twse_disposition_ok=True,
    )

    assert len(features) == 1
    assert "DISPOSITION_STOCK" in features[0].risk_flags
    # is_managed/consecutive_limit_up_days are still unknown for this
    # fixture, so risk_quality_raw correctly stays None rather than a
    # fabricated score — this test is about exclusion, not
    # completeness.
    assert features[0].risk_quality_raw is None


def test_build_stock_features_disposition_stock_excluded_when_policy_disallows():
    """The exclusion behavior still exists, just gated behind config
    now instead of being unconditional — see RiskPolicy.assess()."""
    from app.domain.models import RegulatoryRiskStatus
    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101")]
    client = FakeHistoryFinMindClient(rows_by_stock={"1101": _make_history_rows(20)})

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(RiskPolicyConfig(allow_disposition_stock=False)),
        regulatory_by_stock={
            "1101": RegulatoryRiskStatus(
                trading_date=TARGET_DATE, stock_id="1101", is_disposition=True
            )
        },
        twse_attention_ok=True,
        twse_disposition_ok=True,
    )

    assert features == []


def test_build_stock_features_regulatory_source_failure_leaves_flags_unknown_not_false():
    """
    The core regression this feature's "Unknown when API fails"
    requirement exists for: when a source's fetch/parse failed this
    run (twse_disposition_ok=False), a candidate absent from
    regulatory_by_stock must resolve to is_disposition=None
    (unconfirmed), which RiskPolicy.assess() tracks in
    missing_inputs — NOT is_disposition=False (which would silently
    claim "confirmed not under disposition" when the source was never
    actually successfully checked)."""
    from app.jobs.daily_ranking import build_stock_features

    candidates = [_make_candidate("1101")]
    client = FakeHistoryFinMindClient(rows_by_stock={"1101": _make_history_rows(20)})

    features = build_stock_features(
        candidates=candidates,
        target_date=TARGET_DATE,
        finmind_client=client,
        ingestion_run_id="run-1",
        risk_policy=RiskPolicy(),
        regulatory_by_stock={},
        twse_attention_ok=True,
        twse_disposition_ok=False,  # simulates a failed TWSE punish fetch
    )

    assert len(features) == 1
    assert "DISPOSITION_STOCK" not in features[0].risk_flags
    # risk_quality_raw stays None because is_disposition is Unknown
    # (in missing_inputs), the same observable proxy the existing
    # test_build_stock_features_unknown_status_never_yields_full_risk_quality
    # uses — StockFeatures doesn't expose missing_inputs directly.
    assert features[0].risk_quality_raw is None


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


def test_run_report_dry_run_off_by_default_prints_no_report(capsys, monkeypatch):
    """
    REPORT_DRY_RUN is opt-in. When unset, Step 4 must not print a
    rendered report or create any delivery side effect.
    """
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")
    monkeypatch.delenv("REPORT_DRY_RUN", raising=False)

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
    )

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )

    assert result == 0
    captured = capsys.readouterr()
    assert "REPORT_DRY_RUN preview" not in captured.out
    assert "NOT sent to LINE" not in captured.out


def test_run_report_dry_run_prints_no_qualified_report(capsys, monkeypatch, caplog):
    """
    With REPORT_DRY_RUN=true and a candidate that has no institutional/
    revenue data (as these fixtures produce, since make_all_clients()
    returns the same stock_info payload for every FinMind dataset),
    the stock falls below the completeness threshold, so the
    "no qualified stock" report path must be used — and it must still
    be printed, not skipped.
    """
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")
    monkeypatch.setenv("REPORT_DRY_RUN", "true")

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
    # Make the fixture assumption explicit: this test must genuinely
    # exercise the zero-eligible path, not accidentally pass some
    # other way if the fixture behavior ever changes.
    assert "eligible=0" in caplog.text

    captured = capsys.readouterr()
    assert "REPORT_DRY_RUN preview" in captured.out
    assert "NOT sent to LINE" in captured.out
    assert "進入候選池：1 檔" in captured.out
    assert "今日無符合資料完整度門檻的候選股" in captured.out
    assert "暫無 Top 10 名單" in captured.out
    assert "本清單依公開市場資料及固定量化規則產生" in captured.out
    assert "UTF-16 length:" in captured.out


def test_run_report_dry_run_prints_ranked_report(capsys, monkeypatch):
    """
    Positive path: when score_candidates() produces a stock that
    clears the completeness gate, the rendered report must actually
    show its rank/name/score/factors — not just fall through to the
    no-qualified path. Patches score_candidates() at the boundary
    since FinMind price/institutional/revenue enrichment is already
    covered by Steps 1-3's own tests; this test's job is only to
    verify ScoredStock -> select_top_n -> report_builder ->
    renderer -> stdout is wired correctly.

    text-v6 update: the per-stock "主要得分來源" section was replaced
    by the "訊號" block, which shows every factor as a 🟢/🟡/🔴/⚪ light
    instead of just the top 1-2 highest-scoring factor names — see
    text_renderer.py's _render_signal_lines. This test's fixture uses
    momentum=75.0 (>=70) and risk_flags=() (no HIGH_FIVE_DAY_RETURN),
    so the text-v7 momentum-wording change doesn't affect this
    particular assertion — it still renders the plain "強" word, not
    "漲多過熱".
    """
    from unittest.mock import patch

    from app.domain.scoring import ScoredStock

    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")
    monkeypatch.setenv("REPORT_DRY_RUN", "true")

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
    )

    fake_scored = [
        ScoredStock(
            stock_id="1101",
            total_score=82.5,
            data_completeness=0.90,
            factor_scores={
                "liquidity": 90.0,
                "volume_price": 80.0,
                "momentum": 75.0,
                "institutional": 70.0,
                "fundamental": 85.0,
                "risk_quality": None,
            },
            risk_flags=(),
        )
    ]

    with patch("app.jobs.daily_ranking.score_candidates", return_value=fake_scored):
        result = run(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )

    assert result == 0
    captured = capsys.readouterr()
    assert "REPORT_DRY_RUN preview" in captured.out
    assert "展示範圍：綜合分數 Top 10" in captured.out
    assert "1. 測試水泥（1101）" in captured.out
    assert "綜合分數：82.50" in captured.out
    assert "資料完整度：90%" in captured.out
    assert "訊號" in captured.out
    assert "🟢 流動性：強" in captured.out
    assert "🟢 基本面：強" in captured.out


def test_run_uses_ranking_limit_of_ten_not_five_in_pipeline_summary_log(
    caplog, monkeypatch
):
    """
    Regression test for the Top 5 -> Top 10 change itself: the final
    pipeline-summary log line must report RANKING_LIMIT's actual
    configured value (10), not a value hardcoded back down to 5. This
    is the one assertion in this file that would have caught a
    regression where RANKING_LIMIT was defined but never actually
    threaded through to select_top_n()/the log line.
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
    assert "in Top 10" in caplog.text
    assert "in Top 5" not in caplog.text


def test_run_line_live_push_off_by_default_does_not_touch_delivery(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")
    monkeypatch.delenv("LINE_LIVE_PUSH", raising=False)

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
    )

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )

    assert result == 0


def test_run_report_text_is_deterministic_for_same_inputs(capsys, monkeypatch):
    """
    Direct regression test for the idempotency prerequisite: two
    separate run() calls for the same trading_date/inputs must render
    byte-identical report text across a real wall-clock time gap — no
    timestamp or other execution-time-dependent value may leak into
    it, or DeliveryRepository.reserve() will raise
    DeliveryContentConflict on a same-day rerun instead of returning
    SKIPPED_ALREADY_SENT.
    """
    import time

    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")
    monkeypatch.setenv("REPORT_DRY_RUN", "true")

    def run_once():
        repository = InMemoryRawPayloadRepository()
        twse_client, tpex_client, finmind_client = make_all_clients(
            repository=repository,
            twse_csv=TWSE_CSV_LIMIT_UP,
            tpex_rows=TPEX_JSON_NON_LIMIT_UP,
            stock_info_data=_merged_stock_info(
                FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
            ),
        )
        run(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )
        return capsys.readouterr().out

    first_output = run_once()
    time.sleep(1.1)  # cross at least one wall-clock second boundary
    second_output = run_once()

    assert first_output == second_output


def test_run_line_delivery_mode_off_by_default_does_not_touch_delivery(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")
    monkeypatch.delenv("LINE_DELIVERY_MODE", raising=False)

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
    )

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )
    assert result == 0


def test_run_line_delivery_mode_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")
    monkeypatch.setenv("LINE_DELIVERY_MODE", "not-a-real-mode")

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
    )

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )
    assert result == 1


def test_run_line_delivery_mode_push_requires_target_id(monkeypatch):
    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")
    monkeypatch.setenv("LINE_DELIVERY_MODE", "push")
    monkeypatch.delenv("LINE_TARGET_ID", raising=False)

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
    )

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
    )
    assert result == 1


def test_run_line_delivery_mode_broadcast_does_not_require_target_id(
    monkeypatch, tmp_path
):
    """broadcast mode must NOT fail just because LINE_TARGET_ID is unset."""
    import httpx
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.clients.line_client import LineMessagingClient
    from app.db.delivery_repository import DeliveryRepository
    from app.db.models import MessageDelivery

    monkeypatch.setenv("TARGET_TRADING_DATE", "2026-08-07")
    monkeypatch.setenv("LINE_DELIVERY_MODE", "broadcast")
    monkeypatch.delenv("LINE_TARGET_ID", raising=False)

    db_path = tmp_path / "delivery_broadcast_test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    MessageDelivery.__table__.create(engine)

    call_log = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append(str(request.url))
        return httpx.Response(200, headers={"x-line-request-id": "req-1"})

    repository = InMemoryRawPayloadRepository()
    twse_client, tpex_client, finmind_client = make_all_clients(
        repository=repository,
        twse_csv=TWSE_CSV_LIMIT_UP,
        tpex_rows=TPEX_JSON_NON_LIMIT_UP,
        stock_info_data=_merged_stock_info(
            FINMIND_STOCK_INFO_LIMIT_UP, FINMIND_STOCK_INFO_TPEX_STOCK
        ),
    )
    session = Session(engine)
    line_client = LineMessagingClient(
        channel_access_token="fake-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        initial_backoff_seconds=0,
    )

    result = run(
        repository=repository,
        twse_client=twse_client,
        tpex_client=tpex_client,
        finmind_client=finmind_client,
        delivery_repository=DeliveryRepository(session),
        line_client=line_client,
    )

    assert result == 0
    assert len(call_log) == 1
    assert call_log[0].endswith("/v2/bot/message/broadcast")
