from uuid import UUID

from app.clients.idempotency import (
    create_delivery_idempotency_key,
    create_line_retry_key,
    create_message_hash,
    hash_target_id,
)


def test_idempotency_key_is_deterministic():
    first = create_delivery_idempotency_key(
        trading_date="2026-08-07",
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
    )
    second = create_delivery_idempotency_key(
        trading_date="2026-08-07",
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
    )
    assert first == second
    assert len(first) == 64


def test_idempotency_key_changes_with_trading_date():
    first = create_delivery_idempotency_key(
        trading_date="2026-08-07", strategy_version="rule-v1.0.0", target_id="U123", message_version="text-v1"
    )
    second = create_delivery_idempotency_key(
        trading_date="2026-08-08", strategy_version="rule-v1.0.0", target_id="U123", message_version="text-v1"
    )
    assert first != second


def test_idempotency_key_changes_with_strategy_version():
    first = create_delivery_idempotency_key(
        trading_date="2026-08-07", strategy_version="rule-v1.0.0", target_id="U123", message_version="text-v1"
    )
    second = create_delivery_idempotency_key(
        trading_date="2026-08-07", strategy_version="rule-v1.1.0", target_id="U123", message_version="text-v1"
    )
    assert first != second


def test_idempotency_key_changes_with_target():
    first = create_delivery_idempotency_key(
        trading_date="2026-08-07", strategy_version="rule-v1.0.0", target_id="U123", message_version="text-v1"
    )
    second = create_delivery_idempotency_key(
        trading_date="2026-08-07", strategy_version="rule-v1.0.0", target_id="U456", message_version="text-v1"
    )
    assert first != second


def test_idempotency_key_changes_with_message_version():
    first = create_delivery_idempotency_key(
        trading_date="2026-08-07", strategy_version="rule-v1.0.0", target_id="U123", message_version="text-v1"
    )
    second = create_delivery_idempotency_key(
        trading_date="2026-08-07",
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v2-correction",
    )
    assert first != second


def test_target_id_hash_is_deterministic():
    assert hash_target_id("U123") == hash_target_id("U123")


def test_target_id_hash_does_not_equal_raw_value():
    target_id = "U1234567890abcdef"
    hashed = hash_target_id(target_id)
    assert hashed != target_id
    assert len(hashed) == 64


def test_message_hash_is_deterministic():
    assert create_message_hash("same content") == create_message_hash("same content")


def test_message_hash_changes_with_content():
    assert create_message_hash("report A") != create_message_hash("report B")


def test_line_retry_key_is_uuid():
    assert isinstance(create_line_retry_key(), UUID)


def test_line_retry_keys_are_unique():
    assert create_line_retry_key() != create_line_retry_key()