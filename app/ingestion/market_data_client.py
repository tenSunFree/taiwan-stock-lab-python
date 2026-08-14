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


def _redact_token(params: dict[str, Any]) -> dict[str, Any]:
    """Never persist the API token into raw_source_payloads — it's a
    secret, not request metadata worth keeping in a snapshot that may
    end up in backups, logs, or a database with broader read access
    than the application itself."""
    return {key: value for key, value in params.items() if key != "token"}


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

    def _auth_headers(self) -> dict[str, str]:
        """
        FinMind's own documentation (finmind.github.io/login/) sends
        the token as an Authorization: Bearer header, not a query
        parameter. Besides matching the officially documented usage,
        this also keeps the token out of request URLs — and therefore
        out of httpx's default request-URL logging, browser history,
        proxy logs, etc. — which query-string tokens are not.
        """
        return {"Authorization": f"Bearer {self.api_token}"}

    def fetch_daily_price(
        self, *, ingestion_run_id: str, target_date: dt.date
    ) -> RawSourcePayload:
        params = {
            "dataset": "TaiwanStockPrice",
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
        }

        def _fetch():
            resp = self.http_client.get(
                self.base_url, params=params, headers=self._auth_headers()
            )
            resp.raise_for_status()
            data = resp.json()
            # No documented dataset-level "last updated" timestamp
            # from FinMind — requested_at already records when we
            # called the API; don't invent a source_updated_at value.
            return data, None

        return self.fetch_and_snapshot(
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
            request_parameters=params,  # no longer contains a secret — see _auth_headers()
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
        params = {"dataset": "TaiwanStockInfo"}

        def _fetch():
            response = self.http_client.get(
                self.base_url, params=params, headers=self._auth_headers()
            )
            response.raise_for_status()
            data = response.json()
            # See fetch_daily_price(): no dataset-level update
            # timestamp is available from FinMind.
            return data, None

        return self.fetch_and_snapshot(
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
            request_parameters=params,  # no longer contains a secret
            fetch_fn=_fetch,
        )

    def fetch_stock_price_history(
        self,
        *,
        ingestion_run_id: str,
        stock_id: str,
        start_date: dt.date,
        end_date: dt.date,
        target_date: dt.date,
    ) -> RawSourcePayload:
        """
        Fetch historical TaiwanStockPrice rows for ONE stock
        (data_id=stock_id required by FinMind's free tier — see the
        400 Bad Request that motivated switching whole-market scans
        to TWSE/TPEx). Intentionally per-stock: this is only called
        after CandidateBuilder has narrowed the universe to at most
        50 candidates, keeping request volume well within FinMind's
        free-tier rate limit.

        target_date is ingestion bookkeeping only — the actual query
        range is start_date/end_date.
        """
        stock_id = stock_id.strip()
        if not stock_id:
            raise ValueError("stock_id must not be empty")
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date")

        params = {
            "dataset": "TaiwanStockPrice",
            "data_id": stock_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }

        def _fetch():
            response = self.http_client.get(
                self.base_url, params=params, headers=self._auth_headers()
            )
            response.raise_for_status()
            return response.json(), None

        return self.fetch_and_snapshot(
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
            request_parameters=params,
            fetch_fn=_fetch,
        )

    def fetch_stock_institutional_investors(
        self,
        *,
        ingestion_run_id: str,
        stock_id: str,
        start_date: dt.date,
        end_date: dt.date,
        target_date: dt.date,
    ) -> RawSourcePayload:
        """
        Fetch TaiwanStockInstitutionalInvestorsBuySell for one stock.

        NOTE: FinMind's institutional data updates around 20:00 on
        trading days, well after this project's scheduled run time —
        target_date's own row will never be present during a normal
        run. See app.domain.institutional_flow_builder's module
        docstring.
        """
        stock_id = stock_id.strip()
        if not stock_id:
            raise ValueError("stock_id must not be empty")
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date")

        params = {
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": stock_id,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }

        def _fetch():
            response = self.http_client.get(
                self.base_url, params=params, headers=self._auth_headers()
            )
            response.raise_for_status()
            return response.json(), None

        return self.fetch_and_snapshot(
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
            request_parameters=params,
            fetch_fn=_fetch,
        )

    # Other datasets (margin/short balance, monthly revenue, etc.)
    # follow the same pattern, each mapped to the corresponding
    # FinMind dataset name — check FinMind's current docs before
    # implementing.


class TwseClient(MarketDataClient):
    """
    TWSE full-market daily-price client.

    Uses TWSE's public STOCK_DAY_ALL open-data CSV endpoint
    (www.twse.com.tw/exchangeReport/...), not the separate
    openapi.twse.com.tw/v1 Swagger JSON API — the two are different
    systems, don't conflate them in future docs/code here.

    Important:
        This client is responsible only for fetching and snapshotting
        the raw source response. CSV parsing and conversion into
        DailyPrice domain models belong in app.ingestion.twse_mapper.
    """

    source_name = "twse"

    def __init__(self, repository: RawPayloadRepository, **kwargs) -> None:
        super().__init__(repository, **kwargs)
        self.stock_day_all_url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL"

    def fetch_daily_price(
        self, *, ingestion_run_id: str, target_date: dt.date
    ) -> RawSourcePayload:
        """
        Fetch TWSE STOCK_DAY_ALL.

        The endpoint returns the full TWSE-listed market's latest
        available daily OHLC / volume / turnover data as CSV. No API
        token required — unlike FinMindClient, this endpoint is
        public with no authentication.

        `target_date` is NOT sent to this endpoint. It is retained as
        ingestion bookkeeping and will later be verified against each
        row's own 日期 field by twse_mapper — so an old/stale TWSE
        response cannot silently be treated as data for an arbitrary
        requested historical date.
        """
        params = {"response": "open_data"}

        def _fetch():
            response = self.http_client.get(self.stock_day_all_url, params=params)
            response.raise_for_status()
            # Keep the source response untouched here. TWSE's
            # open_data representation is CSV text rather than
            # FinMind-style JSON — parsing happens only after the raw
            # snapshot boundary, inside twse_mapper.py.
            return response.text, None

        return self.fetch_and_snapshot(
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
            request_parameters=params,
            fetch_fn=_fetch,
        )


class TpexClient(MarketDataClient):
    """
    TPEx (Taipei Exchange / OTC market) full-market daily-price client.

    Uses TPEx's public tpex_mainboard_daily_close_quotes open-data
    JSON endpoint. No authentication required.

    Important:
        This client is responsible only for fetching and snapshotting
        the raw source response. JSON parsing and conversion into
        DailyPrice domain models belong in app.ingestion.tpex_mapper.
    """

    source_name = "tpex"

    def __init__(self, repository: RawPayloadRepository, **kwargs) -> None:
        super().__init__(repository, **kwargs)
        self.daily_close_quotes_url = (
            "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
        )

    def fetch_daily_price(
        self, *, ingestion_run_id: str, target_date: dt.date
    ) -> RawSourcePayload:
        """
        Fetch tpex_mainboard_daily_close_quotes.

        The endpoint returns the full TPEx-listed OTC market's latest
        available daily OHLC / volume / turnover data as a JSON array,
        with no authentication and no query parameters.

        `target_date` is NOT sent to this endpoint. It is retained as
        ingestion bookkeeping and will later be verified against each
        row's own Date field by tpex_mapper — so an old/stale TPEx
        response cannot silently be treated as data for an arbitrary
        requested historical date. This endpoint has the same "latest
        trading day only" limitation as TWSE's STOCK_DAY_ALL.
        """

        def _fetch():
            response = self.http_client.get(self.daily_close_quotes_url)
            response.raise_for_status()
            # Unlike TWSE's CSV response, TPEx returns JSON directly —
            # response.json() gives a list[dict] as-is. Parsing/row
            # extraction still happens only in tpex_mapper.py, not
            # here (same "save raw, parse later" rule as everywhere
            # else).
            return response.json(), None

        return self.fetch_and_snapshot(
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
            request_parameters={},
            fetch_fn=_fetch,
        )


class MopsClient(MarketDataClient):
    source_name = "mops"
    # TODO: material information disclosures and monthly revenue
    # announcements — must record available_at (when the data actually
    # became available), not just the month it refers to, for use in
    # future backtesting (see the "event quality" factor).
