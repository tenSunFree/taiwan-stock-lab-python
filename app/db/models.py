"""
SQLAlchemy ORM models — tables required for Phase 1.
The remaining tables (factor_scores / ranking_results /
generated_reports / message_deliveries / performance_results, etc.)
are deferred to Phase 3-6.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TradingCalendarRow(Base):
    __tablename__ = "trading_calendar"

    trading_date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    market: Mapped[str] = mapped_column(String(16), primary_key=True, default="TWSE")
    is_trading_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    market_status: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    ingestion_run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    target_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    data_source_version: Mapped[str] = mapped_column(String(32), default="v1")
    error_message: Mapped[str | None] = mapped_column(String)


class RawSourcePayloadRow(Base):
    __tablename__ = "raw_source_payloads"
    __table_args__ = (
        UniqueConstraint(
            "ingestion_run_id", "source", "request_key", name="uq_raw_run_source_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    ingestion_run_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_runs.ingestion_run_id"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    request_key: Mapped[str] = mapped_column(String(256), nullable=False)
    target_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source_updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ingested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class DailyPriceClean(Base):
    __tablename__ = "daily_prices_clean"
    __table_args__ = (
        UniqueConstraint(
            "data_snapshot_id", "stock_id", name="uq_clean_snapshot_stock"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    data_snapshot_id: Mapped[str] = mapped_column(String(32), nullable=False)
    trading_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    stock_id: Mapped[str] = mapped_column(String(16), nullable=False)
    reference_price: Mapped[Numeric | None] = mapped_column(Numeric(12, 4))
    limit_up_price: Mapped[Numeric | None] = mapped_column(Numeric(12, 4))
    open: Mapped[Numeric | None] = mapped_column(Numeric(12, 4))
    high: Mapped[Numeric | None] = mapped_column(Numeric(12, 4))
    low: Mapped[Numeric | None] = mapped_column(Numeric(12, 4))
    close: Mapped[Numeric | None] = mapped_column(Numeric(12, 4))
    volume: Mapped[Numeric | None] = mapped_column(Numeric(20, 0))
    turnover: Mapped[Numeric | None] = mapped_column(Numeric(24, 0))
    is_limit_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    data_quality_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="OK"
    )


class MessageDelivery(Base):
    """
    One row per logical push attempt. line_retry_key is persisted
    BEFORE the first HTTP call is made (see DeliveryRepository.reserve)
    specifically so that if the process crashes after LINE accepted
    the request but before this row is updated to SUCCESS, a later
    retry can reuse the same retry key and safely learn — via LINE's
    409 response — that the message was already sent, instead of
    generating a new key and risking a duplicate push.
    """

    __tablename__ = "message_deliveries"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_message_delivery_idempotency"),
    )

    delivery_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    trading_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)

    # hashed, never the raw LINE user/group ID — see app/clients/idempotency.py
    target_id_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    message_version: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    message_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # UUID as text; persisted before the first LINE API call
    line_retry_key: Mapped[str] = mapped_column(String(36), nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    accepted_request_id: Mapped[str | None] = mapped_column(String(128))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
    )
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class EpsCumulativeObservation(Base):
    """
    Append-only, revision-safe observation log for TWSE/TPEx cumulative
    EPS figures (see app.ingestion.eps_mapper.RawCumulativeEps and
    app.ingestion.eps_availability_resolver's module docstring for the
    concepts this table exists to support).

    THE CORE INVARIANT THIS TABLE PROTECTS: first_seen_at, once written
    for a given (stock_id, fiscal_year, quarter, cumulative_eps) VALUE,
    must NEVER be updated — not even by a later ingestion run that
    re-observes the exact same value. This is enforced structurally,
    not by application discipline: the UniqueConstraint below is on
    the value itself (cumulative_eps included), so:

      - Re-observing the SAME value on a later run hits the unique
        constraint -> the repository (see
        app.db.eps_observation_repository.EpsObservationRepository)
        catches the resulting IntegrityError and returns the EXISTING
        row untouched. first_seen_at is never rewritten because no
        UPDATE is ever issued against it — the same pattern
        DeliveryRepository.reserve() already uses to make
        line_retry_key crash-recovery-safe.
      - A REVISED value (e.g. TWSE restates 台泥 2026Q2's EPS from 0.38
        to 0.40) has a DIFFERENT cumulative_eps, so it does not match
        the unique constraint on the OLD row at all — it inserts as a
        brand-new row with its own, later first_seen_at. The OLD row
        (and its original first_seen_at) is left completely alone.
        Both revisions coexist in this table permanently; which one is
        "current" is a query-time decision (see
        get_first_seen_at_for_current_value below), never an in-place
        mutation.

    DESIGN DECISION — why cumulative_eps is part of the uniqueness key,
    not just (stock_id, fiscal_year, quarter): the whole point of this
    table is to answer "when did OUR pipeline first see THIS EXACT
    figure" for
    app.ingestion.eps_availability_resolver.build_resolved_cumulative_eps_point's
    first_seen_at parameter — that function's contract is per-VALUE,
    not per-period, precisely because a later revision needs its own,
    later first_seen_at rather than silently inheriting the original
    period's. Keying on the period alone would make a restatement look
    exactly like it had been known since the original disclosure —
    reintroducing the look-ahead bias this whole mechanism exists to
    prevent.

    DESIGN DECISION — first_seen_at is a Date, not a DateTime: it
    represents a business/calendar date matching this project's daily
    ingestion cadence (typically the ingestion run's own target_date),
    consistent with every other look-ahead-safe available_at field in
    this project (MonthlyRevenuePoint.available_at,
    QuarterlyEpsPoint.available_at). created_at below is the separate,
    wall-clock "when did this ROW get written" timestamp — the two are
    not interchangeable: a backfill run executed on 2026-09-01 for
    target_date 2026-08-11 must record first_seen_at=2026-08-11, not
    today.

    market/batch_report_date are carried here purely for observability
    and as the fallback input to
    eps_availability_resolver.resolve_eps_availability (batch_report_date)
    — neither participates in the uniqueness key, since a company is
    only ever listed on one of TWSE/TPEx and batch_report_date is
    dataset-wide metadata, not part of what makes a revision distinct.
    """

    __tablename__ = "eps_cumulative_observations"
    __table_args__ = (
        UniqueConstraint(
            "stock_id",
            "fiscal_year",
            "quarter",
            "cumulative_eps",
            name="uq_eps_observation_value",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    stock_id: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(16), nullable=False)  # "twse" | "tpex"
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    quarter: Mapped[int] = mapped_column(Integer, nullable=False)
    cumulative_eps: Mapped[Numeric] = mapped_column(Numeric(12, 4), nullable=False)

    # TWSE/TPEx's own 出表日期 for the batch THIS revision was first
    # observed in — the availability-resolver fallback, kept here even
    # though first_seen_at (below) is normally the sharper signal; see
    # app.ingestion.eps_availability_resolver's precedence rule.
    batch_report_date: Mapped[dt.date] = mapped_column(Date, nullable=False)

    # The core, never-updated field this table exists for — see class
    # docstring's "CORE INVARIANT" section.
    first_seen_at: Mapped[dt.date] = mapped_column(Date, nullable=False)

    first_seen_ingestion_run_id: Mapped[str] = mapped_column(
        ForeignKey("ingestion_runs.ingestion_run_id"), nullable=False
    )

    # Wall-clock row-creation time — NOT the same thing as
    # first_seen_at; see class docstring.
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: dt.datetime.now(dt.timezone.utc),
    )
