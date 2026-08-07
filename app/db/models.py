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
