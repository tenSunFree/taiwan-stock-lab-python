"""
Market data ingestion skeleton.

Design principles:
    1. Every run creates an ingestion_run_id (a fresh one even on a
       same-day rerun — never overwrite a previous snapshot).
    2. Any response from any source is saved in full to
       raw_source_payloads before any cleaning, comparison, or
       discarding happens.
    3. FinMind is treated as the primary aggregated source; TWSE /
       TPEx / MOPS are treated as official cross-check sources. The
       actual available datasets, update timing, quota, and license
       terms should be verified against FinMind's docs at
       integration time — never hardcoded into application logic.

This file only provides interfaces and an httpx call skeleton; real
endpoints and parameters must be filled in according to the current
FinMind / TWSE OpenAPI / TPEx / MOPS public specs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


def new_ingestion_run_id(now: dt.datetime | None = None) -> str:
    """Example format: 20260807-161701-a82f"""
    now = now or dt.datetime.now()
    suffix = uuid.uuid4().hex[:4]
    return f"{now:%Y%m%d-%H%M%S}-{suffix}"


def payload_hash(raw_payload: Any) -> str:
    normalized = json.dumps(raw_payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RawSourcePayload:
    ingestion_run_id: str
    source: str  # "finmind" | "twse" | "tpex" | "mops"
    target_date: dt.date
    requested_at: dt.datetime
    source_updated_at: dt.datetime | None
    request_parameters: dict[str, Any]
    schema_version: str
    payload_hash: str
    raw_payload: Any
    ingested_at: dt.datetime


class RawPayloadRepository(Protocol):
    """Persists to the raw_source_payloads table; implementation lives
    in app/db."""

    def save(self, payload: RawSourcePayload) -> None: ...


class MarketDataClient:
    """
    Generic ingestion skeleton: call the API -> wrap into a
    RawSourcePayload -> hand off to the repository for persistence.
    Every subclass (FinMind / TWSE / TPEx / MOPS) must follow the rule
    "save the raw response first, parse it only afterwards."
    """

    source_name: str = "unknown"
    schema_version: str = "v1"

    def __init__(
        self, repository: RawPayloadRepository, http_client: httpx.Client | None = None
    ):
        self.repository = repository
        self.http_client = http_client or httpx.Client(timeout=30.0)

    def fetch_and_snapshot(
        self,
        *,
        ingestion_run_id: str,
        target_date: dt.date,
        request_parameters: dict[str, Any],
        fetch_fn,
    ) -> RawSourcePayload:
        """
        fetch_fn: () -> tuple[raw_payload: Any, source_updated_at: dt.datetime | None]
        Injected by each subclass (FinMindClient, etc.) to perform the
        actual API call.
        """
        requested_at = dt.datetime.now(dt.timezone.utc)
        raw_payload, source_updated_at = fetch_fn()

        payload = RawSourcePayload(
            ingestion_run_id=ingestion_run_id,
            source=self.source_name,
            target_date=target_date,
            requested_at=requested_at,
            source_updated_at=source_updated_at,
            request_parameters=request_parameters,
            schema_version=self.schema_version,
            payload_hash=payload_hash(raw_payload),
            raw_payload=raw_payload,
            ingested_at=dt.datetime.now(dt.timezone.utc),
        )
        self.repository.save(payload)
        return payload


class FinMindClient(MarketDataClient):
    source_name = "finmind"

    def __init__(self, repository: RawPayloadRepository, api_token: str, **kwargs):
        super().__init__(repository, **kwargs)
        self.api_token = api_token
        self.base_url = "https://api.finmindtrade.com/api/v4/data"

    def fetch_daily_price(
        self, *, ingestion_run_id: str, target_date: dt.date
    ) -> RawSourcePayload:
        params = {
            "dataset": "TaiwanStockPrice",
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
            "token": self.api_token,
        }

        def _fetch():
            resp = self.http_client.get(self.base_url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data, dt.datetime.now(dt.timezone.utc)

        return self.fetch_and_snapshot(
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
            request_parameters=params,
            fetch_fn=_fetch,
        )

    def fetch_stock_info(
        self, *, ingestion_run_id: str, target_date: dt.date
    ) -> RawSourcePayload:
        """
        Fetch the TaiwanStockInfo reference dataset.

        TaiwanStockInfo provides stock master metadata such as:
            - stock_id
            - stock_name
            - industry_category
            - type (market: twse / tpex / emerging)
            - date (source update date)

        Unlike TaiwanStockPrice, this dataset is not queried by a
        start/end trading-date range.

        target_date here is used only as ingestion bookkeeping — it
        identifies which ranking/ingestion run this raw snapshot
        belongs to. It must not be interpreted as the source
        dataset's own update date; each TaiwanStockInfo row carries
        its own `date` field in the response.
        """
        params = {
            "dataset": "TaiwanStockInfo",
            "token": self.api_token,
        }

        def _fetch():
            response = self.http_client.get(self.base_url, params=params)
            response.raise_for_status()
            data = response.json()
            return data, dt.datetime.now(dt.timezone.utc)

        return self.fetch_and_snapshot(
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
            request_parameters=params,
            fetch_fn=_fetch,
        )

    # Other datasets (institutional net buy/sell, margin/short balance,
    # monthly revenue, etc.) follow the same pattern, each mapped to
    # the corresponding FinMind dataset name — check FinMind's current
    # docs before implementing.


class TwseClient(MarketDataClient):
    source_name = "twse"
    # TODO: implement against the current TWSE OpenAPI spec (listed
    # daily closing quotes, limit-up/limit-down prices, etc.)


class TpexClient(MarketDataClient):
    source_name = "tpex"
    # TODO: implement against the current TPEx OpenAPI spec (OTC daily
    # closing quotes)


class MopsClient(MarketDataClient):
    source_name = "mops"
    # TODO: material information disclosures and monthly revenue
    # announcements — must record available_at (when the data actually
    # became available), not just the month it refers to, for use in
    # future backtesting (see the "event quality" factor).
