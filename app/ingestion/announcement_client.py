"""
TWSE / TPEx 每日重大訊息 client. Reuses MarketDataClient so retry,
5xx/timeout policy, payload hashing and raw-snapshot-before-parse all
come for free (MopsClient in market_data_client.py is an empty stub;
this is its real implementation under the same source name "mops").
"""

from __future__ import annotations

import datetime as dt

from app.ingestion.market_data_client import MarketDataClient, RawSourcePayload

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"


class AnnouncementClient(MarketDataClient):
    source_name = "mops"
    schema_version = "v1"

    def __init__(self, repository, **kwargs) -> None:
        super().__init__(repository, **kwargs)
        self.twse_url = TWSE_URL
        self.tpex_url = TPEX_URL

    def _fetch(
        self, *, url: str, market: str, ingestion_run_id: str, target_date: dt.date
    ) -> RawSourcePayload:
        def _do():
            response = self.http_client.get(url)
            response.raise_for_status()
            return response.json(), None

        return self.fetch_and_snapshot(
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
            request_parameters={"market": market, "dataset": "material_information"},
            fetch_fn=_do,
        )

    def fetch_twse(
        self, *, ingestion_run_id: str, target_date: dt.date
    ) -> RawSourcePayload:
        return self._fetch(
            url=self.twse_url,
            market="TWSE",
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
        )

    def fetch_tpex(
        self, *, ingestion_run_id: str, target_date: dt.date
    ) -> RawSourcePayload:
        return self._fetch(
            url=self.tpex_url,
            market="TPEX",
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
        )
