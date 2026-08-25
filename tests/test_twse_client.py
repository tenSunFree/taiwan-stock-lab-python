import datetime as dt

import httpx
import pytest

from app.ingestion.market_data_client import RawSourcePayload, TwseClient

TARGET_DATE = dt.date(2026, 8, 7)

SAMPLE_CSV = (
    "日期,證券代號,證券名稱,成交股數,成交金額,"
    "開盤價,最高價,最低價,收盤價,漲跌價差,成交筆數\n"
    '"1150807","2330","台積電","24414025","57947015347",'
    '"2390.00","2395.00","2355.00","2370.00","5.0000","64670"\n'
)


class FakeRepository:
    def __init__(self) -> None:
        self.saved: list[RawSourcePayload] = []

    def save(self, payload: RawSourcePayload) -> None:
        self.saved.append(payload)


def make_client(handler) -> tuple[TwseClient, FakeRepository]:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    repository = FakeRepository()
    client = TwseClient(repository, http_client=http_client)
    return client, repository


def test_fetch_daily_price_uses_correct_twse_endpoint():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["host"] = request.url.host
        captured["path"] = request.url.path
        return httpx.Response(200, text=SAMPLE_CSV)

    client, _ = make_client(handler)
    client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert captured["method"] == "GET"
    assert captured["host"] == "www.twse.com.tw"
    assert captured["path"] == "/exchangeReport/STOCK_DAY_ALL"


def test_fetch_daily_price_sends_open_data_response_param():
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, text=SAMPLE_CSV)

    client, _ = make_client(handler)
    client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert captured_params == {"response": "open_data"}


def test_fetch_daily_price_does_not_send_target_date():
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, text=SAMPLE_CSV)

    client, _ = make_client(handler)
    client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert "date" not in captured_params
    assert "start_date" not in captured_params
    assert "end_date" not in captured_params
    assert TARGET_DATE.isoformat() not in captured_params.values()


def test_fetch_daily_price_saves_raw_csv_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_CSV)

    client, repository = make_client(handler)
    result = client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert len(repository.saved) == 1
    saved = repository.saved[0]
    assert saved.source == "twse"
    assert saved.target_date == TARGET_DATE
    assert saved.raw_payload == SAMPLE_CSV
    assert result is saved


def test_fetch_daily_price_saves_safe_request_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_CSV)

    client, repository = make_client(handler)
    client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    saved = repository.saved[0]
    assert saved.request_parameters == {"response": "open_data"}


def test_fetch_daily_price_does_not_fabricate_source_updated_at():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_CSV)

    client, repository = make_client(handler)
    client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert repository.saved[0].source_updated_at is None


def test_fetch_daily_price_hash_is_stable_for_same_csv():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_CSV)

    client_1, repository_1 = make_client(handler)
    client_1.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    client_2, repository_2 = make_client(handler)
    client_2.fetch_daily_price(ingestion_run_id="run-2", target_date=TARGET_DATE)

    assert repository_1.saved[0].payload_hash == repository_2.saved[0].payload_hash


def test_fetch_daily_price_raises_on_http_error_without_saving():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client, repository = make_client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert repository.saved == []


# --- fetch_valuation (BWIBBU_ALL) -------------------------------------------

SAMPLE_VALUATION_JSON = [
    {
        "Date": "1150807",
        "Code": "2330",
        "Name": "台積電",
        "PEratio": "23.45",
        "DividendYield": "1.92",
        "PBratio": "7.11",
    },
]


