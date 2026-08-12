"""
Daily scheduled job entry point.

Checkpoint 2A: wires real market data through to CandidateBuilder
only — deliberately stops before RiskPolicy/scoring/report so that if
the candidate pool comes back empty or wrong, the debugging surface
is limited to data ingestion and mapping, not the whole pipeline.

Data sources (see architecture discussion: FinMind's free tier
requires a per-stock data_id, making a whole-market daily scan
impractical without a paid tier):

    TWSE (TwseClient + twse_mapper)
        Today's full TWSE-listed market OHLC/volume/turnover, via the
        public STOCK_DAY_ALL open-data endpoint. reference_price is
        derived from the day's own 漲跌價差 (price change) field, so
        — unlike the FinMind-based approach this replaces — NO second
        day's data needs to be fetched at all.

    FinMind (FinMindClient.fetch_stock_info + finmind_mapper)
        Stock master metadata (stock_name, market, security_type) via
        TaiwanStockInfo. Still needed because TWSE's STOCK_DAY_ALL
        doesn't classify instruments or give market (TWSE vs TPEx).

KNOWN LIMITATIONS in this checkpoint:

    - TWSE's STOCK_DAY_ALL only returns the latest available trading
      day, not an arbitrary historical date (see twse_mapper.py's
      module docstring). Manually overriding TARGET_TRADING_DATE to a
      past date will reliably hit WAITING_FOR_DATA below — that is
      expected, not a bug, until a real historical TWSE data source is
      wired in.
    - TPEx (上櫃) stocks are not covered yet — only TWSE-listed (上市)
      instruments. See Roadmap: Step 6 (TpexClient + tpex_mapper).
    - candidate thresholds (minimum_turnover, maximum_candidates) are
      hardcoded here to match config/strategy-v1.yaml rather than
      loaded from it — a config loader is separate follow-up work.

RiskPolicy, StockFeatures, scoring, and LINE delivery are NOT wired in
yet.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from decimal import Decimal

from app.domain.candidate_builder import Candidate, CandidateBuilder
from app.ingestion.finmind_mapper import build_stock_master
from app.ingestion.market_data_client import (
    FinMindClient,
    RawSourcePayload,
    TwseClient,
    new_ingestion_run_id,
)
from app.ingestion.trading_calendar import TradingCalendar
from app.ingestion.twse_mapper import (
    build_daily_prices,
    parse_stock_day_all_csv,
    roc_date_to_gregorian,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily_ranking")

# Provisional MVP thresholds, matching config/strategy-v1.yaml by
# hand for now — a real config loader is separate follow-up work.
MINIMUM_TURNOVER = Decimal("50000000")
MAXIMUM_CANDIDATES = 50


class InMemoryRawPayloadRepository:
    """
    Temporary raw-snapshot repository used by the provisional E2E
    pipeline. Replace with the PostgreSQL-backed implementation
    before production.
    """

    def __init__(self) -> None:
        self.saved: list[RawSourcePayload] = []

    def save(self, payload: RawSourcePayload) -> None:
        self.saved.append(payload)
        logger.info(
            "saved raw payload source=%s target_date=%s hash=%s",
            payload.source,
            payload.target_date,
            payload.payload_hash[:8],
        )


def resolve_target_date() -> dt.date:
    override = os.environ.get("TARGET_TRADING_DATE")
    if override:
        return dt.date.fromisoformat(override)
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()


def extract_data_rows(payload: RawSourcePayload) -> list[dict]:
    """Defensively extract FinMind's raw `data` rows (JSON responses
    only — TWSE's CSV responses are parsed via
    twse_mapper.parse_stock_day_all_csv instead, not this function)."""
    if not isinstance(payload.raw_payload, dict):
        return []
    rows = payload.raw_payload.get("data")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def log_candidates(candidates: list[Candidate]) -> None:
    logger.info("CandidateBuilder produced %d provisional candidates", len(candidates))
    for index, candidate in enumerate(candidates, start=1):
        logger.info(
            "candidate=%02d stock_id=%s name=%s market=%s close=%s "
            "reference(provisional)=%s limit_up(provisional)=%s turnover=%s source=%s",
            index,
            candidate.stock.stock_id,
            candidate.stock.stock_name,
            candidate.stock.market.value,
            candidate.price.close_price,
            candidate.price.reference_price,
            candidate.limit_up.limit_up_price,
            candidate.price.turnover,
            candidate.limit_up.limit_up_source.value,
        )


def run(
    *,
    repository: InMemoryRawPayloadRepository | None = None,
    twse_client: TwseClient | None = None,
    finmind_client: FinMindClient | None = None,
) -> int:
    target_date = resolve_target_date()
    calendar = TradingCalendar()  # production should use the DB-backed trading_calendar

    if not calendar.is_trading_day(target_date):
        logger.info("SKIPPED_NON_TRADING_DAY: %s", target_date)
        return 0

    ingestion_run_id = new_ingestion_run_id()
    logger.info("ingestion_run_id=%s target_date=%s", ingestion_run_id, target_date)

    # One repository per ingestion run — production uses this same
    # repository for both providers so every raw source snapshot
    # belonging to the run is collected together.
    repo = repository or InMemoryRawPayloadRepository()

    if twse_client is None:
        twse_client = TwseClient(repo)

    if finmind_client is None:
        api_token = os.environ.get("FINMIND_TOKEN", "").strip()
        if not api_token:
            logger.error("FINMIND_TOKEN not set")
            return 1
        finmind_client = FinMindClient(repo, api_token=api_token)

    # --- Step 1: today's whole-market price data from TWSE ---
    try:
        twse_price_payload = twse_client.fetch_daily_price(
            ingestion_run_id=ingestion_run_id, target_date=target_date
        )
    except Exception:
        logger.exception("TWSE fetch_daily_price failed")
        return 1

    if not isinstance(twse_price_payload.raw_payload, str):
        logger.error(
            "TWSE STOCK_DAY_ALL returned unexpected raw payload type: %s",
            type(twse_price_payload.raw_payload).__name__,
        )
        return 1

    twse_rows = parse_stock_day_all_csv(twse_price_payload.raw_payload)

    if not twse_rows:
        logger.warning(
            "WAITING_FOR_DATA: TWSE returned no usable STOCK_DAY_ALL rows for %s",
            target_date,
        )
        return 2

    # STOCK_DAY_ALL only exposes the latest available trading day.
    # Never allow that response to silently masquerade as an
    # arbitrary TARGET_TRADING_DATE.
    reported_dates = {roc_date_to_gregorian(row.get("日期", "")) for row in twse_rows}
    reported_dates.discard(None)

    if target_date not in reported_dates:
        logger.warning(
            "WAITING_FOR_DATA: TWSE has not returned price data for %s yet "
            "(reported_dates=%s; target_date may also be a past date this "
            "endpoint cannot serve)",
            target_date,
            sorted(reported_dates),
        )
        return 2

    logger.info(
        "TWSE source ready: rows=%d reported_dates=%s",
        len(twse_rows),
        sorted(reported_dates),
    )

    # --- Step 2: stock master reference data from FinMind ---
    try:
        stock_info_payload = finmind_client.fetch_stock_info(
            ingestion_run_id=ingestion_run_id, target_date=target_date
        )
    except Exception:
        logger.exception("FinMind fetch_stock_info failed")
        return 1

    stock_info_rows = extract_data_rows(stock_info_payload)
    if not stock_info_rows:
        logger.error("TaiwanStockInfo returned no usable rows")
        return 1

    logger.info("FinMind stock info ready: rows=%d", len(stock_info_rows))

    # --- Step 3: map source rows into domain models ---
    stock_master = build_stock_master(stock_info_rows)
    daily_prices = build_daily_prices(target_date=target_date, rows=twse_rows)
    logger.info(
        "mapped stock_master=%d daily_prices=%d (TWSE-listed only, TPEx not yet wired in)",
        len(stock_master),
        len(daily_prices),
    )

    if not stock_master:
        logger.error("FinMind mapper produced no StockMaster records")
        return 1

    if not daily_prices:
        logger.error("TWSE mapper produced no DailyPrice records")
        return 1

    # --- Step 4: candidate pool ---
    candidate_builder = CandidateBuilder(
        minimum_turnover=MINIMUM_TURNOVER, maximum_candidates=MAXIMUM_CANDIDATES
    )
    candidates = candidate_builder.build(list(stock_master.values()), daily_prices)
    log_candidates(candidates)

    logger.info(
        "Provisional Phase 2A complete: %d raw snapshots, %d mapped stocks, "
        "%d prices, %d candidates",
        len(repo.saved),
        len(stock_master),
        len(daily_prices),
        len(candidates),
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
