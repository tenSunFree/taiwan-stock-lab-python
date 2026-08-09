import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.delivery_repository import DeliveryRepository
from app.db.models import MessageDelivery

TRADING_DATE = dt.date(2026, 8, 7)


@pytest.fixture()
def session():
    # In-memory SQLite is enough to exercise the UNIQUE-constraint
    # behavior this repository relies on; no real PostgreSQL needed
    # for these tests.
    engine = create_engine("sqlite:///:memory:")
    MessageDelivery.__table__.create(engine)
    with Session(engine) as s:
        yield s


def test_reserve_creates_a_new_pending_row(session):
    repo = DeliveryRepository(session)
    reservation = repo.reserve(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
        message="hello",
    )
    assert reservation.created is True
    assert reservation.delivery.status == "PENDING"
    assert reservation.delivery.line_retry_key  # a UUID string was assigned


def test_reserve_is_idempotent_on_identical_delivery(session):
    repo = DeliveryRepository(session)
    first = repo.reserve(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
        message="hello",
    )
    second = repo.reserve(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
        message="hello",
    )
    assert second.created is False
    assert second.delivery.delivery_id == first.delivery.delivery_id
    # crash-recovery guarantee: the SAME retry key is returned, never a new one
    assert second.delivery.line_retry_key == first.delivery.line_retry_key


def test_reserve_creates_separate_rows_for_different_message_versions(session):
    repo = DeliveryRepository(session)
    first = repo.reserve(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
        message="hello",
    )
    second = repo.reserve(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v2-correction",
        message="corrected hello",
    )
    assert first.delivery.delivery_id != second.delivery.delivery_id


def test_target_id_is_never_stored_raw(session):
    repo = DeliveryRepository(session)
    reservation = repo.reserve(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="Uraw-secret-id",
        message_version="text-v1",
        message="hello",
    )
    assert reservation.delivery.target_id_hash != "Uraw-secret-id"


def test_mark_success_updates_status_and_clears_error(session):
    repo = DeliveryRepository(session)
    reservation = repo.reserve(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
        message="hello",
    )
    repo.mark_failed(reservation.delivery, error_message="temporary network issue")
    assert reservation.delivery.status == "FAILED"

    repo.mark_success(
        reservation.delivery, request_id="req-1", accepted_request_id=None
    )
    assert reservation.delivery.status == "SUCCESS"
    assert reservation.delivery.error_message is None
    assert reservation.delivery.sent_at is not None


def test_reserve_raises_on_content_conflict_with_same_key(session):
    from app.db.delivery_repository import DeliveryContentConflict

    repo = DeliveryRepository(session)
    repo.reserve(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
        message="original content",
    )

    import pytest

    with pytest.raises(DeliveryContentConflict):
        repo.reserve(
            trading_date=TRADING_DATE,
            strategy_version="rule-v1.0.0",
            target_id="U123",
            message_version="text-v1",  # same version, different content — this is the bug case
            message="different content entirely",
        )
