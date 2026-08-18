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
    # token is no longer sent as a query param — see
    # test_fetch_stock_info_sends_bearer_authorization_header and
    # test_fetch_stock_info_never_persists_api_token instead.


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


def test_fetch_daily_price_sends_bearer_authorization_header():
    captured_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"data": []})

    client, _ = make_client(handler)
    client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert captured_headers.get("authorization") == "Bearer fake-token"


def test_fetch_daily_price_params_never_contain_token_at_all():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client, repo = make_client(handler)
    client.fetch_daily_price(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert "token" not in repo.saved[0].request_parameters


def test_fetch_stock_info_sends_bearer_authorization_header():
    captured_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"data": []})

    client, _ = make_client(handler)
    client.fetch_stock_info(ingestion_run_id="run-1", target_date=TARGET_DATE)

    assert captured_headers.get("authorization") == "Bearer fake-token"


def test_fetch_stock_price_history_sends_stock_id_and_date_range():
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    client, _ = make_client(handler)
    client.fetch_stock_price_history(
        ingestion_run_id="run-1",
        stock_id="2330",
        start_date=dt.date(2026, 6, 1),
        end_date=dt.date(2026, 8, 12),
        target_date=dt.date(2026, 8, 13),
    )

    assert captured_params == {
        "dataset": "TaiwanStockPrice",
        "data_id": "2330",
        "start_date": "2026-06-01",
        "end_date": "2026-08-12",
    }


def test_fetch_stock_price_history_uses_bearer_header_not_url_token():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": []})

    client, _ = make_client(handler)
    client.fetch_stock_price_history(
        ingestion_run_id="run-1",
        stock_id="2330",
        start_date=dt.date(2026, 6, 1),
        end_date=dt.date(2026, 8, 12),
        target_date=dt.date(2026, 8, 13),
    )

    assert "token=" not in captured["url"]
    assert captured["authorization"] == "Bearer fake-token"


def test_fetch_stock_price_history_rejects_empty_stock_id():
    client, _ = make_client(lambda request: httpx.Response(200, json={"data": []}))
    with pytest.raises(ValueError):
        client.fetch_stock_price_history(
            ingestion_run_id="run-1",
            stock_id="",
            start_date=dt.date(2026, 6, 1),
            end_date=dt.date(2026, 8, 12),
            target_date=dt.date(2026, 8, 13),
        )


def test_fetch_stock_price_history_rejects_inverted_date_range():
    client, _ = make_client(lambda request: httpx.Response(200, json={"data": []}))
    with pytest.raises(ValueError):
        client.fetch_stock_price_history(
            ingestion_run_id="run-1",
            stock_id="2330",
            start_date=dt.date(2026, 8, 12),
            end_date=dt.date(2026, 6, 1),
            target_date=dt.date(2026, 8, 13),
        )


def test_fetch_stock_institutional_investors_sends_correct_params():
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    client, _ = make_client(handler)
    client.fetch_stock_institutional_investors(
        ingestion_run_id="run-1",
        stock_id="2330",
        start_date=dt.date(2026, 6, 1),
        end_date=dt.date(2026, 8, 12),
        target_date=dt.date(2026, 8, 13),
    )

    assert captured_params == {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
        "data_id": "2330",
        "start_date": "2026-06-01",
        "end_date": "2026-08-12",
    }


def test_fetch_stock_institutional_investors_uses_bearer_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": []})

    client, _ = make_client(handler)
    client.fetch_stock_institutional_investors(
        ingestion_run_id="run-1",
        stock_id="2330",
        start_date=dt.date(2026, 6, 1),
        end_date=dt.date(2026, 8, 12),
        target_date=dt.date(2026, 8, 13),
    )

    assert "token=" not in captured["url"]
    assert captured["authorization"] == "Bearer fake-token"


def test_fetch_stock_institutional_investors_rejects_inverted_date_range():
    client, repo = make_client(lambda request: httpx.Response(200, json={"data": []}))
    with pytest.raises(ValueError):
        client.fetch_stock_institutional_investors(
            ingestion_run_id="run-1",
            stock_id="2330",
            start_date=dt.date(2026, 8, 12),
            end_date=dt.date(2026, 6, 1),
            target_date=dt.date(2026, 8, 13),
        )
    assert repo.saved == []


