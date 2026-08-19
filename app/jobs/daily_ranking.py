"""
Daily scheduled job entry point.

Wires real market data from BOTH TWSE (listed) and TPEx (OTC) through
to CandidateBuilder, then enriches each candidate with trailing
historical price features, institutional net-buy ratio, and monthly
revenue YoY from FinMind, then applies RiskPolicy and multi-factor
scoring to select a research-only Top 5. When REPORT_DRY_RUN is
enabled, the selected results are adapted into the existing report
view model and rendered as LINE-compatible text, then printed to
stdout for manual inspection — no LINE call, no DB write.
LINE_DELIVERY_MODE controls real delivery through DeliveryService:
"off" (default) does nothing; "push" sends to a single LINE_TARGET_ID
(for testing a report-format change without notifying every
subscriber); "broadcast" sends to every friend of this Official
Account (the real daily delivery to family/subscribers). Both live
modes reserve a DB row before calling LINE, with idempotency
guaranteed by MessageDelivery's UNIQUE constraint (see
app.db.delivery_repository's module docstring): a rerun for the same
trading_date/strategy_version/delivery-scope/message_version is
safely skipped rather than sending a duplicate message. Report
content must therefore be deterministic for the same inputs — see
DATA_UPDATED_LABEL below.

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

    FinMind (FinMindClient.fetch_stock_price_history)
        Per-candidate (at most MAXIMUM_CANDIDATES stocks) historical
        TaiwanStockPrice, used only for trading sessions strictly
        before target_date, to compute average_turnover_20d,
        volume_ratio_20d, return_5d, return_20d. Today's own
        close/volume/turnover come from the TWSE/TPEx candidate data,
        never from FinMind — FinMind's aggregation can lag behind the
        official exchange feeds on the same trading day.

    FinMind (FinMindClient.fetch_stock_institutional_investors)
        Per-candidate historical TaiwanStockInstitutionalInvestorsBuySell,
        used to compute institutional_net_buy_ratio_5d (trailing
        5-session net institutional buy shares / total trading
        volume shares, reusing the volume data already fetched for
        price-history enrichment). FinMind's institutional data
        updates ~20:00 on trading days — target_date's own row is
        never available when this job runs on its normal schedule.

    FinMind (FinMindClient.fetch_stock_monthly_revenue)
        Per-candidate TaiwanStockMonthRevenue history, used to compute
        revenue_yoy from the newest revenue month known to have been
        available as of target_date and the same month one year
        earlier. create_time is carried into
        MonthlyRevenuePoint.available_at so current-period revenue is
        never accepted solely because its revenue month has already
        ended — see app.domain.monthly_revenue_builder's module
        docstring for the full look-ahead rationale.

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
    - A single candidate's historical-price, institutional-flow, or
      monthly-revenue enrichment failing does NOT fail the whole run
      — that stock's corresponding factor(s) simply stay None. All
      three enrichment types fail INDEPENDENTLY of each other (see
      build_stock_features() docstring): a failure in one must never
      clear factors already computed by the others for the same
      stock. This is deliberately different from the TWSE/TPEx
      whole-market failure policy above: one missing stock's
      history/flow/revenue is ordinary incompleteness; an entire
      market's data being unavailable is not.
    - RiskPolicy's is_attention/is_disposition/is_managed inputs and
      consecutive_limit_up_days are always None (unknown) — no
      wired-in data source exists for the former, and no reliable
      historical reference-price source exists for the latter (see
      app.domain.risk_inputs's module docstring). Consequently
      risk_quality_raw is always None too (see
      app.domain.risk_policy.build_risk_quality_raw's docstring for
      why "no flags raised" must not be scored as "confirmed clean"
      when the underlying inputs were never checked). A warning is
      logged on every build_stock_features() call to keep this gap
      visible rather than silently assumed fixed.
    - candidate thresholds (minimum_turnover, maximum_candidates) are
      hardcoded here to match config/strategy-v1.yaml rather than
      loaded from it — a config loader is separate follow-up work.
"""

from __future__ import annotations

import truststore

truststore.inject_into_ssl()

import datetime as dt
import logging
import os
import sys
from decimal import Decimal

