import datetime as dt

import httpx

from app.ingestion.announcement_client import AnnouncementClient
from app.jobs.news_spike import MemoryRepository, run
from app.news.models import TAIPEI_TZ

NOW = dt.datetime(2026, 10, 31, 20, 0, tzinfo=TAIPEI_TZ)


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


def test_spike_survives_one_source_failing(capsys):
    def handler(request):
        if "twse" in str(request.url):
            return httpx.Response(200, json=[make_row()])
        return httpx.Response(404)

    client = AnnouncementClient(
        MemoryRepository(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert run(client, NOW) == 1  # TPEx failed, TWSE still reported
    out = capsys.readouterr().out
    assert "mapped: 1" in out and "FETCH FAILED" in out


def test_spike_does_not_crash_when_first_row_is_not_an_object(capsys):
    def handler(request):
        if "twse" in str(request.url):
            return httpx.Response(200, json=["junk", make_row()])
        return httpx.Response(200, json=[make_row()])

    client = AnnouncementClient(
        MemoryRepository(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert run(client, NOW) == 0  # both markets reported
    out = capsys.readouterr().out
    assert out.count("columns:") == 2 and "dropped: 1" in out
