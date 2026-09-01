import datetime as dt

import httpx
import pytest

from app.ingestion.eps_mapper import build_raw_cumulative_eps_points
from app.ingestion.financial_statement_client import (
    FinancialStatementClient,
    parse_tpex_csv_rows,
    parse_twse_json_rows,
)
from app.ingestion.market_data_client import RawSourcePayload

TARGET_DATE = dt.date(2026, 8, 31)

SAMPLE_TWSE_JSON = [
    {
        "出表日期": "1150831",
        "公司代號": "2330",
        "公司名稱": "台積電",
        "年度": "114",
        "季別": "2",
        "基本每股盈餘（元）": "34.50",
    },
]

# Real TPEx exports from mopsfin.twse.com.tw are UTF-8 WITH a leading
# BOM — reproduced here verbatim (the "\ufeff" below IS the BOM, not a
# stand-in for it) so the parser is tested against the actual failure
# mode, not an idealized clean CSV.
SAMPLE_TPEX_CSV = (
    "\ufeff出表日期,公司代號,公司名稱,年度,季別,基本每股盈餘（元）\n"
    "1150831,1240,茂生農經,114,2,1.23\n"
    "\n"
)


class FakeRepository:
    def __init__(self) -> None:
        self.saved: list[RawSourcePayload] = []

    def save(self, payload: RawSourcePayload) -> None:
        self.saved.append(payload)


def make_client(handler) -> tuple[FinancialStatementClient, FakeRepository]:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    repository = FakeRepository()
    client = FinancialStatementClient(repository, http_client=http_client)
    return client, repository


# --- fetch_twse_financial_statement -----------------------------------------


def test_fetch_twse_financial_statement_uses_correct_endpoint():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["host"] = request.url.host
        captured["path"] = request.url.path
        return httpx.Response(200, json=SAMPLE_TWSE_JSON)

    client, _ = make_client(handler)
    client.fetch_twse_financial_statement(
        ingestion_run_id="run-1", target_date=TARGET_DATE
    )

    assert captured["method"] == "GET"
    assert captured["host"] == "openapi.twse.com.tw"
    assert captured["path"] == "/v1/opendata/t187ap06_L_ci"


def test_fetch_twse_financial_statement_sends_no_query_params():
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json=SAMPLE_TWSE_JSON)

    client, _ = make_client(handler)
    client.fetch_twse_financial_statement(
        ingestion_run_id="run-1", target_date=TARGET_DATE
    )

    assert captured_params == {}


def test_fetch_twse_financial_statement_saves_raw_json_as_is():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE_TWSE_JSON)

    client, repository = make_client(handler)
    result = client.fetch_twse_financial_statement(
        ingestion_run_id="run-1", target_date=TARGET_DATE
    )

    assert len(repository.saved) == 1
    saved = repository.saved[0]
    assert saved.source == "financial_statement"
    assert saved.target_date == TARGET_DATE
    assert saved.raw_payload == SAMPLE_TWSE_JSON
    assert saved.request_parameters == {"market": "twse", "dataset": "t187ap06_L_ci"}
    assert result is saved


def test_fetch_twse_financial_statement_does_not_fabricate_source_updated_at():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE_TWSE_JSON)

    client, repository = make_client(handler)
    client.fetch_twse_financial_statement(
        ingestion_run_id="run-1", target_date=TARGET_DATE
    )

    assert repository.saved[0].source_updated_at is None


def test_fetch_twse_financial_statement_raises_on_http_error_without_saving():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client, repository = make_client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_twse_financial_statement(
            ingestion_run_id="run-1", target_date=TARGET_DATE
        )

    assert repository.saved == []


