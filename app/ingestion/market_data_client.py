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
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger("market_data_client")


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

        Transient read/connect timeouts to TWSE/TPEx are a known,
        recurring condition: their public endpoints are served by a
        DNS round-robin pool of backend nodes, and it's common for a
        subset of those nodes to be temporarily unhealthy while
        others respond normally within milliseconds (confirmed by
        testing all 8 www.twse.com.tw IPs directly — 6 timed out, 2
        responded in ~0.06s). A retry gives the next attempt a chance
        to land on a healthy node via DNS re-resolution, rather than
        being permanently blocked by one bad node.

        Deliberately scoped to ReadTimeout/ConnectTimeout only — an
        HTTP error status (4xx/5xx, raised as HTTPStatusError by each
        subclass's response.raise_for_status()) is a DIFFERENT
        failure mode: the request reached the server and was
        explicitly rejected. Retrying that blindly could hammer a
        server that's already signaling a problem, so it is
        intentionally NOT retried here.
        """
        MAX_FETCH_ATTEMPTS = 3
        RETRY_BACKOFF_BASE_SECONDS = 2.0

        requested_at = dt.datetime.now(dt.timezone.utc)

        raw_payload = None
        source_updated_at = None
        last_timeout_error: httpx.TimeoutException | None = None

        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            try:
                raw_payload, source_updated_at = fetch_fn()
                last_timeout_error = None
                break
            except (httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
                last_timeout_error = exc
                if attempt == MAX_FETCH_ATTEMPTS:
                    break
                wait_seconds = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    "source=%s attempt=%d/%d timed out (%s); retrying in %.0fs",
                    self.source_name,
                    attempt,
                    MAX_FETCH_ATTEMPTS,
                    type(exc).__name__,
                    wait_seconds,
                )
                time.sleep(wait_seconds)

        if last_timeout_error is not None:
            logger.error(
                "source=%s all %d attempts timed out; giving up",
                self.source_name,
                MAX_FETCH_ATTEMPTS,
            )
            raise last_timeout_error

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

    def fetch_stock_monthly_revenue(
        self,
        *,
        ingestion_run_id: str,
        stock_id: str,
        start_date: dt.date,
        end_date: dt.date,
        target_date: dt.date,
    ) -> RawSourcePayload:
        """
        Fetch TaiwanStockMonthRevenue for one stock.

        Taiwan-listed companies must disclose monthly revenue by the
        10th of the following month, so target_date's own month is
        typically NOT yet disclosed when this job runs. start_date/
        end_date should span well over a year back so both the latest
        disclosed month and the same month a year earlier are present
        in the response — but the real look-ahead guard is
        available_at (derived from create_time) in
        app.domain.monthly_revenue_builder, not this query window.
        """
        stock_id = stock_id.strip()
        if not stock_id:
            raise ValueError("stock_id must not be empty")
        if start_date > end_date:
            raise ValueError("start_date must not be after end_date")

        params = {
            "dataset": "TaiwanStockMonthRevenue",
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

    # Other datasets (margin/short balance, etc.) follow the same
    # pattern, each mapped to the corresponding FinMind dataset name —
    # check FinMind's current docs before implementing.


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
        self.valuation_url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"

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

    def fetch_valuation(
        self, *, ingestion_run_id: str, target_date: dt.date
    ) -> RawSourcePayload:
        """
        Fetch TWSE's BWIBBU_ALL (個股日本益比、殖利率及股價淨值比).

        Deliberately a DIFFERENT TWSE system from fetch_daily_price()'s
        www.twse.com.tw/exchangeReport/... open-data CSV endpoint — see
        this class's own docstring warning about not conflating the two.
        BWIBBU_ALL lives on openapi.twse.com.tw/v1, TWSE's newer Swagger
        JSON API: no auth, no query params, whole-market JSON response.

        Verified response shape (each element of the JSON array):
            {"Date": "1150819", "Code": "2330", "Name": "台積電",
             "PEratio": "23.45", "DividendYield": "1.92",
             "PBratio": "7.11"}
        Date is the ROC-calendar trading date the ratios were computed
        for — same "latest trading day only" limitation as
        fetch_daily_price(), so the mapper must verify it against
        target_date the same way twse_mapper does for daily prices.

        request_parameters is a self-describing marker rather than
        real query params (this endpoint takes none) — the point is
        purely to make snapshots distinguishable from
        fetch_daily_price()'s {"response": "open_data"} snapshots,
        since both share source="twse".
        """
        params = {"dataset": "BWIBBU_ALL"}

        def _fetch():
            response = self.http_client.get(self.valuation_url)
            response.raise_for_status()
            return response.json(), None

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
        self.peratio_analysis_url = (
            "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
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

    def fetch_valuation(
        self, *, ingestion_run_id: str, target_date: dt.date
    ) -> RawSourcePayload:
        """
        Fetch TPEx's tpex_mainboard_peratio_analysis (個股本益比、殖利率
        及股價淨值比). No auth, no query params.

        Verified response shape (re-confirmed via a raw HTTP body dump
        with Invoke-WebRequest -UseBasicParsing, bypassing
        PowerShell's Invoke-RestMethod / ConvertTo-Json, both of which
        can silently reshape a bare array's on-the-wire JSON into
        something that LOOKS like an OData-style {"value": [...],
        "Count": N} envelope even when the server never sent one — an
        earlier version of this docstring was wrong for exactly this
        reason): a BARE JSON array, same shape as
        fetch_daily_price()'s:
            [{"Date": "1150821", "SecuritiesCompanyCode": "1240",
              "CompanyName": "...", "PriceEarningRatio": "10.59",
              "DividendPerShare": "0.50000000", "YieldRatio": "0.88",
              "PriceBookRatio": "1.68"}, ...]
        PriceEarningRatio is the literal string "N/A" (not absent, not
        null) when the source has no valid P/E to report — same
        "zero/negative trailing EPS" meaning as TWSE's "-".
        Date is ROC-calendar. Unlike fetch_daily_price()'s Date, this
        endpoint's Date is not guaranteed to equal target_date even on
        a healthy day — see app.ingestion.valuation_mapper's module
        docstring for how the mapper handles that.
        """
        params = {"dataset": "tpex_mainboard_peratio_analysis"}

        def _fetch():
            response = self.http_client.get(self.peratio_analysis_url)
            response.raise_for_status()
            return response.json(), None

        return self.fetch_and_snapshot(
            ingestion_run_id=ingestion_run_id,
            target_date=target_date,
            request_parameters=params,
            fetch_fn=_fetch,
        )


class MopsClient(MarketDataClient):
    source_name = "mops"
    # TODO: material information disclosures and monthly revenue
    # announcements — must record available_at (when the data actually
    # became available), not just the month it refers to, for use in
    # future backtesting (see the "event quality" factor).
