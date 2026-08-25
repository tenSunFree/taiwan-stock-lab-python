"""
Daily scheduled job entry point.

Wires real market data from BOTH TWSE (listed) and TPEx (OTC) through
to CandidateBuilder, then enriches each candidate with trailing
historical price features, institutional net-buy ratio, and monthly
revenue YoY from FinMind, then applies RiskPolicy and multi-factor
scoring to select a research-only Top N (see RANKING_LIMIT below).
When REPORT_DRY_RUN is
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
    - RiskPolicy's is_managed input and consecutive_limit_up_days are
      still always None (unknown) — no wired-in official data source
      exists for full-cash-delivery status, and no reliable historical
      reference-price source exists for the latter (see
      app.domain.risk_inputs's module docstring). is_attention/
      is_disposition ARE wired in as of rule-v1.2.0 (see Step 1d
      below), but a per-source fetch/parse failure still leaves the
      affected market's candidates with those two inputs Unknown for
      that run — see _resolve_regulatory_flags. As long as is_managed/
      consecutive_limit_up_days remain unwired, risk_quality_raw stays
      None for every stock regardless of how attention/disposition
      resolve, since RiskPolicy.assess() requires ALL FOUR inputs to
      be confirmed before it will score risk_quality at all (see
      build_risk_quality_raw()'s docstring). A warning is logged on
      every build_stock_features() call to keep this remaining gap
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
from app.domain.models import Market, RegulatoryRiskStatus, StockMaster, StockValuation
from app.domain.monthly_revenue_builder import build_revenue_yoy
from app.domain.risk_inputs import is_ky_stock, is_one_price_limit_up
from app.domain.risk_policy import RiskPolicy, build_risk_quality_raw
from app.domain.scoring import ScoredStock, score_candidates, select_top_n
from app.domain.valuation_filter import filter_candidates_by_pe
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
from app.ingestion.regulatory_mapper import (
    build_tpex_attention_statuses,
    build_tpex_disposition_statuses,
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
from app.ingestion.twse_regulatory_mapper import (
    build_twse_attention_statuses,
    build_twse_disposition_statuses,
)
from app.ingestion.valuation_mapper import build_tpex_valuations, build_twse_valuations
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("daily_ranking")

# Provisional MVP thresholds, matching config/strategy-v1.yaml by
# hand for now — a real config loader is separate follow-up work.
MINIMUM_TURNOVER = Decimal("50000000")
MAXIMUM_CANDIDATES = 50

# How many stocks the daily ranking publishes at most. Matches
# config/strategy-v1.yaml's candidate.ranking_limit by hand for now,
# same status as MINIMUM_TURNOVER/MAXIMUM_CANDIDATES above.
RANKING_LIMIT = 10

# P/E eligibility threshold: 0 < P/E <= MAXIMUM_PE_RATIO (see
# app.domain.valuation_filter.filter_candidates_by_pe). Matches
# config/strategy-v1.yaml's candidate.maximum_pe_ratio by hand for
# now, same status as MINIMUM_TURNOVER/MAXIMUM_CANDIDATES above.
MAXIMUM_PE_RATIO = Decimal("20")

# How many calendar days a valuation snapshot's own date (see
# app.ingestion.valuation_mapper's module docstring for why it's
# allowed to lag behind target_date at all) may fall behind
# target_date before it's treated as stale rather than merely
# "a day or two behind, as expected." "Accept a short lag" (the
# mapper's job) is a different policy from "accept arbitrarily old
# data" (this bound's job) — 5 calendar days covers a normal long
# weekend/holiday gap plus one buffer day; a real gap this size
# usually means the source itself is broken, not merely running a
# day behind schedule.
MAXIMUM_VALUATION_STALENESS_DAYS = 5

# Matches config/strategy-v1.yaml's strategy_version by hand for now,
# same status as MINIMUM_TURNOVER/MAXIMUM_CANDIDATES above.
#
# rule-v1.1.0: the P/E eligibility filter (Step 4.5) — 0 < P/E <=
# MAXIMUM_PE_RATIO changes WHICH candidates are even eligible to be
# scored at all, not just the display cutoff.
#
# rule-v1.2.0: RiskPolicyConfig's disposition/managed-stock handling
# changed from unconditional hard exclusion to configurable (default:
# allowed, flagged) — a disposition/managed stock that used to
# silently vanish from every report now appears with a
# DISPOSITION_STOCK/MANAGED_STOCK flag instead. ATTENTION_STOCK's
# score penalty also dropped from 0.15 to 0.0 (display-only for now —
# see RISK_FLAG_PENALTIES's own docstring). Both are real changes to
# which candidates end up in the published ranking and what score
# they get, so per this file's own versioning rule (see the yaml's
# comment: "when tuning, create a new strategy_version instead of
# overwriting this one, otherwise historical ranking results lose
# their reference baseline"), this needs its own version too.
STRATEGY_VERSION = "rule-v1.2.0"

# Change only when the rendered report FORMAT changes (not the
# content within it) — see app.clients.idempotency's module docstring
# for why reusing a message_version for genuinely different content
# is a bug, not a valid rerun.
#
# text-v5: report-display step (Step 6) — the risk section rendered
# multi-line entries (ATTENTION_STOCK/DISPOSITION_STOCK with reason
# text, DISPOSITION_STOCK additionally with a period line and a 🚨
# marker instead of a plain "・" bullet).
#
# text-v6: replaced the per-stock block's rendering entirely — new
# "訊號" section (all six factor scores as 🟢/🟡/🔴/⚪ lights, including
# volume_price, which the old template never surfaced on its own),
# new "監管狀態" section (explicit tri-state attention/disposition/
# managed display instead of folding them into risk_flags bullets),
# new "漲停結構" section (one-price-limit-up + 20-day volume ratio),
# an explicit "今日排名：N / ranking_limit" line, and the risk_quality
# gap sentence in "資料缺口" is now built dynamically from
# ReportStockView.risk_missing_inputs instead of a single hardcoded
# string — see text_renderer.py's _render_stock_block and
# _risk_quality_missing_reason. This is a genuine format change (new
# section shapes, new line semantics), not just different content
# flowing through the unchanged template — bumps again from text-v5.
MESSAGE_VERSION = "text-v6"

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


def _merge_regulatory_status(
    a: RegulatoryRiskStatus, b: RegulatoryRiskStatus
) -> RegulatoryRiskStatus:
    """
    OR-merges two RegulatoryRiskStatus records for the SAME stock_id —
    used when a stock appears in both an attention-source hit and a
    disposition-source hit from the same market (see
    app.domain.models.RegulatoryRiskStatus's own "MERGE TARGET"
    docstring for why this is necessary: attention and disposition are
    two separate reports, so a single mapper call only ever populates
    one side's fields). Takes whichever side has a True flag / non-None
    detail field for each attribute — never lets one side's defaults
    silently clobber the other's real data.
    """
    return RegulatoryRiskStatus(
        trading_date=a.trading_date,
        stock_id=a.stock_id,
        is_attention=a.is_attention or b.is_attention,
        attention_reason=a.attention_reason or b.attention_reason,
        is_disposition=a.is_disposition or b.is_disposition,
        disposition_start_date=a.disposition_start_date or b.disposition_start_date,
        disposition_end_date=a.disposition_end_date or b.disposition_end_date,
        disposition_reason=a.disposition_reason or b.disposition_reason,
        disposition_measure=a.disposition_measure or b.disposition_measure,
    )


def _resolve_regulatory_flags(
    *,
    stock_id: str,
    market: Market,
    regulatory_by_stock: dict[str, RegulatoryRiskStatus],
    twse_attention_ok: bool,
    twse_disposition_ok: bool,
    tpex_attention_ok: bool,
    tpex_disposition_ok: bool,
) -> tuple[bool | None, bool | None]:
    """
    Resolves (is_attention, is_disposition) for one candidate, per this
    feature's fail-closed-to-UNKNOWN policy (not fail-closed-to-False):

    - A positive hit in regulatory_by_stock is always True, regardless
      of source health — a stock that's already IN the list has
      obviously been checked successfully as far as that one row goes.
    - An ABSENCE from the dict is only trustworthy as "confirmed not
      flagged" (False) when the specific market+category source that
      would have found it actually succeeded this run. Otherwise it
      must stay None (unconfirmed) — see RiskPolicy.assess()'s own
      tri-state handling, which already does the right thing with
      None (tracked in missing_inputs, never treated as "confirmed
      clean"). This is the actual enforcement point for this
      project's "資料抓不到時要標 Unknown，不能因為 API 失敗就當成不是
      注意股" requirement — Step 1d in run() only RECORDS which
      sources succeeded; this function is what turns that into the
      correct per-candidate tri-state value.

    Only the candidate's OWN market's sources matter — a TPEx stock's
    is_attention is never affected by whether TWSE's fetch succeeded,
    since TWSE's attention list could never have contained a TPEx
    stock_id in the first place.
    """
    status = regulatory_by_stock.get(stock_id)

    if market == Market.TWSE:
        attention_source_ok = twse_attention_ok
        disposition_source_ok = twse_disposition_ok
    else:
        attention_source_ok = tpex_attention_ok
        disposition_source_ok = tpex_disposition_ok

    if status is not None and status.is_attention:
        is_attention: bool | None = True
    else:
        is_attention = False if attention_source_ok else None

    if status is not None and status.is_disposition:
        is_disposition: bool | None = True
    else:
        is_disposition = False if disposition_source_ok else None

    return is_attention, is_disposition


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


def log_top_ranked(
    ranked_stocks: list[ScoredStock], stock_master: dict[str, StockMaster]
) -> None:
    """
    Manual-review checkpoint before report rendering / LINE delivery
    exist. Logs the full factor_scores breakdown, not just the
    aggregate total_score, so ranking order can be inspected and
    explained ("why did #1 beat #2") rather than taken on faith.
    """
    if not ranked_stocks:
        logger.info("=== Ranking (人工檢視用) === 無符合資格股票")
        return

    logger.info("=== Top %d (人工檢視用,尚未產生報告/推播) ===", len(ranked_stocks))
    for rank, scored in enumerate(ranked_stocks, start=1):
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
    regulatory_by_stock: dict[str, RegulatoryRiskStatus] | None = None,
    twse_attention_ok: bool = False,
    twse_disposition_ok: bool = False,
    tpex_attention_ok: bool = False,
    tpex_disposition_ok: bool = False,
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
    None means unknown, never treated as False. is_attention/
    is_disposition are wired in as of rule-v1.2.0 (see run()'s Step
    1d), so they resolve to True/False/None per candidate via
    _resolve_regulatory_flags; is_managed and
    consecutive_limit_up_days still always hit this gap (see the
    warning logged below), so risk_quality_raw remains None for every
    stock until those two are wired in as well.
    """
    logger.warning(
        "RiskPolicy input gap: is_managed has no wired-in data source and "
        "is always None (unknown), not False; consecutive_limit_up_days is "
        "always None (no reliable historical reference-price source — see "
        "app.domain.risk_inputs's module docstring for why this is not "
        "reconstructed from raw closes). is_attention/is_disposition ARE "
        "wired in as of rule-v1.2.0, but a per-source fetch/parse failure "
        "this run still leaves the affected market's candidates with those "
        "two Unknown for this run — see _resolve_regulatory_flags. "
        "risk_quality_raw is None for every stock affected by any of these "
        "gaps — see build_risk_quality_raw()'s docstring."
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
            resolved_is_attention, resolved_is_disposition = _resolve_regulatory_flags(
                stock_id=stock_id,
                market=candidate.stock.market,
                regulatory_by_stock=regulatory_by_stock or {},
                twse_attention_ok=twse_attention_ok,
                twse_disposition_ok=twse_disposition_ok,
                tpex_attention_ok=tpex_attention_ok,
                tpex_disposition_ok=tpex_disposition_ok,
            )
            assessment = risk_policy.assess(
                stock_id=stock_id,
                is_attention=resolved_is_attention,
                is_disposition=resolved_is_disposition,
                # No verified TWSE/TPEx source for managed/full-cash-
                # delivery status yet (unlike attention/disposition
                # above) — see app.domain.models.RegulatoryRiskStatus's
                # own docstring for why this field simply doesn't exist
                # there yet, same "known data gap" status as
                # consecutive_limit_up_days below.
                is_managed=None,
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
            risk_missing_inputs=assessment.missing_inputs,
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
            stock_features.risk_missing_inputs,
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

    # --- Step 1c: today's whole-market P/E valuation snapshot (TWSE + TPEx) ---
    # Fetched at the same "whole-market snapshot" tier as Steps 1a/1b, not
    # deferred until after CandidateBuilder — TWSE/TPEx's valuation open
    # data returns the WHOLE market in a single call, just like their price
    # data, so there's no rate-limit reason to wait. The P/E hard filter
    # itself is applied later, at Step 4.5, right after CandidateBuilder and
    # before the per-candidate FinMind enrichment it's meant to protect.
    #
    # Date handling — DIFFERENT from Steps 1a/1b's strict-equality check:
    # confirmed via a real dry run that BWIBBU_ALL / peratio_analysis lag
    # one or more calendar days behind their price-endpoint counterparts
    # (P/E depends on data that isn't finalized as fast as a closing
    # price). build_twse_valuations/build_tpex_valuations use "newest
    # available date <= target_date" instead of requiring an exact match
    # — see valuation_mapper.py's module docstring for the full reasoning.
    #
    # Failure policy (deliberately stricter than a per-stock gap): if
    # either whole-market valuation source can't be fetched or parsed at
    # all, this is treated the same as a TWSE/TPEx price-data failure —
    # fail the whole run rather than silently publish a ranking that only
    # partially checked the P/E eligibility rule against some candidates.
    # A single candidate with no valuation record is a different, much
    # narrower case (handled per-stock by app.domain.valuation_filter).
    try:
        twse_valuation_payload = twse_client.fetch_valuation(
            ingestion_run_id=ingestion_run_id, target_date=target_date
        )
    except Exception:
        logger.exception("TWSE fetch_valuation failed")
        return 1

    if not isinstance(twse_valuation_payload.raw_payload, list):
        logger.error(
            "TWSE BWIBBU_ALL returned unexpected raw payload type: %s",
            type(twse_valuation_payload.raw_payload).__name__,
        )
        return 1

    twse_valuation_rows = [
        row for row in twse_valuation_payload.raw_payload if isinstance(row, dict)
    ]
    twse_valuations = build_twse_valuations(
        target_date=target_date, rows=twse_valuation_rows
    )

    if not twse_valuations:
        logger.warning(
            "WAITING_FOR_DATA: TWSE BWIBBU_ALL returned no usable valuation rows "
            "at or before %s (raw rows=%d) — P/E cannot be verified for any "
            "TWSE candidate today, so no ranking will be published rather "
            "than one that silently skipped every TWSE stock's eligibility "
            "check",
            target_date,
            len(twse_valuation_rows),
        )
        return 2

    try:
        tpex_valuation_payload = tpex_client.fetch_valuation(
            ingestion_run_id=ingestion_run_id, target_date=target_date
        )
    except Exception:
        logger.exception("TPEx fetch_valuation failed")
        return 1

    if not isinstance(tpex_valuation_payload.raw_payload, list):
        logger.error(
            "TPEx tpex_mainboard_peratio_analysis returned unexpected raw "
            "payload type: %s",
            type(tpex_valuation_payload.raw_payload).__name__,
        )
        return 1

    tpex_valuation_rows = [
        row for row in tpex_valuation_payload.raw_payload if isinstance(row, dict)
    ]
    tpex_valuations = build_tpex_valuations(
        target_date=target_date, rows=tpex_valuation_rows
    )

    if not tpex_valuations:
        logger.warning(
            "WAITING_FOR_DATA: TPEx tpex_mainboard_peratio_analysis returned "
            "no usable valuation rows at or before %s (raw rows=%d) — same "
            "policy as the TWSE valuation check above",
            target_date,
            len(tpex_valuation_rows),
        )
        return 2

    valuations_by_stock: dict[str, StockValuation] = {
        valuation.stock_id: valuation
        for valuation in [*twse_valuations, *tpex_valuations]
    }

    # Valuation snapshot dates may lag behind target_date by a day or
    # two (P/E depends on data that isn't finalized as fast as a
    # closing price — see valuation_mapper's module docstring for why
    # a short lag is accepted rather than rejected). A SHORT lag is
    # expected and fine; an implausibly LONG one is a different
    # problem — likely a genuinely broken/stalled source, not just
    # running a day behind schedule — and must not be silently
    # accepted just because build_twse_valuations/build_tpex_valuations
    # found *some* date <= target_date.
    twse_valuation_date = twse_valuations[0].trading_date
    tpex_valuation_date = tpex_valuations[0].trading_date
    twse_valuation_staleness_days = (target_date - twse_valuation_date).days
    tpex_valuation_staleness_days = (target_date - tpex_valuation_date).days

    if twse_valuation_staleness_days > MAXIMUM_VALUATION_STALENESS_DAYS:
        logger.warning(
            "WAITING_FOR_DATA: TWSE BWIBBU_ALL's newest available date %s "
            "is %dd behind target_date %s, exceeding "
            "MAXIMUM_VALUATION_STALENESS_DAYS=%d — treated as a stalled "
            "source, not merely a short expected lag",
            twse_valuation_date,
            twse_valuation_staleness_days,
            target_date,
            MAXIMUM_VALUATION_STALENESS_DAYS,
        )
        return 2

    if tpex_valuation_staleness_days > MAXIMUM_VALUATION_STALENESS_DAYS:
        logger.warning(
            "WAITING_FOR_DATA: TPEx tpex_mainboard_peratio_analysis's newest "
            "available date %s is %dd behind target_date %s, exceeding "
            "MAXIMUM_VALUATION_STALENESS_DAYS=%d — same policy as the TWSE "
            "valuation check above",
            tpex_valuation_date,
            tpex_valuation_staleness_days,
            target_date,
            MAXIMUM_VALUATION_STALENESS_DAYS,
        )
        return 2

    logger.info(
        "Valuation snapshot ready: twse=%d (as of %s, %dd before target) "
        "tpex=%d (as of %s, %dd before target) merged=%d",
        len(twse_valuations),
        twse_valuation_date,
        twse_valuation_staleness_days,
        len(tpex_valuations),
        tpex_valuation_date,
        tpex_valuation_staleness_days,
        len(valuations_by_stock),
    )

    # --- Step 1d: official regulatory risk data (attention/disposition) ---
    # TWSE + TPEx, whole-market. UNLIKE Step 1c's valuation snapshot, a
    # failure fetching or parsing any ONE of these four sources does NOT
    # fail the whole run — this data is display-only in rule-v1.2.0
    # (RiskPolicy defaults to NOT excluding on it; see
    # RiskPolicyConfig.allow_disposition_stock's own docstring), so there
    # is no "cannot verify eligibility" reason to block the pipeline the
    # way Step 1c's P/E check does. Per this feature's own requirement
    # ("資料抓不到時要標 Unknown，不能因為 API 失敗就當成不是注意股"): a
    # failed source just means every candidate on THAT MARKET's
    # is_attention/is_disposition stays None (unconfirmed) rather than
    # being assumed False — see _resolve_regulatory_flags below, which is
    # the only place that distinction is actually enforced.
    twse_attention_by_stock: dict[str, RegulatoryRiskStatus] = {}
    twse_attention_ok = False
    try:
        twse_attention_payload = twse_client.fetch_attention(
            ingestion_run_id=ingestion_run_id, target_date=target_date
        )
        twse_attention_by_stock = build_twse_attention_statuses(
            target_date=target_date, html_text=twse_attention_payload.raw_payload
        )
        twse_attention_ok = True
    except Exception:
        logger.exception(
            "TWSE announcement/notice fetch/parse failed — is_attention "
            "will be Unknown for every TWSE candidate this run, not "
            "assumed False"
        )

    twse_disposition_by_stock: dict[str, RegulatoryRiskStatus] = {}
    twse_disposition_ok = False
    try:
        twse_disposition_payload = twse_client.fetch_disposition(
            ingestion_run_id=ingestion_run_id, target_date=target_date
        )
        twse_disposition_by_stock = build_twse_disposition_statuses(
            target_date=target_date, html_text=twse_disposition_payload.raw_payload
        )
        twse_disposition_ok = True
    except Exception:
        logger.exception(
            "TWSE announcement/punish fetch/parse failed — is_disposition "
            "will be Unknown for every TWSE candidate this run, not "
            "assumed False"
        )

    tpex_attention_by_stock: dict[str, RegulatoryRiskStatus] = {}
    tpex_attention_ok = False
    try:
        tpex_attention_payload = tpex_client.fetch_attention(
            ingestion_run_id=ingestion_run_id, target_date=target_date
        )
        tpex_attention_by_stock = build_tpex_attention_statuses(
            target_date=target_date, payload=tpex_attention_payload.raw_payload
        )
        tpex_attention_ok = True
    except Exception:
        logger.exception(
            "TPEx bulletin/attention fetch/parse failed — is_attention "
            "will be Unknown for every TPEx candidate this run, not "
            "assumed False"
        )

    tpex_disposition_by_stock: dict[str, RegulatoryRiskStatus] = {}
    tpex_disposition_ok = False
    try:
        tpex_disposition_payload = tpex_client.fetch_disposition(
            ingestion_run_id=ingestion_run_id, target_date=target_date
        )
        tpex_disposition_by_stock = build_tpex_disposition_statuses(
            target_date=target_date, payload=tpex_disposition_payload.raw_payload
        )
        tpex_disposition_ok = True
    except Exception:
        logger.exception(
            "TPEx bulletin/disposal fetch/parse failed — is_disposition "
            "will be Unknown for every TPEx candidate this run, not "
            "assumed False"
        )

    # OR-merge: a stock can appear in both an attention hit and a
    # disposition hit (from the same market) — combine into one
    # RegulatoryRiskStatus per stock_id rather than letting the later
    # source's dict silently overwrite the earlier one's fields. A given
    # stock_id is only ever produced by ONE market's mappers (TWSE and
    # TPEx list disjoint stock_ids), so cross-market collisions can't
    # happen here — only attention-vs-disposition, within the same market.
    regulatory_by_stock: dict[str, RegulatoryRiskStatus] = {}
    for source_dict in (
        twse_attention_by_stock,
        twse_disposition_by_stock,
        tpex_attention_by_stock,
        tpex_disposition_by_stock,
    ):
        for stock_id, status in source_dict.items():
            existing = regulatory_by_stock.get(stock_id)
            regulatory_by_stock[stock_id] = (
                status
                if existing is None
                else _merge_regulatory_status(existing, status)
            )

    logger.info(
        "Regulatory risk snapshot: twse_attention_ok=%s(%d) "
        "twse_disposition_ok=%s(%d) tpex_attention_ok=%s(%d) "
        "tpex_disposition_ok=%s(%d) merged=%d",
        twse_attention_ok,
        len(twse_attention_by_stock),
        twse_disposition_ok,
        len(twse_disposition_by_stock),
        tpex_attention_ok,
        len(tpex_attention_by_stock),
        tpex_disposition_ok,
        len(tpex_disposition_by_stock),
        len(regulatory_by_stock),
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

    # --- Step 4.5: P/E ratio eligibility filter (0 < P/E <= MAXIMUM_PE_RATIO) ---
    # Deliberately BEFORE Step 5's FinMind enrichment, not after — a
    # candidate that fails this filter never costs a FinMind API call for
    # price history / institutional flow / monthly revenue. This is an
    # eligibility rule, not a scoring factor (see
    # app.domain.valuation_filter's module docstring for why it's kept out
    # of app.domain.scoring's FACTOR_WEIGHTS entirely).
    candidate_count_before_pe_filter = len(candidates)
    candidates = filter_candidates_by_pe(
        candidates,
        valuations_by_stock,
        maximum_pe_ratio=MAXIMUM_PE_RATIO,
    )
    logger.info(
        "P/E eligibility filter: before=%d after=%d maximum_pe_ratio=%s",
        candidate_count_before_pe_filter,
        len(candidates),
        MAXIMUM_PE_RATIO,
    )

    # --- Step 5: per-candidate enrichment (price history + institutional flow + monthly revenue + risk) ---
    risk_policy = RiskPolicy()  # matches config/strategy-v1.yaml by hand for now
    features = build_stock_features(
        candidates=candidates,
        target_date=target_date,
        finmind_client=finmind_client,
        ingestion_run_id=ingestion_run_id,
        risk_policy=risk_policy,
        regulatory_by_stock=regulatory_by_stock,
        twse_attention_ok=twse_attention_ok,
        twse_disposition_ok=twse_disposition_ok,
        tpex_attention_ok=tpex_attention_ok,
        tpex_disposition_ok=tpex_disposition_ok,
    )
    logger.info(
        "Built StockFeatures for %d of %d candidates after RiskPolicy",
        len(features),
        len(candidates),
    )

    # --- Step 6: multi-factor scoring + Top N (manual-review checkpoint) ---
    # Deliberately stops here: no report rendering, no LINE delivery.
    # See log_top_ranked()'s docstring for why the full factor breakdown
    # is logged rather than just the final score.
    scored = score_candidates(features)
    logger.info("Scored %d StockFeatures", len(scored))

    turnover_by_stock = {f.stock_id: f.turnover for f in features}
    minimum_data_completeness = risk_policy.config.minimum_data_completeness

    eligible_count = sum(
        1 for stock in scored if stock.data_completeness >= minimum_data_completeness
    )

    top_ranked = select_top_n(
        scored,
        turnover_by_stock,
        limit=RANKING_LIMIT,
        minimum_data_completeness=minimum_data_completeness,
    )

    logger.info(
        "Scoring eligibility: scored=%d eligible=%d minimum_data_completeness=%.0f%%",
        len(scored),
        eligible_count,
        minimum_data_completeness * 100,
    )
    log_top_ranked(top_ranked, stock_master)

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
        candidates_by_stock_id = {
            candidate.stock.stock_id: candidate for candidate in candidates
        }
        features_by_stock_id = {feature.stock_id: feature for feature in features}
        report_stocks = build_report_stocks(
            ranked_stocks=top_ranked,
            stock_master=stock_master,
            candidates=candidates_by_stock_id,
            features_by_stock=features_by_stock_id,
            regulatory_by_stock=regulatory_by_stock,
        )

        # IMPORTANT: must stay deterministic for same-day idempotent
        # reruns — see DATA_UPDATED_LABEL's definition above.
        data_updated_at = DATA_UPDATED_LABEL

        if report_stocks:
            report_text = render_daily_report(
                trading_date=target_date,
                data_updated_at=data_updated_at,
                # candidate_count_before_pe_filter, not len(candidates):
                # `candidates` was reassigned in Step 4.5 to the P/E-
                # eligible subset, so len(candidates) here would report
                # the POST-filter count under a label ("進入候選池")
                # that means "entered CandidateBuilder's pool" — using
                # it would silently redefine what this field measures.
                candidate_count=candidate_count_before_pe_filter,
                eligible_count=eligible_count,
                strategy_version=STRATEGY_VERSION,
                ranked_stocks=report_stocks,
                ranking_limit=RANKING_LIMIT,
            )
        else:
            report_text = render_no_qualified_stock_report(
                trading_date=target_date,
                data_updated_at=data_updated_at,
                candidate_count=candidate_count_before_pe_filter,
                strategy_version=STRATEGY_VERSION,
                ranking_limit=RANKING_LIMIT,
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
        "%d in Top %d",
        len(repo.saved),
        len(stock_master),
        len(daily_prices),
        len(candidates),
        len(features),
        len(scored),
        eligible_count,
        len(top_ranked),
        RANKING_LIMIT,
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