def test_fetch_valuation_uses_correct_twse_endpoint():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["host"] = request.url.host
        captured["path"] = request.url.path
        return httpx.Response(200, json=SAMPLE_VALUATION_JSON)

    client, _ = make_client(handler)
    client.fetch_valuation(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert captured["method"] == "GET"
    assert captured["host"] == "openapi.twse.com.tw"
    assert captured["path"] == "/v1/exchangeReport/BWIBBU_ALL"


def test_fetch_valuation_sends_no_query_params():
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json=SAMPLE_VALUATION_JSON)

    client, _ = make_client(handler)
    client.fetch_valuation(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert captured_params == {}


def test_fetch_valuation_saves_raw_json_as_is():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE_VALUATION_JSON)

    client, repository = make_client(handler)
    result = client.fetch_valuation(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert len(repository.saved) == 1
    saved = repository.saved[0]
    assert saved.source == "twse"
    assert saved.target_date == TARGET_DATE
    assert saved.raw_payload == SAMPLE_VALUATION_JSON
    assert result is saved


def test_fetch_valuation_request_parameters_are_distinguishable_from_daily_price():
    """Regression test for the source="twse" ambiguity between
    fetch_daily_price() and fetch_valuation(): both share source, but
    request_parameters must differ so a raw snapshot is identifiable
    without guessing from raw_payload shape."""

    def price_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_CSV)

    def valuation_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE_VALUATION_JSON)

    price_client, price_repo = make_client(price_handler)
    price_client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    valuation_client, valuation_repo = make_client(valuation_handler)
    valuation_client.fetch_valuation(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert (
        price_repo.saved[0].request_parameters
        != valuation_repo.saved[0].request_parameters
    )
    assert valuation_repo.saved[0].request_parameters == {"dataset": "BWIBBU_ALL"}


def test_fetch_valuation_does_not_fabricate_source_updated_at():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE_VALUATION_JSON)

    client, repository = make_client(handler)
    client.fetch_valuation(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert repository.saved[0].source_updated_at is None


def test_fetch_valuation_raises_on_http_error_without_saving():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client, repository = make_client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_valuation(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert repository.saved == []


# --- fetch_attention / fetch_disposition (announcement/notice, announcement/punish) ---

SAMPLE_NOTICE_HTML = "<html><body>notice placeholder</body></html>"
SAMPLE_PUNISH_HTML = "<html><body>punish placeholder</body></html>"


def test_fetch_attention_uses_correct_twse_endpoint_and_response_param():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["host"] = request.url.host
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, text=SAMPLE_NOTICE_HTML)

    client, _ = make_client(handler)
    client.fetch_attention(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert captured["method"] == "GET"
    assert captured["host"] == "www.twse.com.tw"
    assert captured["path"] == "/announcement/notice"
    assert captured["params"] == {"response": "html"}


def test_fetch_attention_saves_raw_html_text_as_is():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_NOTICE_HTML)

    client, repository = make_client(handler)
    result = client.fetch_attention(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert len(repository.saved) == 1
    saved = repository.saved[0]
    assert saved.source == "twse"
    assert saved.raw_payload == SAMPLE_NOTICE_HTML
    assert isinstance(saved.raw_payload, str)
    assert result is saved


def test_fetch_attention_raises_on_http_error_without_saving():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client, repository = make_client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_attention(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert repository.saved == []


def test_fetch_disposition_uses_correct_twse_endpoint_and_response_param():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, text=SAMPLE_PUNISH_HTML)

    client, _ = make_client(handler)
    client.fetch_disposition(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert captured["path"] == "/announcement/punish"
    assert captured["params"] == {"response": "html"}


def test_fetch_disposition_saves_raw_html_text_as_is():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_PUNISH_HTML)

    client, repository = make_client(handler)
    result = client.fetch_disposition(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert len(repository.saved) == 1
    saved = repository.saved[0]
    assert saved.source == "twse"
    assert saved.raw_payload == SAMPLE_PUNISH_HTML
    assert result is saved


def test_fetch_disposition_raises_on_http_error_without_saving():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client, repository = make_client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_disposition(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert repository.saved == []


def test_fetch_attention_and_fetch_disposition_use_distinguishable_request_parameters():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html></html>")

    attention_client, attention_repo = make_client(handler)
    attention_client.fetch_attention(ingestion_run_id="run-1", target_date=TARGET_DATE)

    disposition_client, disposition_repo = make_client(handler)
    disposition_client.fetch_disposition(
        ingestion_run_id="run-1", target_date=TARGET_DATE
    )

    assert (
        attention_repo.saved[0].request_parameters
        != disposition_repo.saved[0].request_parameters
    )
