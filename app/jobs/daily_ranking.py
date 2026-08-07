"""
Daily scheduled job entry point — Phase 1 version.

Currently covers only the Phase 1 scope from the requirements:
    trading-day check -> create ingestion_run_id -> call each data
    source and persist a snapshot -> data-readiness check (mark
    WAITING_FOR_DATA and stop if not ready).

Phase 2 onward (candidate pool / scoring / LLM / LINE) is
intentionally left out of this file for now — implementing everything
in one pass makes failures harder to localize.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys

from app.ingestion.market_data_client import FinMindClient, new_ingestion_run_id
from app.ingestion.trading_calendar import StaleDataError, TradingCalendar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily_ranking")


def resolve_target_date() -> dt.date:
    """
    Supports a manually specified trading_date (YYYY-MM-DD) via
    workflow_dispatch; otherwise defaults to today in Taiwan time.
    """
    override = os.environ.get("TARGET_TRADING_DATE")
    if override:
        return dt.date.fromisoformat(override)
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()


def run() -> int:
    target_date = resolve_target_date()
    calendar = TradingCalendar()  # production should use the DB-backed trading_calendar

    if not calendar.is_trading_day(target_date):
        logger.info("SKIPPED_NON_TRADING_DAY: %s", target_date)
        return 0

    # TODO(required before Phase 4 goes live): once a real PostgreSQL
    # repository is wired in, check ranking_runs / message_deliveries
    # here first:
    #   if repository.has_successful_ranking(target_date, strategy_version)
    #   and repository.has_successful_delivery(target_date, strategy_version):
    #       logger.info("ALREADY_COMPLETED, skipping this run")
    #       return 0
    # The three scheduled trigger times + the concurrency group in the
    # workflow only prevent "running at the same time" — they do not
    # prevent "this already succeeded earlier today, should it push
    # again?" Both checks are needed.

    ingestion_run_id = new_ingestion_run_id()
    logger.info("ingestion_run_id=%s target_date=%s", ingestion_run_id, target_date)

    # The repository below should be replaced with a real PostgreSQL
    # implementation (see app/db); using a minimal in-memory version
    # here first so the pipeline and tests can run end to end.
    from app.ingestion.market_data_client import RawSourcePayload

    class InMemoryRepo:
        def __init__(self):
            self.saved: list[RawSourcePayload] = []

        def save(self, payload):
            self.saved.append(payload)
            logger.info(
                "saved raw payload source=%s target_date=%s hash=%s",
                payload.source,
                payload.target_date,
                payload.payload_hash[:8],
            )

    repo = InMemoryRepo()

    api_token = os.environ.get("FINMIND_TOKEN", "")
    if not api_token:
        logger.error("FINMIND_TOKEN not set")
        return 1

    finmind = FinMindClient(repo, api_token=api_token)

    try:
        payload = finmind.fetch_daily_price(
            ingestion_run_id=ingestion_run_id, target_date=target_date
        )
    except Exception:
        logger.exception("FinMind fetch failed")
        return 1

    # --- data-readiness check ---
    # This only checks the reported data date for now; a full version
    # should also check field completeness and reasonable row counts,
    # and that both listed and OTC data have both arrived — to be
    # completed in a later Phase 1 iteration.
    reported_dates = {
        row.get("date")
        for row in payload.raw_payload.get("data", [])
        if isinstance(row, dict)
    }
    if reported_dates and target_date.isoformat() not in reported_dates:
        logger.warning(
            "WAITING_FOR_DATA: FinMind has not returned data for %s yet, may need a retry",
            target_date,
        )
        return 2  # lets the GitHub Actions workflow decide whether to retry based on exit code

    logger.info("Phase 1 ingestion complete, %d raw snapshots saved", len(repo.saved))
    return 0


if __name__ == "__main__":
    sys.exit(run())
