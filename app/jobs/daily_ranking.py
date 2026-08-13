"""
Daily scheduled job entry point.

Checkpoint 2A (extended) + Step 8A: wires real market data from BOTH
TWSE (listed) and TPEx (OTC) through to CandidateBuilder, then
enriches each candidate with trailing historical price features from
FinMind. Deliberately stops after StockFeatures[] is built — no
RiskPolicy, scoring, or LINE delivery yet — so a wrong or incomplete
candidate pool / feature set can be diagnosed without the rest of the
pipeline in the way.

Data sources (see architecture discussion: FinMind's free tier
requires a per-stock data_id, making a whole-market daily scan
impractical without a paid tier):

    TWSE (TwseClient + twse_mapper)
        Today's full TWSE-listed (上市) market OHLC/volume/turnover,
        via the public STOCK_DAY_ALL open-data endpoint.
        reference_price is derived from the day's own 漲跌價差 (price
        change) field — no second day's data needs to be fetched.

    TPEx (TpexClient + tpex_mapper)
        Today's full TPEx-listed (上櫃) OTC market OHLC/volume/
        turnover, via the public tpex_mainboard_daily_close_quotes
        endpoint. reference_price is derived from Close - Change, the
        same provisional-fallback approach as TWSE.
        NextReferencePrice/NextLimitUp/NextLimitDown describe the
        NEXT trading session and are intentionally not used here —
        see tpex_mapper.py's module docstring.

    FinMind (FinMindClient.fetch_stock_info + finmind_mapper)
        Stock master metadata (stock_name, market, security_type) via
        TaiwanStockInfo. Still needed because neither TWSE's
        STOCK_DAY_ALL nor TPEx's daily close quotes classify
        instruments in a way this pipeline can rely on.

    FinMind (FinMindClient.fetch_stock_price_history, Step 8A)
        Per-candidate (at most MAXIMUM_CANDIDATES stocks) historical
        TaiwanStockPrice, used only for trading sessions strictly
        before target_date, to compute average_turnover_20d,
        volume_ratio_20d, return_5d, return_20d. Today's own
        close/volume/turnover come from the TWSE/TPEx candidate data,
        never from FinMind — FinMind's aggregation can lag behind the
        official exchange feeds on the same trading day.

KNOWN LIMITATIONS in this checkpoint:

    - Both TWSE's STOCK_DAY_ALL and TPEx's daily close quotes only
      return the latest available trading day, not an arbitrary
      historical date. Manually overriding TARGET_TRADING_DATE to a
      past date will reliably hit WAITING_FOR_DATA below — that is
      expected, not a bug, until real historical data sources are
      wired in.
    - reference_price on both markets remains provisional (see each
      mapper's module docstring for the exact caveats — ex-rights
      days, no-trade days, etc.).
    - If TPEx fails to fetch, has no usable rows, or its mapper
      filters out every row for target_date, the entire run fails
      rather than silently falling back to TWSE-only candidates (and
      vice versa) — an incomplete whole-market scan should never be
      treated as a complete one.
    - A single candidate's historical-price enrichment failing
      (fetch, parse, or compute) does NOT fail the whole run — that
      stock's technical factors simply stay None. This is
      deliberately different from the TWSE/TPEx whole-market failure
      policy above: one missing stock's history is ordinary
      incompleteness: an entire market's data being unavailable is
      not.
    - institutional_net_buy_ratio_5d (Step 8B), revenue_yoy (Step
      8C), and risk_quality_raw (Step 9) are still always None.
      Combined with the 25%+20%+15%=60% factor weight this checkpoint
      can supply, no candidate can reach the 80% minimum
      data_completeness scoring.py requires for the Top 5 — that is
      expected at this checkpoint, not a bug. RiskPolicy and scoring
      are not wired in yet regardless.
    - candidate thresholds (minimum_turnover, maximum_candidates) are
      hardcoded here to match config/strategy-v1.yaml rather than
      loaded from it — a config loader is separate follow-up work.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from decimal import Decimal

from app.domain.candidate_builder import Candidate, CandidateBuilder
from app.domain.feature_builder import build_price_features
from app.domain.features import StockFeatures
from app.ingestion.finmind_mapper import (
    build_historical_price_points,
    build_stock_master,
)
from app.ingestion.market_data_client import (
    FinMindClient,
    RawSourcePayload,
    TpexClient,
    TwseClient,
    new_ingestion_run_id,
)
from app.ingestion.tpex_mapper import build_daily_prices as build_tpex_daily_prices
from app.ingestion.tpex_mapper import (
    roc_date_to_gregorian as tpex_roc_date_to_gregorian,
)
from app.ingestion.trading_calendar import TradingCalendar
from app.ingestion.twse_mapper import build_daily_prices as build_twse_daily_prices
from app.ingestion.twse_mapper import parse_stock_day_all_csv
from app.ingestion.twse_mapper import (
    roc_date_to_gregorian as twse_roc_date_to_gregorian,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily_ranking")

# Provisional MVP thresholds, matching config/strategy-v1.yaml by
# hand for now — a real config loader is separate follow-up work.
MINIMUM_TURNOVER = Decimal("50000000")
MAXIMUM_CANDIDATES = 50

# Retrieval buffer only — historical factors still use the trailing
# 5/20 actual trading-day observations; 60 calendar days simply gives
# FinMind enough room to cover weekends and market holidays.
HISTORY_LOOKBACK_CALENDAR_DAYS = 60


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
    only — TWSE's CSV and TPEx's JSON-array responses are parsed via
    their own mappers instead, not this function)."""
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


