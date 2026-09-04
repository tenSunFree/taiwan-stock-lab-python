import datetime as dt

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clients.line_client import LineMessagingClient, LineNonRetryableError
from app.db.delivery_repository import DeliveryRepository
from app.db.models import MessageDelivery
from app.delivery.service import DeliveryService, build_message_part_version

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


def test_first_broadcast_sends_and_marks_success(session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-line-request-id": "req-broadcast-1"})

    service = make_service(session, handler)
    result = service.deliver_broadcast(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        message_version="text-v1",
        message="today's report to everyone",
    )
    assert result == "SUCCESS"


def test_broadcast_rerun_is_skipped_without_calling_line(session):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, headers={"x-line-request-id": "req-broadcast-1"})

    service = make_service(session, handler)
    service.deliver_broadcast(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        message_version="text-v1",
        message="today's report to everyone",
    )
    assert call_count["n"] == 1

    second_result = service.deliver_broadcast(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        message_version="text-v1",
        message="today's report to everyone",
    )
    assert second_result == "SKIPPED_ALREADY_SENT"
    assert call_count["n"] == 1


def test_broadcast_and_push_are_independent_deliveries(session):
    """
    A push to a specific target and a broadcast for the same
    trading_date/strategy_version/message_version must be tracked as
    two SEPARATE deliveries (different idempotency keys), since
    BROADCAST_DELIVERY_SCOPE is distinct from any real target_id.
    """
    call_log = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append(str(request.url))
        return httpx.Response(200, headers={"x-line-request-id": "req-x"})

    service = make_service(session, handler)

    push_result = service.deliver(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        target_id="U123",
        message_version="text-v1",
        message="same content",
    )
    broadcast_result = service.deliver_broadcast(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.0.0",
        message_version="text-v1",
        message="same content",
    )

    assert push_result == "SUCCESS"
    assert broadcast_result == "SUCCESS"
    assert len(call_log) == 2  # both actually called LINE, not skipped


# --- build_message_part_version -----------------------------------------


def test_build_message_part_version_pads_index_and_count():
    assert (
        build_message_part_version(base_version="text-v12", part_index=1, part_count=3)
        == "text-v12:p01-of-03"
    )
    assert (
        build_message_part_version(
            base_version="text-v12", part_index=10, part_count=12
        )
        == "text-v12:p10-of-12"
    )


def test_build_message_part_version_differs_when_part_count_changes():
    """A shape change (e.g. today's report splits into 4 parts instead
    of yesterday's 3) must NOT collide with an already-persisted
    idempotency key for the same part_index — see this function's own
    docstring for why part_count is baked into the string."""
    three_part = build_message_part_version(
        base_version="text-v12", part_index=1, part_count=3
    )
    four_part = build_message_part_version(
        base_version="text-v12", part_index=1, part_count=4
    )
    assert three_part != four_part


# --- deliver_many / deliver_broadcast_many --------------------------------


def test_deliver_many_uses_unique_part_versions(session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-line-request-id": "req-part"})

    service = make_service(session, handler)
    results = service.deliver_many(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.2.0",
        target_id="U123",
        message_version="text-v12",
        messages=["part one", "part two", "part three"],
    )

    assert results == ["SUCCESS", "SUCCESS", "SUCCESS"]

    rows = session.query(MessageDelivery).all()
    versions = {row.message_version for row in rows}
    assert versions == {
        "text-v12:p01-of-03",
        "text-v12:p02-of-03",
        "text-v12:p03-of-03",
    }
    # Each part is a distinct row with its own content hash, not one
    # row overwritten three times.
    assert len(rows) == 3


def test_deliver_many_rerun_skips_all_successful_parts(session):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200, headers={"x-line-request-id": f"req-{call_count['n']}"}
        )

    service = make_service(session, handler)
    messages = ["part one", "part two"]

    first = service.deliver_many(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.2.0",
        target_id="U123",
        message_version="text-v12",
        messages=messages,
    )
    assert first == ["SUCCESS", "SUCCESS"]
    assert call_count["n"] == 2

    second = service.deliver_many(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.2.0",
        target_id="U123",
        message_version="text-v12",
        messages=messages,
    )
    assert second == ["SKIPPED_ALREADY_SENT", "SKIPPED_ALREADY_SENT"]
    # No new LINE calls on the skipped rerun.
    assert call_count["n"] == 2


def test_deliver_many_recovers_after_partial_success(session):
    """
    Simulates a crash mid-batch: part 1 succeeds, part 2 fails with a
    non-retryable error and deliver_many propagates that exception
    (matching deliver()'s own behavior — a failure is not silently
    swallowed), part 3 is never attempted. A rerun must skip part 1
    (already SUCCESS) and retry parts 2 and 3.
    """
    call_log: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "part two" in body and "attempt-2-should-fail" not in call_log:
            call_log.append("attempt-2-should-fail")
            return httpx.Response(400, json={"message": "invalid"})
        call_log.append("ok")
        return httpx.Response(200, headers={"x-line-request-id": "req-ok"})

    service = make_service(session, handler)
    messages = ["part one", "part two", "part three"]

    with pytest.raises(LineNonRetryableError):
        service.deliver_many(
            trading_date=TRADING_DATE,
            strategy_version="rule-v1.2.0",
            target_id="U123",
            message_version="text-v12",
            messages=messages,
        )

    # Part 1 landed as SUCCESS before the batch aborted; part 3 was
    # never attempted (the loop stopped at the failing part 2).
    rows = {row.message_version: row.status for row in session.query(MessageDelivery)}
    assert rows["text-v12:p01-of-03"] == "SUCCESS"
    assert rows["text-v12:p02-of-03"] == "FAILED"
    assert "text-v12:p03-of-03" not in rows

    # Rerun: part 1 skips (no new LINE call), part 2 retries and now
    # succeeds, part 3 finally gets attempted.
    results = service.deliver_many(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.2.0",
        target_id="U123",
        message_version="text-v12",
        messages=messages,
    )
    assert results == ["SKIPPED_ALREADY_SENT", "SUCCESS", "SUCCESS"]


def test_deliver_broadcast_many_uses_unique_part_versions(session):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-line-request-id": "req-broadcast-part"})

    service = make_service(session, handler)
    results = service.deliver_broadcast_many(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.2.0",
        message_version="text-v12",
        messages=["part one", "part two"],
    )

    assert results == ["SUCCESS", "SUCCESS"]
    versions = {row.message_version for row in session.query(MessageDelivery).all()}
    assert versions == {"text-v12:p01-of-02", "text-v12:p02-of-02"}


def test_deliver_many_and_deliver_broadcast_many_parts_are_independent(session):
    """A push-many and a broadcast-many for the same trading_date/
    strategy_version/message_version must not collide, mirroring
    test_broadcast_and_push_are_independent_deliveries above for the
    single-message case."""
    call_log = []

    def handler(request: httpx.Request) -> httpx.Response:
        call_log.append(str(request.url))
        return httpx.Response(200, headers={"x-line-request-id": "req-x"})

    service = make_service(session, handler)

    push_results = service.deliver_many(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.2.0",
        target_id="U123",
        message_version="text-v12",
        messages=["same content part 1", "same content part 2"],
    )
    broadcast_results = service.deliver_broadcast_many(
        trading_date=TRADING_DATE,
        strategy_version="rule-v1.2.0",
        message_version="text-v12",
        messages=["same content part 1", "same content part 2"],
    )

    assert push_results == ["SUCCESS", "SUCCESS"]
    assert broadcast_results == ["SUCCESS", "SUCCESS"]
    assert len(call_log) == 4  # all four actually called LINE, none skipped
