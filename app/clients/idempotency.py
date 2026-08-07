"""
Delivery deduplication.

Two intentionally separate idempotency mechanisms:

DB idempotency key:
    trading_date + strategy_version + target_id + message_version -> SHA-256
    Used for a database UNIQUE INDEX to prevent the same delivery
    content from being recorded twice. A correction to already-sent
    content must use a new message_version, which produces a new
    idempotency key — never reuse the old one for corrected content.

LINE X-Line-Retry-Key:
    Every individual logical push attempt needs its own UUID, decided
    once and reused for every retry of that same attempt (see
    app/clients/line_client.py for why). This key's purpose is "retry
    this one push attempt safely" — a different concept from the
    business-level idempotency key above, and the two must never be
    shared.

Correct usage:
    1. One push attempt = one delivery_idempotency_key (SHA-256,
       persisted to the DB) AND one line_retry_key (UUID, also
       persisted to the DB before the first HTTP call — see
       app/db/delivery_repository.py for the crash-recovery reasoning).
    2. If that same push attempt needs to be retried (e.g. after a
       timeout or a process crash before the DB was updated), reuse
       the same UUID retry key for the resend. Only generate a new
       UUID retry key when it's genuinely a new push attempt (a
       different message_version).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID, uuid4


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_target_id(target_id: str) -> str:
    """Avoid storing the raw LINE user/group ID in the database when
    it isn't otherwise needed — only the hash is required to detect
    duplicate deliveries."""
    return sha256_text(target_id)


def create_message_hash(message: str) -> str:
    """Hash of the actual rendered text, stored alongside the
    idempotency key for debugging/audit — lets you confirm two
    deliveries with the same key really did carry the same content."""
    return sha256_text(message)


def create_delivery_idempotency_key(
    *,
    trading_date: str,
    strategy_version: str,
    target_id: str,
    message_version: str,
) -> str:
    """Business-level unique key for DB-level deduplication, distinct
    from LINE's retry key."""
    raw = f"{trading_date}:{strategy_version}:{target_id}:{message_version}"
    return sha256_text(raw)


def create_line_retry_key() -> UUID:
    """UUID required by the LINE Messaging API's X-Line-Retry-Key
    header. Reuse the same value across retries of a single push
    attempt; never regenerate it on every retry."""
    return uuid4()


@dataclass(frozen=True)
class DeliveryDecision:
    should_send: bool
    reason: str
    idempotency_key: str


class DeliveryGuard:
    """
    Pre-send idempotency check skeleton.

    A production implementation should query the message_deliveries
    table:
        SELECT status FROM message_deliveries WHERE idempotency_key = ?
    This class defines the interface and decision logic only; the
    actual DB query is delegated to the repository implementation (see
    the MessageDelivery table planned in app/db/models.py).
    """

    def __init__(self, existing_delivery_status_lookup) -> None:
        """
        existing_delivery_status_lookup: Callable[[str], str | None]
        Looks up the existing delivery record's status
        (SUCCESS / FAILED / None) by idempotency_key.
        """
        self._lookup = existing_delivery_status_lookup

    def decide(
        self,
        *,
        trading_date: str,
        strategy_version: str,
        target_id: str,
        message_version: str,
    ) -> DeliveryDecision:
        key = create_delivery_idempotency_key(
            trading_date=trading_date,
            strategy_version=strategy_version,
            target_id=target_id,
            message_version=message_version,
        )
        existing_status = self._lookup(key)

        if existing_status == "SUCCESS":
            return DeliveryDecision(
                False,
                "already delivered successfully with this idempotency key, skipping",
                key,
            )

        if existing_status == "FAILED":
            return DeliveryDecision(
                True, "previous delivery failed, retrying per retry policy", key
            )

        return DeliveryDecision(True, "first delivery attempt", key)
