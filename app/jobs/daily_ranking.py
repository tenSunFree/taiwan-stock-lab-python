"""
Daily scheduled job entry point.

Checkpoint 2A: wires real FinMind data through to CandidateBuilder
only — deliberately stops before RiskPolicy/scoring/report so that if
the candidate pool comes back empty or wrong, the debugging surface
is limited to data ingestion and mapping, not the whole pipeline.

KNOWN LIMITATIONS in this checkpoint:

    - reference_price is approximated from the previous trading day's
      close (see app/ingestion/finmind_mapper.py). Wrong on
      ex-rights/ex-dividend days.
    - "Previous trading day" is found by querying FinMind itself and
      walking backward until a date with actual price rows is found —
      this correctly skips weekends AND holidays without needing a
      real holiday calendar, but does spend extra API calls doing so.
    - candidate thresholds (minimum_turnover, maximum_candidates) are
      hardcoded here to match config/strategy-v1.yaml rather than
      loaded from it — a config loader is a separate piece of work,
      not bundled into this checkpoint.

RiskPolicy, StockFeatures, scoring, and LINE delivery are NOT wired in
yet — see Step 2B/2C/2D/2E in the project's working notes.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from decimal import Decimal

from app.domain.candidate_builder import Candidate, CandidateBuilder
from app.ingestion.finmind_mapper import build_daily_prices, build_stock_master
from app.ingestion.market_data_client import (
    FinMindClient,
    RawSourcePayload,
    new_ingestion_run_id,
)
from app.ingestion.trading_calendar import TradingCalendar

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
    """Defensively extract FinMind's raw `data` rows. The raw response
    itself stays untouched in RawSourcePayload; this only extracts
    rows for downstream mapping."""
    if not isinstance(payload.raw_payload, dict):
        return []
    rows = payload.raw_payload.get("data")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def fetch_previous_trading_day_price(
    *,
    finmind: FinMindClient,
    ingestion_run_id: str,
    target_date: dt.date,
    maximum_lookback_days: int = 10,
) -> tuple[dt.date, RawSourcePayload]:
    """
    Find the immediately preceding date FinMind actually has
    TaiwanStockPrice rows for, by querying FinMind itself and walking
    backward — not by assuming target_date - 1 day, which would be
    wrong across weekends and holidays. TradingCalendar isn't
    production-grade yet, so using the source data directly is safer
    for this checkpoint.
    """
    for days_back in range(1, maximum_lookback_days + 1):
        candidate_date = target_date - dt.timedelta(days=days_back)
        payload = finmind.fetch_daily_price(
            ingestion_run_id=ingestion_run_id, target_date=candidate_date
        )
        rows = extract_data_rows(payload)

        if any(row.get("date") == candidate_date.isoformat() for row in rows):
            return candidate_date, payload

        logger.info(
            "No FinMind price data for previous-date candidate %s; looking further back",
            candidate_date,
        )

    raise RuntimeError(
        f"Could not find a previous trading day with FinMind price data "
        f"within {maximum_lookback_days} calendar days before {target_date}"
    )


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


def run() -> int:
    target_date = resolve_target_date()
    calendar = TradingCalendar()  # production should use the DB-backed trading_calendar

    if not calendar.is_trading_day(target_date):
        logger.info("SKIPPED_NON_TRADING_DAY: %s", target_date)
        return 0

    ingestion_run_id = new_ingestion_run_id()
    logger.info("ingestion_run_id=%s target_date=%s", ingestion_run_id, target_date)

    api_token = os.environ.get("FINMIND_TOKEN", "")
    if not api_token:
        logger.error("FINMIND_TOKEN not set")
        return 1

    repo = InMemoryRawPayloadRepository()
    finmind = FinMindClient(repo, api_token=api_token)

    # --- Step 1: today's price data + readiness check ---
    try:
        today_price_payload = finmind.fetch_daily_price(
            ingestion_run_id=ingestion_run_id, target_date=target_date
        )
    except Exception:
        logger.exception("FinMind fetch_daily_price failed")
        return 1

    today_rows = extract_data_rows(today_price_payload)
    reported_dates = {row.get("date") for row in today_rows}

    # NOTE: no "reported_dates and ..." guard here — if today_rows is
    # completely empty, reported_dates is an empty set, and we still
    # need this to correctly report WAITING_FOR_DATA rather than
    # silently proceeding with zero rows.
    if target_date.isoformat() not in reported_dates:
        logger.warning(
            "WAITING_FOR_DATA: FinMind has not returned price data for %s yet",
            target_date,
        )
        return 2

    # --- Step 2: stock master reference data ---
    try:
        stock_info_payload = finmind.fetch_stock_info(
            ingestion_run_id=ingestion_run_id, target_date=target_date
        )
    except Exception:
        logger.exception("FinMind fetch_stock_info failed")
        return 1

    stock_info_rows = extract_data_rows(stock_info_payload)
    if not stock_info_rows:
        logger.error("TaiwanStockInfo returned no usable rows")
        return 1

    # --- Step 3: find the real previous trading day ---
    try:
        previous_date, previous_price_payload = fetch_previous_trading_day_price(
            finmind=finmind, ingestion_run_id=ingestion_run_id, target_date=target_date
        )
    except Exception:
        logger.exception("Could not resolve previous trading day")
        return 1

    previous_rows = extract_data_rows(previous_price_payload)
    logger.info(
        "resolved previous_trading_day=%s for target_date=%s",
        previous_date,
        target_date,
    )

    # --- Step 4: map to domain models ---
    stock_master = build_stock_master(stock_info_rows)
    daily_prices = build_daily_prices(
        target_date=target_date, today_rows=today_rows, previous_day_rows=previous_rows
    )
    logger.info(
        "mapped stock_master=%d daily_prices=%d", len(stock_master), len(daily_prices)
    )

    # --- Step 5: candidate pool ---
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
