import datetime as dt

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clients.line_client import LineMessagingClient, LineNonRetryableError
from app.db.delivery_repository import DeliveryRepository
from app.db.models import MessageDelivery
from app.delivery.service import DeliveryService

TRADING_DATE = dt.date(2026, 8, 7)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    MessageDelivery.__table__.create(engine)
    with Session(engine) as s:
        yield s


def make_service(session, handler) -> DeliveryService:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    line_client = LineMessagingClient(
        channel_access_token="fake-token",
        http_client=http_client,
        initial_backoff_seconds=0,
    )
    repository = DeliveryRepository(session)
    return DeliveryService(repository=repository, line_client=line_client)


def test_first_delivery_sends_and_marks_success(session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-line-request-id": "req-1"})

    service = make_service(session, handler)
    result = service.deliver(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
        message="today's report",
    )
    assert result == "SUCCESS"


def test_rerun_after_success_is_skipped_without_calling_line(session):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, headers={"x-line-request-id": "req-1"})

    service = make_service(session, handler)
    service.deliver(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
        message="today's report",
    )
    assert call_count["n"] == 1

    # Simulate a rerun of the same job for the same trading date —
    # this must NOT hit the LINE API a second time.
    second_result = service.deliver(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
        message="today's report",
    )
    assert second_result == "SKIPPED_ALREADY_SENT"
    assert call_count["n"] == 1


def test_crash_before_db_update_recovers_via_409(session):
    """
    Simulates: first attempt reaches LINE and is accepted, but the
    process crashes before mark_success() runs (so the DB row is still
    PENDING). A second attempt should reuse the same persisted retry
    key, LINE should respond 409 (already accepted), and the service
    should treat that as success and update the DB accordingly.
    """
    accepted_keys = set()

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers.get("X-Line-Retry-Key")
        if key in accepted_keys:
            return httpx.Response(
                409,
                headers={
                    "x-line-request-id": "retry-id",
                    "x-line-accepted-request-id": "original-id",
                },
            )
        accepted_keys.add(key)
        return httpx.Response(200, headers={"x-line-request-id": "original-id"})

    repository = DeliveryRepository(session)
    reservation = repository.reserve(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
        message="today's report",
    )
    # Manually simulate "LINE already accepted it" by pre-populating
    # accepted_keys with the reserved retry key, without calling
    # mark_success — i.e. the crash scenario.
    accepted_keys.add(reservation.delivery.line_retry_key)

    service = make_service(session, handler)
    result = service.deliver(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
        message="today's report",
    )
    assert result == "SUCCESS_ALREADY_ACCEPTED"
    assert reservation.delivery.status == "SUCCESS"
    assert reservation.delivery.accepted_request_id == "original-id"


def test_non_retryable_failure_marks_delivery_failed_and_raises(session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "invalid"})

    service = make_service(session, handler)
    with pytest.raises(LineNonRetryableError):
        service.deliver(
            trading_date=TRADING_DATE,
            strategy_version="rule-v1.0.0",
            target_id="U123",
            message_version="text-v1",
            message="today's report",
        )
