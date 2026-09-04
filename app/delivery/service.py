"""
Delivery orchestration: reserve a delivery slot in the DB, then push
to LINE using the retry key that was already persisted for that slot.

deliver() (single target, Push API) and deliver_broadcast() (all OA
friends, Broadcast API) share the exact same reserve -> send ->
mark_success/mark_failed orchestration via _deliver() — the only
difference is which LineMessagingClient method gets called and what
"target" identifies this delivery for idempotency purposes.

Broadcast has no single LINE target_id to hash into the idempotency
key the way Push does. Rather than changing MessageDelivery's schema
to support a "no target" case, deliver_broadcast() reuses the same
target_id_hash mechanism with a fixed logical scope string
(BROADCAST_DELIVERY_SCOPE) standing in for "the target". This keeps
DeliveryRepository and the DB schema completely unchanged — see that
constant's own docstring for the resulting idempotency semantics.

As of text-v12 (explainable signals + multi-message reports), this
module also adds deliver_many() / deliver_broadcast_many() for
sending a report that app.reports.text_renderer.render_daily_report_messages()
split into several LINE messages. A naive loop calling deliver() with
the SAME message_version for every part would NOT work: reserve()'s
idempotency_key is (trading_date, strategy_version, target_id,
message_version) — it does not include message content — so the
second part's reserve() call would hit the row already created by the
first part and raise DeliveryContentConflict, since the two parts'
message_hash differ under the same key. deliver_many() avoids this by
giving each part its own message_version via
build_message_part_version(), so each part gets its own independent
idempotency identity, its own row, and its own crash-recovery
guarantees — a crash after part 2 of 3 succeeded, followed by a
rerun, correctly SKIPs parts 1-2 and only (re)sends part 3.
"""

from __future__ import annotations

import datetime as dt
from typing import Callable
from uuid import UUID

from app.clients.line_client import (
    LineMessagingClient,
    LineNonRetryableError,
    LinePushError,
    LinePushResult,
)
from app.db.delivery_repository import DeliveryRepository

# Not a real LINE target_id — a fixed logical "audience" label used
# only so the existing target_id_hash-based idempotency key can be
# reused for broadcast deliveries without any schema change. Because
# this value never changes, the idempotency key it feeds into
# (trading_date + strategy_version + hash(this) + message_version)
# answers exactly one question: "has today's broadcast for this
# strategy/message version already succeeded?" — NOT "has every
# current friend received it?". A friend who joins after a successful
# broadcast will not retroactively receive that day's report; that is
# a deliberate scope decision, not a bug — see the module docstring
# above.
BROADCAST_DELIVERY_SCOPE = "line:broadcast:all-friends:v1"


def build_message_part_version(
    *, base_version: str, part_index: int, part_count: int
) -> str:
    """
    Turns one logical message_version into a per-part idempotency
    identity, e.g. "text-v12" + part 1 of 3 -> "text-v12:p01-of-03".
    part_count is baked into the string (not just part_index) so that
    if a future code change alters how many parts a day's report
    splits into, the resulting idempotency keys differ from any
    already-persisted rows for that trading_date — a shape change is
    itself a content change and must not silently reuse old keys.
    """
    return f"{base_version}:p{part_index:02d}-of-{part_count:02d}"


