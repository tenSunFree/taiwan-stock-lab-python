from uuid import uuid4

import httpx
import pytest
import json

from app.clients.line_client import (
    LineMessagingClient,
    LineNonRetryableError,
    LinePushError,
)


def make_client(handler, **kwargs) -> LineMessagingClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    return LineMessagingClient(
        channel_access_token="fake-token", http_client=http_client, **kwargs
    )


def test_push_text_success_sends_retry_key_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["retry_key_header"] = request.headers.get("X-Line-Retry-Key")
        captured["auth_header"] = request.headers.get("Authorization")
        return httpx.Response(200, headers={"x-line-request-id": "req-1"})

    client = make_client(handler)
    result = client.push_text(target_id="U123", text="hello")

    assert result.status_code == 200
    assert result.success is True
    assert result.already_accepted is False
    assert captured["retry_key_header"] == str(result.retry_key)
    assert captured["auth_header"] == "Bearer fake-token"


def test_push_text_uses_provided_retry_key():
    fixed_key = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Line-Retry-Key") == str(fixed_key)
        return httpx.Response(200, headers={"x-line-request-id": "req-2"})

    client = make_client(handler)
    result = client.push_text(target_id="U123", text="hello", retry_key=fixed_key)
    assert result.retry_key == fixed_key


def test_push_text_409_returns_already_accepted_with_original_request_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            headers={
                "x-line-request-id": "retry-attempt-id",
                "x-line-accepted-request-id": "original-accepted-id",
            },
        )

    client = make_client(handler)
    result = client.push_text(target_id="U123", text="hello")
    assert result.status_code == 409
    assert result.success is True
    assert result.already_accepted is True
    assert result.accepted_request_id == "original-accepted-id"


def test_push_text_4xx_raises_without_auto_retry():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(400, json={"message": "invalid request"})

    client = make_client(handler)
    with pytest.raises(LineNonRetryableError):
        client.push_text(target_id="U123", text="hello")

    assert call_count["n"] == 1  # 4xx (including 429) must never be auto-retried


def test_push_text_429_raises_without_auto_retry():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(429)

    client = make_client(handler)
    with pytest.raises(LineNonRetryableError):
        client.push_text(target_id="U123", text="hello")

    assert call_count["n"] == 1


def test_push_text_5xx_retries_then_raises():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(500)

    client = make_client(handler, max_attempts=3, initial_backoff_seconds=0)
    with pytest.raises(LinePushError):
        client.push_text(target_id="U123", text="hello")

    assert call_count["n"] == 3


def test_5xx_retry_reuses_the_same_retry_key():
    """
    Regression test for the bug found in review: the retry key must
    NOT change between attempts of the same logical push. Each
    attempt's X-Line-Retry-Key header is captured and they must all
    be identical.
    """
    seen_retry_keys = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_retry_keys.append(request.headers.get("X-Line-Retry-Key"))
        if len(seen_retry_keys) < 3:
            return httpx.Response(500)
        return httpx.Response(200, headers={"x-line-request-id": "req-final"})

    client = make_client(handler, max_attempts=3, initial_backoff_seconds=0)
    result = client.push_text(target_id="U123", text="hello")

    assert result.success is True
    assert len(seen_retry_keys) == 3
    assert len(set(seen_retry_keys)) == 1  # every attempt used the exact same key
    assert seen_retry_keys[0] == str(result.retry_key)


def test_timeout_then_success_reuses_the_same_retry_key():
    seen_retry_keys = []
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            seen_retry_keys.append(request.headers.get("X-Line-Retry-Key"))
            raise httpx.TimeoutException("simulated timeout", request=request)
        seen_retry_keys.append(request.headers.get("X-Line-Retry-Key"))
        return httpx.Response(200, headers={"x-line-request-id": "req-after-timeout"})

    client = make_client(handler, max_attempts=3, initial_backoff_seconds=0)
    result = client.push_text(target_id="U123", text="hello")

    assert result.success is True
    assert (
        len(set(seen_retry_keys)) == 1
    )  # timeout attempt and the retry used the same key
    assert seen_retry_keys[0] == str(result.retry_key)


def test_broadcast_text_has_no_to_field():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"x-line-request-id": "broadcast-1"})

    client = make_client(
        handler
    )  # 沿用你既有的 test_line_client.py 裡的 make_client() helper
    result = client.broadcast_text(text="hello family")

    assert captured["url"].endswith("/v2/bot/message/broadcast")
    assert captured["body"] == {"messages": [{"type": "text", "text": "hello family"}]}
    assert "to" not in captured["body"]
    assert result.success is True


def test_broadcast_text_uses_provided_retry_key():
    from uuid import uuid4

    captured = {}
    fixed_key = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        captured["retry_key_header"] = request.headers.get("X-Line-Retry-Key")
        return httpx.Response(200, headers={"x-line-request-id": "broadcast-2"})

    client = make_client(handler)
    result = client.broadcast_text(text="hello", retry_key=fixed_key)

    assert captured["retry_key_header"] == str(fixed_key)
    assert result.retry_key == fixed_key


def test_broadcast_text_409_is_already_accepted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, headers={"x-line-accepted-request-id": "already-1"})

    client = make_client(handler)
    result = client.broadcast_text(text="hello")

    assert result.success is True
    assert result.already_accepted is True
    assert result.accepted_request_id == "already-1"


def test_broadcast_text_5xx_retries_then_succeeds():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 2:
            return httpx.Response(503)
        return httpx.Response(200, headers={"x-line-request-id": "broadcast-3"})

    client = make_client(handler, initial_backoff_seconds=0)
    result = client.broadcast_text(text="hello")

    assert call_count["n"] == 2
    assert result.success is True
