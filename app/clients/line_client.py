"""
LINE Messaging API push/broadcast client.

Retry-key discipline (verified against LINE's official docs at
https://developers.line.biz/en/docs/messaging-api/retrying-api-request/):
    - One logical send attempt = one X-Line-Retry-Key (UUID), decided
      BEFORE the first HTTP call and reused for every retry of that
      same attempt. Generating a new key on each retry defeats the
      whole point of the mechanism. This applies identically to Push,
      Multicast, and Broadcast endpoints.
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

push_text() and broadcast_text() share a single _send_text_request()
implementation — the only difference between a Push and a Broadcast
call is the endpoint URL and whether the payload has a `to` field.
Keeping the retry/409/4xx/5xx handling in one place means a future
change to that logic (e.g. adjusting backoff, handling a new status
code) can't silently apply to one send mode and not the other.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID

import httpx

from app.clients.idempotency import create_line_retry_key

LINE_PUSH_ENDPOINT = "https://api.line.me/v2/bot/message/push"
LINE_BROADCAST_ENDPOINT = "https://api.line.me/v2/bot/message/broadcast"


class LinePushError(RuntimeError):
    """Raised when a send ultimately fails after exhausting retries,
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
        """Send to ONE specific user/group/room. Does not reveal
        anything about who else may or may not receive this text —
        see broadcast_text() for "send to every friend of this OA"."""
        payload = {"to": target_id, "messages": [{"type": "text", "text": text}]}
        return self._send_text_request(
            endpoint=LINE_PUSH_ENDPOINT, payload=payload, retry_key=retry_key
        )

    def broadcast_text(
        self, *, text: str, retry_key: UUID | None = None
    ) -> LinePushResult:
        """
        Send to ALL friends of this Official Account — no `to` field,
        unlike push_text(). Same retry-key/409/4xx/5xx discipline,
        via the shared _send_text_request(). The lack of a single
        target_id matters one layer up: see
        app.delivery.service.DeliveryService.deliver_broadcast()'s
        docstring for how idempotency is handled without one.
        """
        payload = {"messages": [{"type": "text", "text": text}]}
        return self._send_text_request(
            endpoint=LINE_BROADCAST_ENDPOINT, payload=payload, retry_key=retry_key
        )

    def _send_text_request(
        self,
        *,
        endpoint: str,
        payload: dict,
        retry_key: UUID | None,
    ) -> LinePushResult:
        actual_retry_key = retry_key or create_line_retry_key()

        headers = {
            "Authorization": f"Bearer {self.channel_access_token}",
            "Content-Type": "application/json",
            "X-Line-Retry-Key": str(actual_retry_key),
        }
        client = self._get_client()

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = client.post(endpoint, headers=headers, json=payload)
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
                        f"LINE server error after {attempt} attempts: "
                        f"{response.status_code}"
                    )
                self._sleep(attempt)
                continue

            raise LinePushError(
                f"Unexpected LINE response status: {response.status_code}"
            )

        raise LinePushError("LINE send exhausted all retry attempts")

    def _sleep(self, attempt: int) -> None:
        time.sleep(self.initial_backoff_seconds * (2 ** (attempt - 1)))