def test_fetch_twse_financial_statement_retries_bounded_on_timeout():
    """Exercises the shared MarketDataClient.fetch_and_snapshot retry
    path (bounded to 3 attempts, timeouts only) through this client's
    concrete endpoint, rather than re-asserting the generic behaviour
    already covered by test_market_data_client.py."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ReadTimeout("simulated timeout", request=request)

    client, repository = make_client(handler)

    with pytest.raises(httpx.ReadTimeout):
        client.fetch_twse_financial_statement(
            ingestion_run_id="run-1", target_date=TARGET_DATE
        )

    assert attempts["count"] == 3
    assert repository.saved == []


# --- fetch_tpex_financial_statement ------------------------------------------


def test_fetch_tpex_financial_statement_uses_correct_endpoint():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["host"] = request.url.host
        captured["path"] = request.url.path
        return httpx.Response(200, text=SAMPLE_TPEX_CSV)

    client, _ = make_client(handler)
    client.fetch_tpex_financial_statement(
        ingestion_run_id="run-1", target_date=TARGET_DATE
    )

    assert captured["method"] == "GET"
    assert captured["host"] == "mopsfin.twse.com.tw"
    assert captured["path"] == "/opendata/t187ap06_O_ci"


def test_fetch_tpex_financial_statement_saves_raw_csv_text_bom_included():
    """The BOM must survive into the RAW snapshot untouched — stripping
    it is parse_tpex_csv_rows's job, not the fetch step's, so the
    exact bytes TPEx sent stay recoverable from raw_source_payloads."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_TPEX_CSV)

    client, repository = make_client(handler)
    result = client.fetch_tpex_financial_statement(
        ingestion_run_id="run-1", target_date=TARGET_DATE
    )

    saved = repository.saved[0]
    assert saved.source == "financial_statement"
    assert saved.raw_payload == SAMPLE_TPEX_CSV
    assert saved.raw_payload.startswith("\ufeff")
    assert saved.request_parameters == {"market": "tpex", "dataset": "t187ap06_O_ci"}
    assert result is saved


def test_fetch_tpex_financial_statement_raises_on_http_error_without_saving():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client, repository = make_client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_tpex_financial_statement(
            ingestion_run_id="run-1", target_date=TARGET_DATE
        )

    assert repository.saved == []


# --- parse_twse_json_rows ----------------------------------------------------


def test_parse_twse_json_rows_passes_through_dict_rows():
    assert parse_twse_json_rows(SAMPLE_TWSE_JSON) == SAMPLE_TWSE_JSON


def test_parse_twse_json_rows_drops_non_dict_elements():
    assert parse_twse_json_rows([{"a": "1"}, "not-a-dict", 123, None]) == [{"a": "1"}]


def test_parse_twse_json_rows_returns_empty_list_for_non_list_payload():
    assert parse_twse_json_rows({"error": "unexpected envelope"}) == []
    assert parse_twse_json_rows(None) == []
    assert parse_twse_json_rows("not even json-ish") == []


def test_parse_twse_json_rows_can_be_fed_directly_to_eps_mapper():
    rows = parse_twse_json_rows(SAMPLE_TWSE_JSON)
    points = build_raw_cumulative_eps_points(rows=rows)

    assert len(points) == 1
    assert points[0].stock_id == "2330"
    assert points[0].fiscal_year == 2025
    assert points[0].quarter == 2
    assert points[0].cumulative_eps == 34.50
    assert points[0].batch_report_date == dt.date(2026, 8, 31)


# --- parse_tpex_csv_rows ------------------------------------------------------


def test_parse_tpex_csv_rows_real_payload_strips_bom():
    rows = parse_tpex_csv_rows(SAMPLE_TPEX_CSV)

    assert len(rows) == 1
    # The critical assertion: the BOM must NOT have leaked into the
    # first header's key. If it had, this key would be
    # "\ufeff出表日期" instead, and eps_mapper would treat every row
    # as missing 出表日期 and silently drop the whole batch.
    assert "出表日期" in rows[0]
    assert "\ufeff出表日期" not in rows[0]
    assert rows[0]["出表日期"] == "1150831"


def test_parse_tpex_csv_rows_drops_trailing_blank_lines():
    rows = parse_tpex_csv_rows(SAMPLE_TPEX_CSV)
    assert len(rows) == 1  # the trailing blank line is not a phantom row


def test_parse_tpex_csv_rows_returns_empty_list_for_empty_payload():
    assert parse_tpex_csv_rows("") == []


def test_parse_tpex_csv_rows_coalesces_ragged_row_none_values_to_empty_string():
    csv_text = "\ufeffa,b,c\n1,2\n"  # short row: c is missing -> None from DictReader
    rows = parse_tpex_csv_rows(csv_text)

    assert len(rows) == 1
    assert rows[0] == {"a": "1", "b": "2", "c": ""}
    assert None not in rows[0]


def test_parse_tpex_csv_rows_can_be_fed_directly_to_eps_mapper():
    rows = parse_tpex_csv_rows(SAMPLE_TPEX_CSV)
    points = build_raw_cumulative_eps_points(rows=rows)

    assert len(points) == 1
    assert points[0].stock_id == "1240"
    assert points[0].fiscal_year == 2025
    assert points[0].quarter == 2
    assert points[0].cumulative_eps == 1.23
    assert points[0].batch_report_date == dt.date(2026, 8, 31)
