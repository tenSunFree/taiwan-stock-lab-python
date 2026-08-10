import datetime as dt

import httpx
import pytest

from app.ingestion.market_data_client import FinMindClient, RawSourcePayload
from app.jobs.daily_ranking import fetch_previous_trading_day_price


class FakeRepository:
    def __init__(self) -> None:
        self.saved: list[RawSourcePayload] = []

    def save(self, payload: RawSourcePayload) -> None:
        self.saved.append(payload)


def test_finds_previous_trading_day_across_a_long_holiday_closure():
    """
    Regression test for the 2025 Chinese New Year closure: TWSE was
    closed 2025-01-23 through 2025-02-02 (11 calendar days), reopening
    2025-02-03. The previous trading day for 2025-02-03 is 2025-01-22,
    12 calendar days earlier — this must not raise even with the
    default maximum_lookback_days.
    """
    target_date = dt.date(2025, 2, 3)
    actual_previous_trading_day = dt.date(2025, 1, 22)

    def handler(request: httpx.Request) -> httpx.Response:
        queried_date = dict(request.url.params).get("start_date")
        if queried_date == actual_previous_trading_day.isoformat():
            return httpx.Response(
                200,
                json={"data": [{"date": queried_date, "stock_id": "2330"}]},
            )
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    repo = FakeRepository()
    finmind = FinMindClient(repo, api_token="fake-token", http_client=http_client)

    found_date, payload = fetch_previous_trading_day_price(
        finmind=finmind, ingestion_run_id="run-1", target_date=target_date
    )

    assert found_date == actual_previous_trading_day


def test_raises_when_no_trading_day_found_within_lookback_window():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    repo = FakeRepository()
    finmind = FinMindClient(repo, api_token="fake-token", http_client=http_client)

    with pytest.raises(RuntimeError):
        fetch_previous_trading_day_price(
            finmind=finmind,
            ingestion_run_id="run-1",
            target_date=dt.date(2026, 8, 7),
            maximum_lookback_days=3,  # deliberately too small, to confirm the error path still works
        )
