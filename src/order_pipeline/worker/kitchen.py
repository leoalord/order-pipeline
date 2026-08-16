"""Kitchen work-type handlers: confirm (POST /accept) and poll_cook (GET-by-key)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import httpx

from order_pipeline.worker.classify import classify_status
from order_pipeline.worker.dispatch import DISPATCH_WORK_TYPE, dispatch_idempotency_key
from order_pipeline.worker.plugin import (
    ClaimedWork,
    GuardedTransition,
    HandlerResult,
    NextWork,
    WorkDisposition,
)
from order_pipeline.worker.settings import WorkerSettings

POLL_COOK_WORK_TYPE = "poll_cook"
CAUSE_CONFIRM = "confirm"
CAUSE_COOKING_STARTED = "cooking_started"
CAUSE_READY = "ready"


class KitchenClient(Protocol):
    async def accept(self, *, idempotency_key: str, items: list[str]) -> httpx.Response: ...

    async def get_by_key(self, idempotency_key: str) -> httpx.Response: ...


def poll_cook_idempotency_key(order_id: UUID) -> str:
    """Queue identity for the poll_cook work item — not a restaurant HTTP key."""
    return f"({order_id}, poll_cook)"


def parse_ready_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _cart_items(claimed: ClaimedWork) -> list[str] | None:
    raw = claimed.items
    if not isinstance(raw, list) or not raw:
        return None
    items = [item for item in raw if isinstance(item, str)]
    if len(items) != len(raw):
        return None
    return items


def _payload_dict(claimed: ClaimedWork) -> dict[str, Any]:
    raw = claimed.payload
    return raw if isinstance(raw, dict) else {}


class KitchenHandlers:
    def __init__(
        self,
        settings: WorkerSettings,
        client: KitchenClient,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self._now = now_fn or (lambda: datetime.now(UTC))

    def _poll_again(self, *, transition: GuardedTransition | None = None) -> HandlerResult:
        return HandlerResult(
            outcome="ok",
            disposition=WorkDisposition.RETRY,
            transition=transition,
            next_attempt_at=self._now() + timedelta(seconds=self.settings.poll_interval_s),
        )

    async def confirm(self, claimed: ClaimedWork) -> HandlerResult:
        items = _cart_items(claimed)
        if items is None:
            return HandlerResult(outcome="http_4xx")

        # Always the stored place_order key. Timeout retries must not mint a new one.
        response = await self.client.accept(
            idempotency_key=claimed.idempotency_key,
            items=items,
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
                expected_state="placed",
                to_state="confirmed",
                cause=CAUSE_CONFIRM,
            ),
            next_work=(
                NextWork(
                    work_type=POLL_COOK_WORK_TYPE,
                    idempotency_key=poll_cook_idempotency_key(claimed.order_id),
                    payload=ticket,
                    next_attempt_at=eta,
                ),
            ),
            result_payload=ticket,
        )

    async def poll_cook(self, claimed: ClaimedWork) -> HandlerResult:
        payload = _payload_dict(claimed)
        accept_key = payload.get("accept_key")
        if not isinstance(accept_key, str) or not accept_key:
            return HandlerResult(outcome="unknown")

        response = await self.client.get_by_key(accept_key)
        outcome = classify_status(response.status_code)
        if outcome != "ok":
            return HandlerResult(outcome=outcome)

        status = response.json().get("status")
        cooking_started = status in {"cooking", "ready"}
        if claimed.order_state == "confirmed" and cooking_started:
            # Own commit so GET can observe being_prepared before ready.
            return self._poll_again(
                transition=GuardedTransition(
                    expected_state="confirmed",
                    to_state="being_prepared",
                    cause=CAUSE_COOKING_STARTED,
                )
            )
        if claimed.order_state == "being_prepared" and status == "ready":
            return HandlerResult(
                outcome="ok",
                transition=GuardedTransition(
                    expected_state="being_prepared",
                    to_state="ready",
                    cause=CAUSE_READY,
                ),
                next_work=(
                    NextWork(
                        work_type=DISPATCH_WORK_TYPE,
                        idempotency_key=dispatch_idempotency_key(claimed.order_id),
                        next_attempt_at=self._now(),
                    ),
                ),
                result_payload=payload,
            )
        return self._poll_again()
