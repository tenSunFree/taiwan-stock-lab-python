"""
LINE Messaging API push client.

Retry-key discipline (verified against LINE's official docs at
https://developers.line.biz/en/docs/messaging-api/retrying-api-request/):
    - One logical push attempt = one X-Line-Retry-Key (UUID), decided
      BEFORE the first HTTP call and reused for every retry of that
      same attempt. Generating a new key on each retry defeats the
      whole point of the mechanism.
    - If a request with a given retry key has already been accepted,
      resending it (even after a timeout) returns 409 Conflict with an
      `x-line-accepted-request-id` header pointing at the original
      accepted request. That is treated as success, not an error.
    - Only 5xx responses and network-level timeouts are retried, with
      exponential backoff, up to max_attempts. 4xx (400/401/403/429/...)
      is never auto-retried — those indicate the request itself is
      invalid or throttled, and retrying blindly would either repeat
      the same failure or violate a rate limit further.

This uses a hand-rolled retry loop instead of a decorator-based retry
library specifically so the retry key can be pinned once, outside the
loop, and reused across every attempt inside it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

import httpx

from app.clients.idempotency import create_line_retry_key

LINE_PUSH_ENDPOINT = "https://api.line.me/v2/bot/message/push"


class LinePushError(RuntimeError):
    """Raised when a push ultimately fails after exhausting retries,
    or on a network error with no retries left."""


class LineNonRetryableError(LinePushError):
    """Raised for 4xx responses (including 429) that must not be
    auto-retried by this client."""


@dataclass(frozen=True)
class LinePushResult:
    retry_key: UUID
    success: bool
    already_accepted: bool  # True if this retry key was already accepted (409)
    status_code: int
    request_id: str | None
    accepted_request_id: str | None  # only set when already_accepted is True
    attempts: int


class LineMessagingClient:
    def __init__(
        self,
        *,
        channel_access_token: str,
        http_client: httpx.Client | None = None,
        max_attempts: int = 3,
        initial_backoff_seconds: float = 1.0,
    ) -> None:
        self.channel_access_token = channel_access_token
        self.max_attempts = max_attempts
        self.initial_backoff_seconds = initial_backoff_seconds
        self._http_client = http_client

    def _get_client(self) -> httpx.Client:
        return self._http_client or httpx.Client(timeout=10.0)

    def push_text(
        self, *, target_id: str, text: str, retry_key: UUID | None = None
    ) -> LinePushResult:
        actual_retry_key = retry_key or create_line_retry_key()

        headers = {
            "Authorization": f"Bearer {self.channel_access_token}",
            "Content-Type": "application/json",
            "X-Line-Retry-Key": str(actual_retry_key),
        }
        payload = {"to": target_id, "messages": [{"type": "text", "text": text}]}
        client = self._get_client()

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = client.post(
                    LINE_PUSH_ENDPOINT, headers=headers, json=payload
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= self.max_attempts:
                    raise LinePushError(
                        f"LINE network error after {attempt} attempts"
                    ) from exc
                self._sleep(attempt)
                continue

            request_id = response.headers.get("x-line-request-id")

            if 200 <= response.status_code < 300:
                return LinePushResult(
                    retry_key=actual_retry_key,
                    success=True,
                    already_accepted=False,
                    status_code=response.status_code,
                    request_id=request_id,
                    accepted_request_id=None,
                    attempts=attempt,
                )

            if response.status_code == 409:
                return LinePushResult(
                    retry_key=actual_retry_key,
                    success=True,
                    already_accepted=True,
                    status_code=409,
                    request_id=request_id,
                    accepted_request_id=response.headers.get(
                        "x-line-accepted-request-id"
                    ),
                    attempts=attempt,
                )

            if 400 <= response.status_code < 500:
                raise LineNonRetryableError(
                    f"LINE rejected the request: {response.status_code} {response.text}"
                )

            if 500 <= response.status_code < 600:
                if attempt >= self.max_attempts:
                    raise LinePushError(
                        f"LINE server error after {attempt} attempts: {response.status_code}"
                    )
                self._sleep(attempt)
                continue

            raise LinePushError(
                f"Unexpected LINE response status: {response.status_code}"
            )

        raise LinePushError("LINE push exhausted all retry attempts")

    def _sleep(self, attempt: int) -> None:
        time.sleep(self.initial_backoff_seconds * (2 ** (attempt - 1)))
