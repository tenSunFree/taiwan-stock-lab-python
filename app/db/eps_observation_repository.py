"""
EPS observation repository.

The core safety property this provides — mirroring
app.db.delivery_repository.DeliveryRepository.reserve()'s crash/
concurrency-safe pattern — is that observe() either INSERTS a
brand-new row for a (stock_id, fiscal_year, quarter, cumulative_eps)
value never seen before (this run is the one that gets to set
first_seen_at), or, if that exact value already has a row (because a
previous run already observed it, or two runs raced), returns that
EXISTING row's first_seen_at untouched. There is no UPDATE path onto
first_seen_at anywhere in this repository — the DB's UNIQUE constraint
(see EpsCumulativeObservation.__table_args__) is the single source of
truth for "have we already recorded this exact figure," the same way
it already is for MessageDelivery.idempotency_key. See
app.db.models.EpsCumulativeObservation's docstring for the full
revision-safety rationale (why a REVISED value gets its own new row
rather than overwriting the old one).

DESIGN DECISION — float -> Decimal conversion before every query/
insert: RawCumulativeEps.cumulative_eps (and QuarterlyEpsPoint.eps)
are plain Python floats throughout the domain/ingestion layers, but
this table stores Numeric(12, 4) for exact decimal comparison — a
raw float's binary representation (e.g. 0.38 is not exactly
representable) must never be compared directly against a stored
Decimal, or two runs observing the textually-identical source figure
"0.38" could wrongly be treated as different values and each get an
unwanted new row, silently defeating the whole point of the unique
constraint. _to_decimal() below is the single, shared conversion path
for both writes and reads specifically so the two sides can never
drift apart from each other.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import EpsCumulativeObservation

EPS_DECIMAL_PLACES = Decimal("0.0001")


def _to_decimal(value: float) -> Decimal:
    """
    Convert via str(value), never Decimal(value) directly — Decimal(a
    float) preserves the float's exact (and often ugly, e.g.
    0.379999999999999...) binary representation instead of the decimal
    text a human/TWSE actually meant, which would defeat the entire
    purpose of storing this as Numeric in the first place.
    """
    return Decimal(str(value)).quantize(EPS_DECIMAL_PLACES)


@dataclass(frozen=True)
class EpsObservationResult:
    observation: EpsCumulativeObservation
    created: bool  # False means this exact value was already recorded — first_seen_at is the ORIGINAL run's, not this one's


class EpsObservationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def observe(
        self,
        *,
        stock_id: str,
        market: str,
        fiscal_year: int,
        quarter: int,
        cumulative_eps: float,
        batch_report_date: dt.date,
        first_seen_at: dt.date,
        ingestion_run_id: str,
    ) -> EpsObservationResult:
        """
        Record that `ingestion_run_id` observed this exact
        (stock_id, fiscal_year, quarter, cumulative_eps) value as of
        `first_seen_at`. If this exact value was already recorded by
        an earlier run, that earlier row — and its earlier
        first_seen_at — is returned unchanged; this call's own
        first_seen_at/ingestion_run_id are simply discarded in that
        case, never written.
        """
        eps_decimal = _to_decimal(cumulative_eps)

        row = EpsCumulativeObservation(
            stock_id=stock_id,
            market=market,
            fiscal_year=fiscal_year,
            quarter=quarter,
            cumulative_eps=eps_decimal,
            batch_report_date=batch_report_date,
            first_seen_at=first_seen_at,
            first_seen_ingestion_run_id=ingestion_run_id,
        )

        self.session.add(row)
        try:
            self.session.commit()
            self.session.refresh(row)
            return EpsObservationResult(observation=row, created=True)
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(EpsCumulativeObservation).where(
                    EpsCumulativeObservation.stock_id == stock_id,
                    EpsCumulativeObservation.fiscal_year == fiscal_year,
                    EpsCumulativeObservation.quarter == quarter,
                    EpsCumulativeObservation.cumulative_eps == eps_decimal,
                )
            )
            if existing is None:
                # extremely unlikely (the conflict came from something
                # else) — re-raise rather than silently swallow it,
                # same reasoning as DeliveryRepository.reserve().
                raise
            return EpsObservationResult(observation=existing, created=False)

    def get_first_seen_at(
        self,
        *,
        stock_id: str,
        fiscal_year: int,
        quarter: int,
        cumulative_eps: float,
    ) -> dt.date | None:
        """
        Look up the first_seen_at this project's own pipeline recorded
        for this EXACT value — the precise input
        app.ingestion.eps_availability_resolver.build_resolved_cumulative_eps_point
        needs for its `first_seen_at` parameter. Returns None (not an
        exception) when this exact value has never been observed yet
        — e.g. a historical quarter ingested before this table
        existed, or a revision this run is seeing for the very first
        time and hasn't called observe() for yet — so the resolver's
        existing batch_report_date fallback takes over exactly as
        designed, rather than this lookup masking a real "we don't
        know yet" as a crash.
        """
        existing = self.session.scalar(
            select(EpsCumulativeObservation).where(
                EpsCumulativeObservation.stock_id == stock_id,
                EpsCumulativeObservation.fiscal_year == fiscal_year,
                EpsCumulativeObservation.quarter == quarter,
                EpsCumulativeObservation.cumulative_eps == _to_decimal(cumulative_eps),
            )
        )
        return existing.first_seen_at if existing is not None else None
