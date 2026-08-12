import datetime as dt

import httpx
import pytest

from app.ingestion.market_data_client import RawSourcePayload, TpexClient

TARGET_DATE = dt.date(2026, 8, 12)

SAMPLE_JSON = [
    {
        "Date": "1150812",
        "SecuritiesCompanyCode": "006201",
        "CompanyName": "測試ETF",
        "Close": "45.21",
        "Change": "+1.41",
        "Open": "44.20",
        "High": "45.26",
        "Low": "44.20",
        "TradingShares": "508551",
        "TransactionAmount": "22841328",
        "NextReferencePrice": "45.21",
        "NextLimitUp": "49.73",
        "NextLimitDown": "40.69",
    }
]


class FakeRepository:
    def __init__(self) -> None:
        self.saved: list[RawSourcePayload] = []

    def save(self, payload: RawSourcePayload) -> None:
        self.saved.append(payload)


def make_client(handler) -> tuple[TpexClient, FakeRepository]:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    repository = FakeRepository()
    client = TpexClient(repository, http_client=http_client)
    return client, repository


def test_fetch_daily_price_uses_correct_tpex_endpoint():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["host"] = request.url.host
        captured["path"] = request.url.path
        return httpx.Response(200, json=SAMPLE_JSON)

    client, _ = make_client(handler)
    client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert captured["method"] == "GET"
    assert captured["host"] == "www.tpex.org.tw"
    assert captured["path"] == "/openapi/v1/tpex_mainboard_daily_close_quotes"


def test_fetch_daily_price_sends_no_query_params():
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json=SAMPLE_JSON)

    client, _ = make_client(handler)
    client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert captured_params == {}


def test_fetch_daily_price_saves_raw_json_as_is():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE_JSON)

    client, repository = make_client(handler)
    result = client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert len(repository.saved) == 1
    saved = repository.saved[0]
    assert saved.source == "tpex"
    assert saved.target_date == TARGET_DATE
    assert saved.raw_payload == SAMPLE_JSON
    assert result is saved


def test_fetch_daily_price_does_not_fabricate_source_updated_at():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE_JSON)

    client, repository = make_client(handler)
    client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert repository.saved[0].source_updated_at is None


def test_fetch_daily_price_raises_on_http_error_without_saving():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client, repository = make_client(handler)

    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert repository.saved == []