def build_stock_features(
    *,
    candidates: list[Candidate],
    target_date: dt.date,
    finmind_client: FinMindClient,
    ingestion_run_id: str,
) -> list[StockFeatures]:
    """
    Enrich candidate stocks with trailing historical price features.

    Today's close / volume / turnover always come from the already
    validated TWSE / TPEx candidate data. FinMind is queried only for
    sessions strictly before target_date.

    Failure policy:
        A failure affecting one candidate's historical enrichment
        (fetch, parsing, or computation — the ENTIRE per-stock
        pipeline) does NOT fail the whole ranking job. That stock is
        retained with missing historical factors (None), letting
        scoring reflect the gap through data_completeness rather than
        crashing the batch.

        Missing core candidate data (today's close/volume/turnover)
        is different: CandidateBuilder should already have rejected
        such a record, so seeing it here indicates an internal
        invariant violation, not ordinary enrichment-data absence —
        that raises immediately rather than being silently patched.
    """
    if not candidates:
        logger.info("No candidates require historical-price enrichment")
        return []

    history_start_date = target_date - dt.timedelta(days=HISTORY_LOOKBACK_CALENDAR_DAYS)
    history_end_date = target_date - dt.timedelta(days=1)

    features: list[StockFeatures] = []
    success_count = 0
    empty_count = 0
    failure_count = 0

    for candidate in candidates:
        stock_id = candidate.stock.stock_id
        today_close = candidate.price.close_price
        today_volume = candidate.price.volume
        today_turnover = candidate.price.turnover

        if today_close is None or today_volume is None or today_turnover is None:
            raise RuntimeError(
                f"Candidate invariant violated for stock_id={stock_id}: "
                f"close, volume, and turnover must all be present "
                f"(CandidateBuilder should have excluded this record)"
            )

        average_turnover_20d = None
        volume_ratio_20d = None
        return_5d = None
        return_20d = None

        try:
            history_payload = finmind_client.fetch_stock_price_history(
                ingestion_run_id=ingestion_run_id,
                stock_id=stock_id,
                start_date=history_start_date,
                end_date=history_end_date,
                target_date=target_date,
            )
            history_rows = extract_data_rows(history_payload)

            if not history_rows:
                empty_count += 1
                logger.warning(
                    "FinMind history returned no usable rows for stock_id=%s; "
                    "technical factors remain None",
                    stock_id,
                )
            else:
                history_points = build_historical_price_points(history_rows)
                price_features = build_price_features(
                    target_date=target_date,
                    today_close=float(today_close),
                    today_volume=float(today_volume),
                    history=history_points,
                )
                average_turnover_20d = price_features.average_turnover_20d
                volume_ratio_20d = price_features.volume_ratio_20d
                return_5d = price_features.return_5d
                return_20d = price_features.return_20d
                success_count += 1

        except Exception:
            failure_count += 1
            logger.exception(
                "FinMind historical-price enrichment failed for stock_id=%s; "
                "technical factors remain None",
                stock_id,
            )

        stock_features = StockFeatures(
            stock_id=stock_id,
            turnover=float(today_turnover),
            average_turnover_20d=average_turnover_20d,
            volume_ratio_20d=volume_ratio_20d,
            return_5d=return_5d,
            return_20d=return_20d,
            institutional_net_buy_ratio_5d=None,  # Step 8B
            revenue_yoy=None,  # Step 8C
            risk_quality_raw=None,  # Step 9
        )
        features.append(stock_features)

        logger.info(
            "features stock_id=%s turnover=%s avg_turnover_20d=%s "
            "volume_ratio_20d=%s return_5d=%s return_20d=%s",
            stock_id,
            stock_features.turnover,
            stock_features.average_turnover_20d,
            stock_features.volume_ratio_20d,
            stock_features.return_5d,
            stock_features.return_20d,
        )

    logger.info(
        "Historical-price enrichment complete: candidates=%d success=%d empty=%d failed=%d",
        len(candidates),
        success_count,
        empty_count,
        failure_count,
    )
    return features


