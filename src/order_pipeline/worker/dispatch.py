"""Courier work-type handlers: dispatch (POST /accept) and poll_ride (GET-by-key)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import httpx

from order_pipeline.worker.classify import classify_status
from order_pipeline.worker.plugin import (
    ClaimedWork,
    GuardedTransition,
    HandlerResult,
    NextWork,
    WorkDisposition,
)
from order_pipeline.worker.settings import WorkerSettings

DISPATCH_WORK_TYPE = "dispatch"
POLL_RIDE_WORK_TYPE = "poll_ride"
CAUSE_DISPATCH = "dispatch"
CAUSE_DELIVERED = "delivered"
QUIET_TRIP_BAND = "near"


class CourierSimClient(Protocol):
    async def accept(self, *, idempotency_key: str, body: dict[str, Any]) -> httpx.Response: ...

    async def get_by_key(self, idempotency_key: str) -> httpx.Response: ...


def dispatch_idempotency_key(order_id: UUID) -> str:
    """Stored unique key `(order_id, dispatch)` — not recomputed at call time."""
    return f"({order_id}, dispatch)"


def poll_ride_idempotency_key(order_id: UUID) -> str:
    """Queue identity for the poll_ride work item — not a courier HTTP key."""
    return f"({order_id}, poll_ride)"


def parse_ready_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _payload_dict(claimed: ClaimedWork) -> dict[str, Any]:
    raw = claimed.payload
    return raw if isinstance(raw, dict) else {}


class CourierHandlers:
    def __init__(
        self,
        settings: WorkerSettings,
        client: CourierSimClient,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self._now = now_fn or (lambda: datetime.now(UTC))

    def _poll_again(self) -> HandlerResult:
        return HandlerResult(
            outcome="ok",
            disposition=WorkDisposition.RETRY,
            next_attempt_at=self._now() + timedelta(seconds=self.settings.poll_interval_s),
        )

    async def dispatch(self, claimed: ClaimedWork) -> HandlerResult:
        # Always the stored (order_id, dispatch) key. Timeout retries must not mint a new one.
        response = await self.client.accept(
            idempotency_key=claimed.idempotency_key,
            body={"band": QUIET_TRIP_BAND, "order_id": str(claimed.order_id)},
        )
        outcome = classify_status(response.status_code)
        if outcome != "ok":
            return HandlerResult(outcome=outcome)

        body = response.json()
        ticket_id = body["ticket_id"]
        raw_eta = body["estimated_ready_at"]
        eta = parse_ready_at(raw_eta)
        ticket = {
            "ticket_id": ticket_id,
            "estimated_ready_at": raw_eta,
            "accept_key": claimed.idempotency_key,
        }
        return HandlerResult(
            outcome="ok",
            transition=GuardedTransition(
                expected_state="ready",
                to_state="out_for_delivery",
                cause=CAUSE_DISPATCH,
            ),
            next_work=(
                NextWork(
                    work_type=POLL_RIDE_WORK_TYPE,
                    idempotency_key=poll_ride_idempotency_key(claimed.order_id),
                    payload=ticket,
                    next_attempt_at=eta,
                ),
            ),
            result_payload=ticket,
        )

    async def poll_ride(self, claimed: ClaimedWork) -> HandlerResult:
        payload = _payload_dict(claimed)
        accept_key = payload.get("accept_key")
        if not isinstance(accept_key, str) or not accept_key:
            return HandlerResult(outcome="unknown")

        response = await self.client.get_by_key(accept_key)
        outcome = classify_status(response.status_code)
        if outcome != "ok":
            return HandlerResult(outcome=outcome)

        status = response.json().get("status")
        # assigned / en_route stay out_for_delivery — there is no extra lifecycle stage.
        if claimed.order_state == "out_for_delivery" and status == "delivered":
            return HandlerResult(
                outcome="ok",
                transition=GuardedTransition(
                    expected_state="out_for_delivery",
                    to_state="delivered",
                    cause=CAUSE_DELIVERED,
                ),
                result_payload=payload,
            )
        return self._poll_again()