def test_fetch_stock_institutional_investors_http_error_does_not_save():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server error"})

    client, repo = make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_stock_institutional_investors(
            ingestion_run_id="run-1",
            stock_id="2330",
            start_date=dt.date(2026, 6, 1),
            end_date=dt.date(2026, 8, 12),
            target_date=dt.date(2026, 8, 13),
        )
    assert repo.saved == []


def test_fetch_stock_monthly_revenue_sends_correct_params():
    captured_params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_params.update(dict(request.url.params))
        return httpx.Response(200, json={"data": []})

    client, repo = make_client(handler)
    client.fetch_stock_monthly_revenue(
        ingestion_run_id="run-1",
        stock_id="1101",
        start_date=dt.date(2025, 3, 1),
        end_date=dt.date(2026, 8, 14),
        target_date=dt.date(2026, 8, 15),
    )

    assert captured_params == {
        "dataset": "TaiwanStockMonthRevenue",
        "data_id": "1101",
        "start_date": "2025-03-01",
        "end_date": "2026-08-14",
    }
    assert repo.saved[0].request_parameters == captured_params


def test_fetch_stock_monthly_revenue_uses_bearer_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": []})

    client, _ = make_client(handler)
    client.fetch_stock_monthly_revenue(
        ingestion_run_id="run-1",
        stock_id="1101",
        start_date=dt.date(2025, 3, 1),
        end_date=dt.date(2026, 8, 14),
        target_date=dt.date(2026, 8, 15),
    )

    assert "token=" not in captured["url"]
    assert captured["authorization"] == "Bearer fake-token"


def test_fetch_stock_monthly_revenue_rejects_empty_stock_id():
    client, _ = make_client(lambda request: httpx.Response(200, json={"data": []}))
    with pytest.raises(ValueError):
        client.fetch_stock_monthly_revenue(
            ingestion_run_id="run-1",
            stock_id="",
            start_date=dt.date(2025, 3, 1),
            end_date=dt.date(2026, 8, 14),
            target_date=dt.date(2026, 8, 15),
        )


def test_fetch_stock_monthly_revenue_rejects_inverted_date_range():
    client, repo = make_client(lambda request: httpx.Response(200, json={"data": []}))
    with pytest.raises(ValueError):
        client.fetch_stock_monthly_revenue(
            ingestion_run_id="run-1",
            stock_id="1101",
            start_date=dt.date(2026, 8, 15),
            end_date=dt.date(2025, 1, 1),
            target_date=dt.date(2026, 8, 15),
        )
    assert repo.saved == []


def test_fetch_stock_monthly_revenue_http_error_does_not_save():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server error"})

    client, repo = make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_stock_monthly_revenue(
            ingestion_run_id="run-1",
            stock_id="1101",
            start_date=dt.date(2025, 3, 1),
            end_date=dt.date(2026, 8, 14),
            target_date=dt.date(2026, 8, 15),
        )
    assert repo.saved == []


def test_fetch_and_snapshot_retries_on_read_timeout_then_succeeds(monkeypatch):
    """
    Regression test tied to the real incident: TWSE's DNS-round-robin
    pool sometimes routes to an unhealthy node. A ReadTimeout on the
    first attempt(s) must not fail the whole call if a later attempt
    succeeds.
    """
    monkeypatch.setattr("app.ingestion.market_data_client.time.sleep", lambda _: None)

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise httpx.ReadTimeout("simulated timeout", request=request)
        return httpx.Response(200, json={"data": []})

    client, repo = make_client(handler)
    result = client.fetch_stock_info(
        ingestion_run_id="run-1", target_date=dt.date(2026, 8, 18)
    )

    assert call_count["n"] == 3
    assert len(repo.saved) == 1


def test_fetch_and_snapshot_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr("app.ingestion.market_data_client.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    client, repo = make_client(handler)
    with pytest.raises(httpx.ReadTimeout):
        client.fetch_stock_info(
            ingestion_run_id="run-1", target_date=dt.date(2026, 8, 18)
        )

    assert repo.saved == []


def test_fetch_and_snapshot_does_not_retry_http_error_status(monkeypatch):
    """
    HTTPStatusError (4xx/5xx) is a different failure mode from a
    timeout — the server explicitly responded with an error. This
    must fail immediately on the first attempt, not be retried.
    """
    monkeypatch.setattr("app.ingestion.market_data_client.time.sleep", lambda _: None)

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(500, json={"error": "server error"})

    client, repo = make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_stock_info(
            ingestion_run_id="run-1", target_date=dt.date(2026, 8, 18)
        )

    assert call_count["n"] == 1  # no retry for HTTP error status
    assert repo.saved == []
