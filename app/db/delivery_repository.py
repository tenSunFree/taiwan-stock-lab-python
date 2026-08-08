"""
Delivery repository.

The core safety property this provides: reserve() either creates a
brand-new PENDING row (and this caller is the one responsible for
actually pushing to LINE), or — if a row with the same idempotency_key
already exists because of a concurrent run or a prior crash — returns
that existing row instead, including its already-persisted
line_retry_key. The caller never has to guess whether it's safe to
generate a new retry key; the DB's UNIQUE constraint is the single
source of truth for "has this exact delivery already been claimed."

The one thing the UNIQUE constraint alone does NOT catch: the same
idempotency_key being reserved with genuinely different message
content. That can only happen because of a bug elsewhere (e.g. the
same message_version accidentally reused for changed content instead
of bumping to a new version), and it must never be silently papered
over by returning the old row — see DeliveryContentConflict below.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clients.idempotency import (
    create_delivery_idempotency_key,
    create_message_hash,
    hash_target_id,
)
from app.db.models import MessageDelivery


class DeliveryContentConflict(RuntimeError):
    """
    Raised when reserve() is called with an idempotency_key that
    already exists in the database, but the message content hashes to
    something different than what was originally reserved.

    This should never happen during normal crash-recovery/rerun flows
    (a rerun renders the exact same content from the same input data
    for the same message_version). If it does happen, it means the
    caller changed the report content without bumping message_version
    — treat this as a bug to fix in the caller, not a case to paper
    over by silently keeping the old content.
    """


@dataclass(frozen=True)
class DeliveryReservation:
    delivery: MessageDelivery
    created: bool  # False means this reservation already existed (concurrent run or crash recovery)


class DeliveryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def reserve(
        self,
        *,
        trading_date: dt.date,
        strategy_version: str,
        target_id: str,
        message_version: str,
        message: str,
    ) -> DeliveryReservation:
        key = create_delivery_idempotency_key(
            trading_date=trading_date.isoformat(),
            strategy_version=strategy_version,
            target_id=target_id,
            message_version=message_version,
        )
        message_hash = create_message_hash(message)

        row = MessageDelivery(
            trading_date=trading_date,
            strategy_version=strategy_version,
            target_id_hash=hash_target_id(target_id),
            message_version=message_version,
            idempotency_key=key,
            message_hash=message_hash,
            line_retry_key=str(uuid4()),
            status="PENDING",
        )

        self.session.add(row)
        try:
            self.session.commit()
            self.session.refresh(row)
            return DeliveryReservation(delivery=row, created=True)
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(MessageDelivery).where(MessageDelivery.idempotency_key == key)
            )
            if existing is None:
                # extremely unlikely (the conflict came from something
                # else), re-raise rather than silently swallow it
                raise

            if existing.message_hash != message_hash:
                raise DeliveryContentConflict(
                    f"idempotency_key {key} was already reserved with different "
                    f"content (existing message_hash={existing.message_hash}, "
                    f"new message_hash={message_hash}). This message_version "
                    f"({message_version}) must not be reused for different "
                    f"content — create a new message_version instead."
                )

            return DeliveryReservation(delivery=existing, created=False)

    def mark_success(
        self,
        delivery: MessageDelivery,
        *,
        request_id: str | None,
        accepted_request_id: str | None = None,
    ) -> None:
        delivery.status = "SUCCESS"
        delivery.provider_request_id = request_id
        delivery.accepted_request_id = accepted_request_id
        delivery.sent_at = dt.datetime.now(dt.timezone.utc)
        delivery.error_message = None
        self.session.commit()

    def mark_failed(self, delivery: MessageDelivery, *, error_message: str) -> None:
        delivery.status = "FAILED"
        delivery.retry_count += 1
        delivery.error_message = error_message
        self.session.commit()