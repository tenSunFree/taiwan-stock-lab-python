import datetime as dt

import httpx
import pytest

from app.ingestion.announcement_client import AnnouncementClient
from app.ingestion.market_data_client import RawSourcePayload


class MemoryRepository:
    def __init__(self) -> None:
        self.saved: list[RawSourcePayload] = []

    def save(self, payload: RawSourcePayload) -> None:
        self.saved.append(payload)


def make_row(**kw):
    row = {
        "出表日期": "1151031",
        "發言日期": "1151030",
        "發言時間": "154210",
        "公司代號": "2352",
        "公司名稱": "佳世達",
        "主旨 ": "公告本公司董事會決議配息",
        "符合條款": "第51款",
        "事實發生日": "1151030",
        "說明": "董事會通過",
    }
    row.update(kw)
    return row


def test_client_snapshots_raw_payload_before_parse():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[make_row()])

    repo = MemoryRepository()
    client = AnnouncementClient(
        repo, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    payload = client.fetch_twse(
        ingestion_run_id="r1", target_date=dt.date(2026, 10, 31)
    )
    assert repo.saved == [payload] and payload.source == "mops"
    assert payload.request_parameters["market"] == "TWSE"


def test_client_4xx_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    client = AnnouncementClient(
        MemoryRepository(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.fetch_tpex(ingestion_run_id="r", target_date=dt.date(2026, 10, 31))
    assert len(calls) == 1
