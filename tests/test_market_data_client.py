import datetime as dt

import httpx
import pytest

from app.ingestion.market_data_client import FinMindClient, RawSourcePayload

TARGET_DATE = dt.date(2026, 8, 7)


class FakeRepository:
    """In-memory stand-in for the real raw_source_payloads repository,
    just to verify save() is actually called with the right payload."""

    def __init__(self) -> None:
        self.saved: list[RawSourcePayload] = []

    def save(self, payload: RawSourcePayload) -> None:
        self.saved.append(payload)


def make_client(handler) -> tuple[FinMindClient, FakeRepository]:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    repo = FakeRepository()
    client = FinMindClient(repo, api_token="fake-token", http_client=http_client)
    return client, repo


def test_fetch_stock_info_sends_correct_dataset_param():
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    client, _ = make_client(handler)
    client.fetch_stock_info(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert captured_params["dataset"] == "TaiwanStockInfo"
    assert captured_params["token"] == "fake-token"


def test_fetch_stock_info_does_not_send_date_range_params():
    """TaiwanStockInfo is a reference dataset, not queried by
    start_date/end_date — unlike fetch_daily_price()."""
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    client, _ = make_client(handler)
    client.fetch_stock_info(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert "start_date" not in captured_params
    assert "end_date" not in captured_params


def test_fetch_stock_info_saves_full_raw_response_before_parsing():
    fake_response_data = {
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

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fake_response_data)

    client, repo = make_client(handler)
    result = client.fetch_stock_info(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert len(repo.saved) == 1
    saved = repo.saved[0]
    assert saved.source == "finmind"
    assert saved.target_date == TARGET_DATE
    assert saved.raw_payload == fake_response_data
    assert result is saved


def test_fetch_stock_info_target_date_is_bookkeeping_not_a_query_param():
    """target_date must land in RawSourcePayload for run tracking, but
    must never leak into the actual HTTP request parameters — the
    dataset itself has no date-range query."""
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    client, repo = make_client(handler)
    client.fetch_stock_info(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert repo.saved[0].target_date == TARGET_DATE
    assert TARGET_DATE.isoformat() not in captured_params.values()


def test_fetch_stock_info_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client, _ = make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_stock_info(ingestion_run_id="run-1", target_date=TARGET_DATE)


def test_fetch_stock_info_never_persists_api_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client, repo = make_client(handler)
    client.fetch_stock_info(ingestion_run_id="run-1", target_date=TARGET_DATE)

    saved_params = repo.saved[0].request_parameters
    assert "token" not in saved_params
    assert "fake-token" not in saved_params.values()


def test_fetch_stock_info_does_not_fabricate_source_updated_at():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client, repo = make_client(handler)
    client.fetch_stock_info(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert repo.saved[0].source_updated_at is None


def test_fetch_daily_price_never_persists_api_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client, repo = make_client(handler)
    client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    saved_params = repo.saved[0].request_parameters
    assert "token" not in saved_params
    assert "fake-token" not in saved_params.values()
