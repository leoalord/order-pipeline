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


def first_cook_poll_at(
    *,
    now: datetime,
    estimated_ready_at: datetime,
    service_started_at: datetime | None,
    poll_interval_s: float,
) -> datetime:
    """Poll when the pan is due, not at t=0 and not only at ETA.

    Queued tickets wait until ``service_started_at``. Already-cooking tickets dwell
    one poll interval so `/` can observe confirmed, then ``being_prepared``.
    Missing ``service_started_at`` keeps the pre-rail schedule (first poll at ETA).
    """
    start = service_started_at if service_started_at is not None else estimated_ready_at
    if start > now:
        return start
    return now + timedelta(seconds=poll_interval_s)


def _optional_time(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    return parse_ready_at(raw)


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
        raw_start = body.get("service_started_at")
        service_started_at = _optional_time(raw_start)
        ticket: dict[str, Any] = {
            "ticket_id": ticket_id,
            "estimated_ready_at": raw_eta,
            "accept_key": claimed.idempotency_key,
        }
        raw_accepted = body.get("accepted_at")
        if isinstance(raw_accepted, str) and raw_accepted:
            ticket["accepted_at"] = raw_accepted
        if isinstance(raw_start, str) and raw_start:
            ticket["service_started_at"] = raw_start
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
                    next_attempt_at=first_cook_poll_at(
                        now=self._now(),
                        estimated_ready_at=eta,
                        service_started_at=service_started_at,
                        poll_interval_s=self.settings.poll_interval_s,
                    ),
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
        if status == "queued":
            # Waiting for a pan: stay confirmed. Do not treat this as cooking.
            start = _optional_time(payload.get("service_started_at"))
            if start is not None and start > self._now():
                return HandlerResult(
                    outcome="ok",
                    disposition=WorkDisposition.RETRY,
                    next_attempt_at=start,
                )
            return self._poll_again()

        cooking_started = status in {"cooking", "ready"}
        if claimed.order_state == "confirmed" and cooking_started:
            # On a pan → being_prepared. Next poll at ETA so cook does not burn the budget.
            eta = _optional_time(payload.get("estimated_ready_at"))
            next_at = (
                eta
                if eta is not None and eta > self._now()
                else self._now() + timedelta(seconds=self.settings.poll_interval_s)
            )
            return HandlerResult(
                outcome="ok",
                disposition=WorkDisposition.RETRY,
                transition=GuardedTransition(
                    expected_state="confirmed",
                    to_state="being_prepared",
                    cause=CAUSE_COOKING_STARTED,
                ),
                next_attempt_at=next_at,
            )
        if claimed.order_state == "being_prepared" and status == "ready":
            # Dwell so GET and `/` can observe ready. Dispatch at now skips the card.
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
                        next_attempt_at=self._now()
                        + timedelta(seconds=self.settings.poll_interval_s),
                    ),
                ),
                result_payload=payload,
            )
        return self._poll_again()
