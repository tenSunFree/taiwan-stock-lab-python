from uuid import UUID

from app.clients.idempotency import (
    DeliveryGuard,
    create_delivery_idempotency_key,
    create_line_retry_key,
    create_message_hash,
    hash_target_id,
)


def test_idempotency_key_is_deterministic():
    key1 = create_delivery_idempotency_key(
        trading_date="2026-08-07",
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="v1",
    )
    key2 = create_delivery_idempotency_key(
        trading_date="2026-08-07",
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="v1",
    )
    assert key1 == key2
    assert len(key1) == 64  # sha256 hex digest length


def test_idempotency_key_changes_with_message_version():
    key1 = create_delivery_idempotency_key(
        trading_date="2026-08-07",
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="v1",
    )
    key2 = create_delivery_idempotency_key(
        trading_date="2026-08-07",
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="v2-correction",
    )
    assert key1 != key2


def test_line_retry_key_is_uuid_and_unique():
    key1 = create_line_retry_key()
    key2 = create_line_retry_key()
    assert isinstance(key1, UUID)
    assert key1 != key2


def test_delivery_guard_skips_when_already_succeeded():
    guard = DeliveryGuard(lambda key: "SUCCESS")
    decision = guard.decide(
        trading_date="2026-08-07",
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="v1",
    )
    assert decision.should_send is False


def test_delivery_guard_retries_when_previously_failed():
    guard = DeliveryGuard(lambda key: "FAILED")
    decision = guard.decide(
        trading_date="2026-08-07",
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="v1",
    )
    assert decision.should_send is True
    assert "failed" in decision.reason


def test_delivery_guard_sends_when_no_existing_record():
    guard = DeliveryGuard(lambda key: None)
    decision = guard.decide(
        trading_date="2026-08-07",
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="v1",
    )
    assert decision.should_send is True
    assert decision.reason == "first delivery attempt"


def test_message_hash_changes_with_content():
    assert create_message_hash("report A") != create_message_hash("report B")


def test_message_hash_is_deterministic():
    assert create_message_hash("same content") == create_message_hash("same content")


def test_hash_target_id_never_returns_the_raw_value():
    target_id = "U1234567890abcdef"
    hashed = hash_target_id(target_id)
    assert hashed != target_id
    assert len(hashed) == 64
