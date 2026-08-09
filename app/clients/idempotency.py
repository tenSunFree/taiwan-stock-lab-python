"""
Delivery idempotency helpers.

Two intentionally separate idempotency mechanisms:

1. Database business idempotency key
   trading_date + strategy_version + hashed target + message_version
   -> SHA-256.
   Enforced via a UNIQUE constraint on MessageDelivery.idempotency_key
   (see app/db/delivery_repository.py) — that DB-level atomicity is
   what actually prevents duplicate rows, not any check-then-act logic
   in Python. A correction to already-sent content must use a new
   message_version, producing a new idempotency key; never reuse the
   old one for corrected content.

2. LINE X-Line-Retry-Key
   One UUID per logical LINE push attempt, generated once and reused
   for every retry of that same attempt (see app/clients/line_client.py
   for why). A different concept from the key above — the two must
   never be shared.

NOTE ON KEY FORMAT: create_delivery_idempotency_key() hashes the
target ID before combining it into the key material. Changing the
combination logic (delimiter, field order, or whether the target is
pre-hashed) changes every resulting key value. That's harmless before
any real delivery records exist, but once MessageDelivery rows exist
in a real database, changing this function is a breaking migration,
not a routine edit.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_target_id(target_id: str) -> str:
    """Avoid storing or hashing-in the raw LINE user/group ID directly
    — only the hash is needed to detect duplicate deliveries."""
    return sha256_text(target_id)


def create_message_hash(message: str) -> str:
    """Hash of the actual rendered text. Stored alongside the
    idempotency key so DeliveryRepository can detect the case where
    the same idempotency key was reserved but the message content
    differs from what was reserved before — see
    app/db/delivery_repository.py's DeliveryContentConflict."""
    return sha256_text(message)


def create_delivery_idempotency_key(
    *,
    trading_date: str,
    strategy_version: str,
    target_id: str,
    message_version: str,
) -> str:
    """Business-level unique key for DB-level deduplication, distinct
    from LINE's retry key. Hashes the target ID first so the raw
    target never appears in the key material, consistent with
    hash_target_id() being used everywhere else this value is stored."""
    target_id_hash = hash_target_id(target_id)
    raw = "|".join((trading_date, strategy_version, target_id_hash, message_version))
    return sha256_text(raw)


def create_line_retry_key() -> UUID:
    """UUID required by the LINE Messaging API's X-Line-Retry-Key
    header. Reuse the same value across retries of a single push
    attempt; never regenerate it on every retry."""
    return uuid4()