def _validate_shared_repository(
    *,
    repository: InMemoryRawPayloadRepository,
    twse_client: TwseClient | None,
    tpex_client: TpexClient | None,
    finmind_client: FinMindClient | None,
) -> None:
    """
    A caller could pass repository=A but hand run() a client that was
    actually built with a different repository (repository=B). The
    "repository must be provided" guard alone doesn't catch that — it
    only checks that *something* was passed, not that it's the same
    object every injected client is writing snapshots to.
    """
    for name, client in (
        ("twse_client", twse_client),
        ("tpex_client", tpex_client),
        ("finmind_client", finmind_client),
    ):
        if client is not None and client.repository is not repository:
            raise ValueError(
                f"{name} was constructed with a different repository than the "
                f"one passed to run(). All injected clients must share the same "
                f"repository instance, or raw-snapshot bookkeeping silently "
                f"splits across repositories."
            )


def run(
    *,
    repository: InMemoryRawPayloadRepository | None = None,
    twse_client: TwseClient | None = None,
    tpex_client: TpexClient | None = None,
    finmind_client: FinMindClient | None = None,
) -> int:
    if (
        twse_client is not None or tpex_client is not None or finmind_client is not None
    ) and repository is None:
        raise ValueError(
            "repository must be provided whenever twse_client, tpex_client, or "
            "finmind_client is injected, otherwise raw-snapshot bookkeeping (the "
            "'raw snapshots' count in the completion log) can silently under-count. "
            "Pass all injected clients together with repository, or none of them "
            "for production behavior."
        )

    if repository is not None:
        _validate_shared_repository(
            repository=repository,
            twse_client=twse_client,
            tpex_client=tpex_client,
            finmind_client=finmind_client,
        )

    target_date = resolve_target_date()
    calendar = TradingCalendar()  # production should use the DB-backed trading_calendar

    if not calendar.is_trading_day(target_date):
        logger.info("SKIPPED_NON_TRADING_DAY: %s", target_date)
        return 0

    ingestion_run_id = new_ingestion_run_id()
    logger.info("ingestion_run_id=%s target_date=%s", ingestion_run_id, target_date)

    # One repository per ingestion run — production uses this same
    # repository for all three providers so every raw source snapshot
    # belonging to the run is collected together.
    repo = repository or InMemoryRawPayloadRepository()

    if twse_client is None:
        twse_client = TwseClient(repo)

    if tpex_client is None:
        tpex_client = TpexClient(repo)

    if finmind_client is None:
        api_token = os.environ.get("FINMIND_TOKEN", "").strip()
        if not api_token:
            logger.error("FINMIND_TOKEN not set")
            return 1
        finmind_client = FinMindClient(repo, api_token=api_token)

    # --- Step 1a: today's whole-market price data from TWSE ---
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

    twse_reported_dates = {
        twse_roc_date_to_gregorian(row.get("日期", "")) for row in twse_rows
    }
    twse_reported_dates.discard(None)

    if target_date not in twse_reported_dates:
        logger.warning(
            "WAITING_FOR_DATA: TWSE has not returned price data for %s yet "
            "(reported_dates=%s; target_date may also be a past date this "
            "endpoint cannot serve)",
            target_date,
            sorted(twse_reported_dates),
        )
        return 2

    logger.info(
        "TWSE source ready: rows=%d reported_dates=%s",
        len(twse_rows),
        sorted(twse_reported_dates),
    )

    # --- Step 1b: today's whole-market price data from TPEx ---
    try:
        tpex_price_payload = tpex_client.fetch_daily_price(
            ingestion_run_id=ingestion_run_id, target_date=target_date
        )
    except Exception:
        logger.exception("TPEx fetch_daily_price failed")
        return 1

    if not isinstance(tpex_price_payload.raw_payload, list):
        logger.error(
            "TPEx daily close quotes returned unexpected raw payload type: %s",
            type(tpex_price_payload.raw_payload).__name__,
        )
        return 1

    tpex_rows = [row for row in tpex_price_payload.raw_payload if isinstance(row, dict)]

    if not tpex_rows:
        logger.warning(
            "WAITING_FOR_DATA: TPEx returned no usable daily close quote rows for %s",
            target_date,
        )
        return 2

    tpex_reported_dates = {
        tpex_roc_date_to_gregorian(row.get("Date", "")) for row in tpex_rows
    }
    tpex_reported_dates.discard(None)

    if target_date not in tpex_reported_dates:
        logger.warning(
            "WAITING_FOR_DATA: TPEx has not returned price data for %s yet "
            "(reported_dates=%s; target_date may also be a past date this "
            "endpoint cannot serve)",
            target_date,
            sorted(tpex_reported_dates),
        )
        return 2

    logger.info(
        "TPEx source ready: rows=%d reported_dates=%s",
        len(tpex_rows),
        sorted(tpex_reported_dates),
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
    twse_daily_prices = build_twse_daily_prices(target_date=target_date, rows=twse_rows)
    tpex_daily_prices = build_tpex_daily_prices(target_date=target_date, rows=tpex_rows)

    if not twse_daily_prices:
        logger.warning(
            "WAITING_FOR_DATA: TWSE mapper produced zero DailyPrice records for %s "
            "even though raw rows were present — treating as not-ready rather than "
            "silently proceeding with a TPEx-only pool",
            target_date,
        )
        return 2

    if not tpex_daily_prices:
        logger.warning(
            "WAITING_FOR_DATA: TPEx mapper produced zero DailyPrice records for %s "
            "even though raw rows were present — treating as not-ready rather than "
            "silently proceeding with a TWSE-only pool",
            target_date,
        )
        return 2

    daily_prices = twse_daily_prices + tpex_daily_prices

    logger.info(
        "mapped stock_master=%d daily_prices=%d (twse=%d, tpex=%d)",
        len(stock_master),
        len(daily_prices),
        len(twse_daily_prices),
        len(tpex_daily_prices),
    )

    if not stock_master:
        logger.error("FinMind mapper produced no StockMaster records")
        return 1

    # --- Step 4: candidate pool (whole market: TWSE + TPEx) ---
    candidate_builder = CandidateBuilder(
        minimum_turnover=MINIMUM_TURNOVER, maximum_candidates=MAXIMUM_CANDIDATES
    )
    candidates = candidate_builder.build(list(stock_master.values()), daily_prices)
    log_candidates(candidates)

    # --- Step 8A: per-candidate historical-price enrichment ---
    features = build_stock_features(
        candidates=candidates,
        target_date=target_date,
        finmind_client=finmind_client,
        ingestion_run_id=ingestion_run_id,
    )
    logger.info("Built StockFeatures for %d candidates", len(features))

    logger.info(
        "Provisional Phase 2A + Step 8A complete: %d raw snapshots, %d mapped stocks, "
        "%d prices, %d candidates, %d features built",
        len(repo.saved),
        len(stock_master),
        len(daily_prices),
        len(candidates),
        len(features),
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