from app.domain.candidate_builder import Candidate, CandidateBuilder
from app.domain.feature_builder import build_price_features
from app.domain.features import StockFeatures
from app.clients.line_client import LineMessagingClient
from app.db.delivery_repository import DeliveryRepository
from app.db.models import MessageDelivery
from app.delivery.service import DeliveryService
from app.domain.institutional_flow_builder import build_institutional_net_buy_ratio
from app.domain.models import StockMaster
from app.domain.monthly_revenue_builder import build_revenue_yoy
from app.domain.risk_inputs import is_ky_stock, is_one_price_limit_up
from app.domain.risk_policy import RiskPolicy, build_risk_quality_raw
from app.domain.scoring import ScoredStock, score_candidates, select_top_five
from app.reports.report_builder import build_report_stocks
from app.reports.text_renderer import (
    MAX_LINE_TEXT_UTF16_UNITS,
    render_daily_report,
    render_no_qualified_stock_report,
    utf16_length,
)
from app.ingestion.finmind_mapper import (
    build_historical_price_points,
    build_institutional_flow_points,
    build_monthly_revenue_points,
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
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily_ranking")

# Provisional MVP thresholds, matching config/strategy-v1.yaml by
# hand for now — a real config loader is separate follow-up work.
MINIMUM_TURNOVER = Decimal("50000000")
MAXIMUM_CANDIDATES = 50

# Matches config/strategy-v1.yaml's strategy_version by hand for now,
# same status as MINIMUM_TURNOVER/MAXIMUM_CANDIDATES above.
STRATEGY_VERSION = "rule-v1.0.0"

# Change only when the rendered report FORMAT changes (not the
# content within it) — see app.clients.idempotency's module docstring
# for why reusing a message_version for genuinely different content
# is a bug, not a valid rerun.
MESSAGE_VERSION = "text-v1"

# CRITICAL for delivery idempotency: the same
# trading_date + strategy_version + target + message_version MUST
# render byte-identical content, or DeliveryRepository.reserve()
# correctly raises DeliveryContentConflict on a same-day rerun instead
# of returning SKIPPED_ALREADY_SENT (see app/db/delivery_repository.py's
# module docstring). A wall-clock timestamp (e.g. datetime.now()) must
# NEVER appear in report content for this reason — use a fixed label
# instead.
DATA_UPDATED_LABEL = "收盤後"

# Retrieval buffer only — historical factors still use the trailing
# 5/20 actual trading-day observations; 60 calendar days simply gives
# FinMind enough room to cover weekends and market holidays.
HISTORY_LOOKBACK_CALENDAR_DAYS = 60

# Retrieval buffer only. ~18 months ensures the response normally
# contains both the latest disclosed revenue month and the same
# revenue month one year earlier — this window is NOT the look-ahead
# guard. Actual availability is enforced by
# MonthlyRevenuePoint.available_at inside build_revenue_yoy().
REVENUE_LOOKBACK_CALENDAR_DAYS = 550


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


def env_flag(name: str) -> bool:
    """Parse a boolean-ish environment variable. Centralizes truthy
    parsing so REPORT_DRY_RUN and any future feature flags
    (LINE_DRY_RUN, ENABLE_DELIVERY, etc.) all agree on what counts as
    "on"."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def ensure_delivery_table(engine) -> None:
    """Temporary bootstrap for local/manual delivery validation.

    Alembic is listed as a dependency (see requirements.txt) but a
    migration workflow (alembic.ini / migrations env) is not wired
    into this repository yet. This checkfirst=True create() is a
    no-op against a database where the table already exists — fine
    for local SQLite smoke tests, but production schema changes
    should go through a real migration once one exists, not through
    application startup silently creating tables.
    """
    MessageDelivery.__table__.create(engine, checkfirst=True)


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


def log_top_five(
    top_five: list[ScoredStock], stock_master: dict[str, StockMaster]
) -> None:
    """
    Manual-review checkpoint before report rendering / LINE delivery
    exist. Logs the full factor_scores breakdown, not just the
    aggregate total_score, so ranking order can be inspected and
    explained ("why did #1 beat #2") rather than taken on faith.
    """
    if not top_five:
        logger.info("=== Top 5 (人工檢視用) === 無符合資格股票")
        return

    logger.info("=== Top 5 (人工檢視用,尚未產生報告/推播) ===")
    for rank, scored in enumerate(top_five, start=1):
        stock = stock_master.get(scored.stock_id)
        name = stock.stock_name if stock else scored.stock_id
        logger.info(
            "#%d %s(%s) score=%.2f completeness=%.0f%% risk_flags=%s factor_scores=%s",
            rank,
            name,
            scored.stock_id,
            scored.total_score,
            scored.data_completeness * 100,
            scored.risk_flags,
            scored.factor_scores,
        )


def build_stock_features(
    *,
    candidates: list[Candidate],
    target_date: dt.date,
    finmind_client: FinMindClient,
    ingestion_run_id: str,
    risk_policy: RiskPolicy,
) -> list[StockFeatures]:
    """
    Enrich candidate stocks with trailing historical price features,
    institutional net-buy ratio, monthly revenue YoY, and a risk
    assessment.

    Today's close / volume / turnover always come from the already
    validated TWSE / TPEx candidate data. FinMind is queried only for
    sessions strictly before target_date, for price history,
    institutional flow, and monthly revenue alike.

    Failure policy — deliberately INDEPENDENT per data source:
        Price-history enrichment, institutional-flow enrichment, and
        monthly-revenue enrichment each have their OWN try/except
        block. A failure in one must never clear factors already
        successfully computed by the others — e.g. FinMind's
        institutional endpoint being briefly unavailable must not
        wipe out average_turnover_20d/return_5d that were already
        computed from a successful price-history fetch for the same
        stock, and vice versa for revenue.

        Institutional net-buy ratio reuses the volume data already
        fetched for price-history enrichment (see
        app.domain.institutional_flow_builder's module docstring) —
        it is not fetched again. If price-history enrichment failed
        for a stock, the institutional ratio simply has no volume
        data to divide by and naturally comes back None; no separate
        coordination logic is needed between the two blocks for that
        case.

        Missing core candidate data (today's close/volume/turnover)
        is different: CandidateBuilder should already have rejected
        such a record, so seeing it here indicates an internal
        invariant violation, not ordinary enrichment-data absence —
        that raises immediately rather than being silently patched.

    Risk assessment (block 4) is deliberately NOT part of the same
    fail-soft family as blocks 1-3 — see the inline comment at that
    block for why. is_attention/is_disposition/is_managed and
    consecutive_limit_up_days are tri-state (bool|None / int|None):
    None means unknown, never treated as False. Currently every stock
    hits this gap (see the warning logged below), so risk_quality_raw
    is None for every stock unless that changes.
    """
    logger.warning(
        "RiskPolicy input gap: is_attention/is_disposition/is_managed have "
        "no wired-in data source and are always None (unknown), not False; "
        "consecutive_limit_up_days is always None (no reliable historical "
        "reference-price source — see app.domain.risk_inputs's module "
        "docstring for why this is not reconstructed from raw closes). "
        "risk_quality_raw is None for every stock affected by this gap — "
        "see build_risk_quality_raw()'s docstring."
    )

    if not candidates:
        logger.info("No candidates require enrichment")
        return []

    history_start_date = target_date - dt.timedelta(days=HISTORY_LOOKBACK_CALENDAR_DAYS)
    history_end_date = target_date - dt.timedelta(days=1)

    revenue_start_date = target_date - dt.timedelta(days=REVENUE_LOOKBACK_CALENDAR_DAYS)
    revenue_end_date = target_date - dt.timedelta(days=1)

    features: list[StockFeatures] = []
    price_success_count = 0
    price_empty_count = 0
    price_failure_count = 0
    institutional_success_count = 0
    institutional_none_count = 0
    institutional_failure_count = 0
    revenue_success_count = 0
    revenue_none_count = 0
    revenue_failure_count = 0
    risk_excluded_count = 0
    risk_flagged_count = 0
    risk_clean_count = 0
    risk_incomplete_count = 0
    risk_assessment_failure_count = 0

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

        # --- Independent block 1: historical price factors ---
        average_turnover_20d = None
        volume_ratio_20d = None
        return_5d = None
        return_20d = None
        volume_by_date: dict[dt.date, float] = {}

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
                price_empty_count += 1
                logger.warning(
                    "FinMind history returned no usable rows for stock_id=%s; "
                    "technical factors remain None",
                    stock_id,
                )
            else:
                history_points = build_historical_price_points(history_rows)
                volume_by_date = {
                    point.trading_date: point.volume for point in history_points
                }

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
                price_success_count += 1

        except Exception:
            price_failure_count += 1
            logger.exception(
                "FinMind historical-price enrichment failed for stock_id=%s; "
                "technical factors remain None",
                stock_id,
            )

        # --- Independent block 2: institutional net-buy ratio ---
        institutional_net_buy_ratio_5d = None

        try:
            institutional_payload = finmind_client.fetch_stock_institutional_investors(
                ingestion_run_id=ingestion_run_id,
                stock_id=stock_id,
                start_date=history_start_date,
                end_date=history_end_date,
                target_date=target_date,
            )
            institutional_rows = extract_data_rows(institutional_payload)

            if not institutional_rows:
                institutional_none_count += 1
                logger.warning(
                    "FinMind institutional data returned no usable rows for "
                    "stock_id=%s; institutional_net_buy_ratio_5d remains None",
                    stock_id,
                )
            else:
                flow_points = build_institutional_flow_points(
                    institutional_rows, expected_stock_id=stock_id
                )
                institutional_net_buy_ratio_5d = build_institutional_net_buy_ratio(
                    target_date=target_date,
                    flow_points=flow_points,
                    volume_by_date=volume_by_date,
                )
                if institutional_net_buy_ratio_5d is None:
                    institutional_none_count += 1
                else:
                    institutional_success_count += 1

        except Exception:
            institutional_failure_count += 1
            logger.exception(
                "FinMind institutional-flow enrichment failed for stock_id=%s; "
                "institutional_net_buy_ratio_5d remains None",
                stock_id,
            )

        # --- Independent block 3: monthly revenue YoY ---
        revenue_yoy = None

        try:
            revenue_payload = finmind_client.fetch_stock_monthly_revenue(
                ingestion_run_id=ingestion_run_id,
                stock_id=stock_id,
                start_date=revenue_start_date,
                end_date=revenue_end_date,
                target_date=target_date,
            )
            revenue_rows = extract_data_rows(revenue_payload)

            if not revenue_rows:
                revenue_none_count += 1
                logger.warning(
                    "FinMind monthly revenue returned no usable rows for "
                    "stock_id=%s; revenue_yoy remains None",
                    stock_id,
                )
            else:
                revenue_points = build_monthly_revenue_points(
                    revenue_rows, expected_stock_id=stock_id
                )
                revenue_yoy = build_revenue_yoy(
                    target_date=target_date, points=revenue_points
                )
                if revenue_yoy is None:
                    revenue_none_count += 1
                else:
                    revenue_success_count += 1

        except Exception:
            revenue_failure_count += 1
            logger.exception(
                "FinMind monthly-revenue enrichment failed for stock_id=%s; "
                "revenue_yoy remains None",
                stock_id,
            )

        # --- Block 4: risk assessment (hard exclusion + soft flags) ---
        # Deliberately NOT symmetric with blocks 1-3: those fail-soft
        # (leave a factor None, keep the stock). Risk assessment can
        # hard-exclude a stock entirely (disposition/managed status),
        # which is a "should this candidate exist at all" decision,
        # not a "this one factor is missing" decision.
        try:
            one_price = is_one_price_limit_up(
                price=candidate.price, limit_up_price=candidate.limit_up.limit_up_price
            )
            assessment = risk_policy.assess(
                stock_id=stock_id,
                is_attention=candidate.stock.is_attention,
                is_disposition=candidate.stock.is_disposition,
                is_managed=candidate.stock.is_managed,
                is_ky=is_ky_stock(candidate.stock.stock_name),
                is_one_price_limit_up=one_price,
                # No reliable historical reference-price/limit-up
                # source is wired in yet — see app.domain.risk_inputs's
                # module docstring for why this is not reconstructed
                # from raw closes as a heuristic.
                consecutive_limit_up_days=None,
                return_5d=return_5d,
            )
        except Exception:
            risk_assessment_failure_count += 1
            logger.exception(
                "Risk assessment failed for stock_id=%s; excluding defensively "
                "rather than scoring with unknown risk",
                stock_id,
            )
            continue

        if assessment.is_excluded:
            risk_excluded_count += 1
            logger.info(
                "stock_id=%s excluded by RiskPolicy: %s",
                stock_id,
                assessment.exclusion_reason,
            )
            continue  # this candidate never becomes a StockFeatures

        risk_flags = assessment.risk_flags
        risk_quality_raw = build_risk_quality_raw(assessment)

        if assessment.missing_inputs:
            risk_incomplete_count += 1
        elif risk_flags:
            risk_flagged_count += 1
        else:
            risk_clean_count += 1

        stock_features = StockFeatures(
            stock_id=stock_id,
            turnover=float(today_turnover),
            average_turnover_20d=average_turnover_20d,
            volume_ratio_20d=volume_ratio_20d,
            return_5d=return_5d,
            return_20d=return_20d,
            institutional_net_buy_ratio_5d=institutional_net_buy_ratio_5d,
            revenue_yoy=revenue_yoy,
            risk_quality_raw=risk_quality_raw,
            risk_flags=risk_flags,
        )
        features.append(stock_features)

        logger.info(
            "features stock_id=%s turnover=%s avg_turnover_20d=%s "
            "volume_ratio_20d=%s return_5d=%s return_20d=%s "
            "institutional_net_buy_ratio_5d=%s revenue_yoy=%s "
            "risk_quality_raw=%s risk_flags=%s risk_missing_inputs=%s",
            stock_id,
            stock_features.turnover,
            stock_features.average_turnover_20d,
            stock_features.volume_ratio_20d,
            stock_features.return_5d,
            stock_features.return_20d,
            stock_features.institutional_net_buy_ratio_5d,
            stock_features.revenue_yoy,
            stock_features.risk_quality_raw,
            stock_features.risk_flags,
            assessment.missing_inputs,
        )

    logger.info(
        "Price-history enrichment: candidates=%d success=%d empty=%d failed=%d",
        len(candidates),
        price_success_count,
        price_empty_count,
        price_failure_count,
    )
    logger.info(
        "Institutional-flow enrichment: candidates=%d success=%d none=%d failed=%d",
        len(candidates),
        institutional_success_count,
        institutional_none_count,
        institutional_failure_count,
    )
    logger.info(
        "Monthly-revenue enrichment: candidates=%d success=%d none=%d failed=%d",
        len(candidates),
        revenue_success_count,
        revenue_none_count,
        revenue_failure_count,
    )
    logger.info(
        "Risk assessment: candidates=%d excluded=%d flagged=%d clean=%d "
        "incomplete=%d assessment_failed=%d",
        len(candidates),
        risk_excluded_count,
        risk_flagged_count,
        risk_clean_count,
        risk_incomplete_count,
        risk_assessment_failure_count,
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
    delivery_repository: DeliveryRepository | None = None,
    line_client: LineMessagingClient | None = None,
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

    # --- Step 5: per-candidate enrichment (price history + institutional flow + monthly revenue + risk) ---
    risk_policy = RiskPolicy()  # matches config/strategy-v1.yaml by hand for now
    features = build_stock_features(
        candidates=candidates,
        target_date=target_date,
        finmind_client=finmind_client,
        ingestion_run_id=ingestion_run_id,
        risk_policy=risk_policy,
    )
    logger.info(
        "Built StockFeatures for %d of %d candidates after RiskPolicy",
        len(features),
        len(candidates),
    )

    # --- Step 6: multi-factor scoring + Top 5 (manual-review checkpoint) ---
    # Deliberately stops here: no report rendering, no LINE delivery.
    # See log_top_five()'s docstring for why the full factor breakdown
    # is logged rather than just the final score.
    scored = score_candidates(features)
    logger.info("Scored %d StockFeatures", len(scored))

    turnover_by_stock = {f.stock_id: f.turnover for f in features}
    minimum_data_completeness = risk_policy.config.minimum_data_completeness

    eligible_count = sum(
        1 for stock in scored if stock.data_completeness >= minimum_data_completeness
    )

    top_five = select_top_five(
        scored,
        turnover_by_stock,
        minimum_data_completeness=minimum_data_completeness,
    )

    logger.info(
        "Scoring eligibility: scored=%d eligible=%d minimum_data_completeness=%.0f%%",
        len(scored),
        eligible_count,
        minimum_data_completeness * 100,
    )
    log_top_five(top_five, stock_master)

    # --- Step 7: report rendering (shared by dry-run preview and live delivery) ---
    report_dry_run = env_flag("REPORT_DRY_RUN")
    line_delivery_mode = os.environ.get("LINE_DELIVERY_MODE", "off").strip().lower()

    if line_delivery_mode not in {"off", "push", "broadcast"}:
        logger.error(
            "Invalid LINE_DELIVERY_MODE=%r; expected off, push, or broadcast",
            line_delivery_mode,
        )
        return 1

    line_live_delivery = line_delivery_mode != "off"
    report_text: str | None = None

    if report_dry_run or line_live_delivery:
        report_stocks = build_report_stocks(
            top_five=top_five, stock_master=stock_master
        )

        # IMPORTANT: must stay deterministic for same-day idempotent
        # reruns — see DATA_UPDATED_LABEL's definition above.
        data_updated_at = DATA_UPDATED_LABEL

        if report_stocks:
            report_text = render_daily_report(
                trading_date=target_date,
                data_updated_at=data_updated_at,
                candidate_count=len(candidates),
                eligible_count=eligible_count,
                strategy_version=STRATEGY_VERSION,
                ranked_stocks=report_stocks,
            )
        else:
            report_text = render_no_qualified_stock_report(
                trading_date=target_date,
                data_updated_at=data_updated_at,
                candidate_count=len(candidates),
                strategy_version=STRATEGY_VERSION,
            )

    if report_dry_run:
        assert report_text is not None
        report_length = utf16_length(report_text)

        print()
        print("=" * 72)
        print("REPORT_DRY_RUN preview")
        print("NOT sent to LINE / NOT written to delivery DB")
        print("=" * 72)
        print(report_text)
        print("=" * 72)
        print(f"UTF-16 length: {report_length} / {MAX_LINE_TEXT_UTF16_UNITS}")
        print("=" * 72)
        print()

    # --- Step 8: LINE live delivery (real side effect — opt-in only) ---
    # off       -> no LINE call, no DB write (default)
    # push      -> send to ONE target (LINE_TARGET_ID) — for testing a
    #              report-format change without notifying every
    #              subscriber
    # broadcast -> send to ALL friends of this Official Account — the
    #              real daily delivery to family/subscribers
    if line_live_delivery:
        assert report_text is not None

        target_id: str | None = None
        if line_delivery_mode == "push":
            target_id = os.environ.get("LINE_TARGET_ID", "").strip()
            if not target_id:
                logger.error("LINE_DELIVERY_MODE=push requires LINE_TARGET_ID")
                return 1

        if line_client is None:
            channel_access_token = os.environ.get(
                "LINE_CHANNEL_ACCESS_TOKEN", ""
            ).strip()
            if not channel_access_token:
                logger.error(
                    "LINE_DELIVERY_MODE=%s is enabled but "
                    "LINE_CHANNEL_ACCESS_TOKEN is missing",
                    line_delivery_mode,
                )
                return 1
            line_client = LineMessagingClient(channel_access_token=channel_access_token)

        def _run_delivery(service: DeliveryService) -> str:
            if line_delivery_mode == "push":
                return service.deliver(
                    trading_date=target_date,
                    strategy_version=STRATEGY_VERSION,
                    target_id=target_id,
                    message_version=MESSAGE_VERSION,
                    message=report_text,
                )
            return service.deliver_broadcast(
                trading_date=target_date,
                strategy_version=STRATEGY_VERSION,
                message_version=MESSAGE_VERSION,
                message=report_text,
            )

        try:
            if delivery_repository is not None:
                # Injected (e.g. by a test) — ownership of the
                # session's lifecycle belongs to the caller, not to
                # this function. Do not close it here.
                delivery_service = DeliveryService(
                    repository=delivery_repository, line_client=line_client
                )
                delivery_result = _run_delivery(delivery_service)
            else:
                database_url = os.environ.get("DATABASE_URL", "").strip()
                if not database_url:
                    logger.error(
                        "LINE_DELIVERY_MODE=%s is enabled but DATABASE_URL is missing",
                        line_delivery_mode,
                    )
                    return 1

                engine = create_engine(database_url)
                try:
                    ensure_delivery_table(engine)
                    with Session(engine) as session:
                        delivery_service = DeliveryService(
                            repository=DeliveryRepository(session),
                            line_client=line_client,
                        )
                        delivery_result = _run_delivery(delivery_service)
                finally:
                    engine.dispose()
        except Exception:
            logger.exception(
                "LINE_DELIVERY_MODE=%s failed trading_date=%s strategy_version=%s "
                "message_version=%s",
                line_delivery_mode,
                target_date,
                STRATEGY_VERSION,
                MESSAGE_VERSION,
            )
            return 1

        logger.info(
            "LINE delivery mode=%s result=%s trading_date=%s strategy_version=%s "
            "message_version=%s",
            line_delivery_mode,
            delivery_result,
            target_date,
            STRATEGY_VERSION,
            MESSAGE_VERSION,
        )

    logger.info(
        "Provisional pipeline complete: %d raw snapshots, %d mapped stocks, "
        "%d prices, %d candidates, %d features built, %d scored, %d eligible, "
        "%d in Top 5",
        len(repo.saved),
        len(stock_master),
        len(daily_prices),
        len(candidates),
        len(features),
        len(scored),
        eligible_count,
        len(top_five),
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