class DeliveryService:
    def __init__(
        self, *, repository: DeliveryRepository, line_client: LineMessagingClient
    ) -> None:
        self.repository = repository
        self.line_client = line_client

    def deliver(
        self,
        *,
        trading_date: dt.date,
        strategy_version: str,
        target_id: str,
        message_version: str,
        message: str,
    ) -> str:
        """
        Send to ONE specific LINE user/group/room via the Push API.

        Returns one of:
            "SKIPPED_ALREADY_SENT"      — a prior run already succeeded
            "SUCCESS"                    — pushed to LINE just now
            "SUCCESS_ALREADY_ACCEPTED"   — LINE had already accepted this retry key (409)
        """
        return self._deliver(
            trading_date=trading_date,
            strategy_version=strategy_version,
            delivery_scope=target_id,
            message_version=message_version,
            message=message,
            send=lambda retry_key: self.line_client.push_text(
                target_id=target_id, text=message, retry_key=retry_key
            ),
        )

    def deliver_broadcast(
        self,
        *,
        trading_date: dt.date,
        strategy_version: str,
        message_version: str,
        message: str,
    ) -> str:
        """
        Send to ALL friends of this Official Account via the
        Broadcast API. Same return values / idempotency / crash-
        recovery guarantees as deliver() — see BROADCAST_DELIVERY_SCOPE
        for how this reuses the single-target idempotency mechanism
        without a real target_id.
        """
        return self._deliver(
            trading_date=trading_date,
            strategy_version=strategy_version,
            delivery_scope=BROADCAST_DELIVERY_SCOPE,
            message_version=message_version,
            message=message,
            send=lambda retry_key: self.line_client.broadcast_text(
                text=message, retry_key=retry_key
            ),
        )

    def deliver_many(
        self,
        *,
        trading_date: dt.date,
        strategy_version: str,
        target_id: str,
        message_version: str,
        messages: list[str],
    ) -> list[str]:
        """
        Deliver a multi-part report to ONE target via the Push API.
        Each part gets its own idempotency identity (see
        build_message_part_version) so a crash after part 2 of 3
        succeeded, followed by a rerun, correctly SKIPs parts 1-2 and
        only sends part 3 — see this module's own docstring for why a
        naive loop calling deliver() with the SAME message_version for
        every part would instead raise DeliveryContentConflict on the
        second call.

        If a part fails (raises), this loop does NOT catch it — the
        exception propagates immediately, matching deliver()'s own
        behavior of never swallowing a failure. Parts already
        delivered before the failure remain persisted as SUCCESS;
        parts after the failing one are simply never attempted this
        run. A rerun of the same messages list will skip the
        already-succeeded parts and retry from the failed one.
        """
        part_count = len(messages)
        results = []
        for index, message in enumerate(messages, start=1):
            part_version = build_message_part_version(
                base_version=message_version, part_index=index, part_count=part_count
            )
            results.append(
                self.deliver(
                    trading_date=trading_date,
                    strategy_version=strategy_version,
                    target_id=target_id,
                    message_version=part_version,
                    message=message,
                )
            )
        return results

    def deliver_broadcast_many(
        self,
        *,
        trading_date: dt.date,
        strategy_version: str,
        message_version: str,
        messages: list[str],
    ) -> list[str]:
        """Broadcast sibling of deliver_many — same per-part idempotency
        identity scheme and same fail-fast/no-swallowing behavior, see
        that method's docstring."""
        part_count = len(messages)
        results = []
        for index, message in enumerate(messages, start=1):
            part_version = build_message_part_version(
                base_version=message_version, part_index=index, part_count=part_count
            )
            results.append(
                self.deliver_broadcast(
                    trading_date=trading_date,
                    strategy_version=strategy_version,
                    message_version=part_version,
                    message=message,
                )
            )
        return results

    def _deliver(
        self,
        *,
        trading_date: dt.date,
        strategy_version: str,
        delivery_scope: str,
        message_version: str,
        message: str,
        send: Callable[[UUID], LinePushResult],
    ) -> str:
        reservation = self.repository.reserve(
            trading_date=trading_date,
            strategy_version=strategy_version,
            target_id=delivery_scope,
            message_version=message_version,
            message=message,
        )
        delivery = reservation.delivery

        if not reservation.created and delivery.status == "SUCCESS":
            return "SKIPPED_ALREADY_SENT"

        retry_key = UUID(delivery.line_retry_key)

        try:
            result = send(retry_key)
        except (LinePushError, LineNonRetryableError) as exc:
            self.repository.mark_failed(delivery, error_message=str(exc))
            raise

        self.repository.mark_success(
            delivery,
            request_id=result.request_id,
            accepted_request_id=result.accepted_request_id,
        )

        return "SUCCESS_ALREADY_ACCEPTED" if result.already_accepted else "SUCCESS"
