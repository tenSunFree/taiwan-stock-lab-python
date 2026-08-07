"""
Delivery orchestration: reserve a delivery slot in the DB, then push
to LINE using the retry key that was already persisted for that slot.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from app.clients.line_client import (
    LineMessagingClient,
    LineNonRetryableError,
    LinePushError,
)
from app.db.delivery_repository import DeliveryRepository


class DeliveryService:
    def __init__(
        self, *, repository: DeliveryRepository, line_client: LineMessagingClient
    ) -> None:
        self.repository = repository
        self.line_client = line_client

    def deliver(
        self,
        *,
        trading_date: dt.date,
        strategy_version: str,
        target_id: str,
        message_version: str,
        message: str,
    ) -> str:
        """
        Returns one of:
            "SKIPPED_ALREADY_SENT"      — a prior run already succeeded
            "SUCCESS"                    — pushed to LINE just now
            "SUCCESS_ALREADY_ACCEPTED"   — LINE had already accepted this retry key (409)
        """
        reservation = self.repository.reserve(
            trading_date=trading_date,
            strategy_version=strategy_version,
            target_id=target_id,
            message_version=message_version,
            message=message,
        )
        delivery = reservation.delivery

        if not reservation.created and delivery.status == "SUCCESS":
            return "SKIPPED_ALREADY_SENT"

        retry_key = UUID(delivery.line_retry_key)

        try:
            result = self.line_client.push_text(
                target_id=target_id, text=message, retry_key=retry_key
            )
        except (LinePushError, LineNonRetryableError) as exc:
            self.repository.mark_failed(delivery, error_message=str(exc))
            raise

        self.repository.mark_success(
            delivery,
            request_id=result.request_id,
            accepted_request_id=result.accepted_request_id,
        )

        return "SUCCESS_ALREADY_ACCEPTED" if result.already_accepted else "SUCCESS"
