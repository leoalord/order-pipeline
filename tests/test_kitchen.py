"""Kitchen handlers: first poll at ETA, confirm deadline, poll-budget park."""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.intake import confirm_idempotency_key, place_order
from order_pipeline.models import Order, OrderEvent, WorkItem
from order_pipeline.worker.chassis import Worker
from order_pipeline.worker.claim import claim_next
from order_pipeline.worker.counters import WorkerCounters
from order_pipeline.worker.finalize import CAUSE_CONFIRM_DEADLINE, finalize_claim
from order_pipeline.worker.kitchen import KitchenHandlers, parse_ready_at, poll_cook_idempotency_key
from order_pipeline.worker.plugin import ClaimedWork, HandlerResult, WorkHandler
from order_pipeline.worker.settings import WorkerSettings

TTL_HOURS = 48
TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/order_pipeline"


def _settings() -> WorkerSettings:
    return WorkerSettings(
        database_url=TEST_DATABASE_URL,
        restaurant_base_url="http://restaurant:8081",
    )


class FakeRestaurantClient:
    def __init__(
        self,
        *,
        accept_body: dict[str, Any] | None = None,
        accept_status: int = 200,
        poll_body: dict[str, Any] | None = None,
        poll_status: int = 200,
    ) -> None:
        self.accept_body = accept_body
        self.accept_status = accept_status
        self.poll_body = poll_body
        self.poll_status = poll_status
        self.accept_calls: list[tuple[str, list[str]]] = []
        self.get_calls: list[str] = []

    async def accept(self, *, idempotency_key: str, items: list[str]) -> httpx.Response:
        self.accept_calls.append((idempotency_key, items))
        return httpx.Response(self.accept_status, json=self.accept_body or {})

    async def get_by_key(self, idempotency_key: str) -> httpx.Response:
        self.get_calls.append(idempotency_key)
        return httpx.Response(self.poll_status, json=self.poll_body or {})


def _seed_and_claim(
    factory: sessionmaker[Session],
    *,
    now: datetime,
    worker_id: str,
    work_types: tuple[str, ...] = ("confirm",),
) -> tuple[uuid.UUID, uuid.UUID, str, ClaimedWork]:
    """Place and claim in one commit so the compose worker never sees a due pending row.

    Lease stamping must use wall-clock `now`. A frozen 2026 clock makes
    `lease_until` already expired vs the live SKIP LOCKED loop.
    """
    with factory.begin() as session:
        order = place_order(
            session,
            place_key=f"kitchen-{uuid.uuid4()}",
            items=["burrito"],
            cohort_id=None,
            ttl_hours=TTL_HOURS,
            now=now,
        )
        item = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
        claimed = claim_next(
            session,
            now=now,
            lease_s=15.0,
            worker_id=worker_id,
            work_types=work_types,
            work_item_id=item.id,
        )
        assert claimed is not None
        return order.id, item.id, item.idempotency_key, claimed


def _claim(
    session: Session,
    item_id: uuid.UUID,
    *,
    now: datetime,
    worker_id: str,
    work_types: tuple[str, ...] = ("confirm",),
) -> ClaimedWork | None:
    return claim_next(
        session,
        now=now,
        lease_s=15.0,
        worker_id=worker_id,
        work_types=work_types,
        work_item_id=item_id,
    )


async def _never_confirm(_claimed: ClaimedWork) -> HandlerResult:
    raise AssertionError("confirm handler must not run after the deadline")


def test_first_cook_poll_scheduled_at_estimated_ready_at(
    db_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    now = datetime.now(UTC)
    eta = now + timedelta(seconds=25)
    raw_eta = eta.isoformat().replace("+00:00", "Z")
    parsed_eta = parse_ready_at(raw_eta)
    worker_id = "kitchen-eta"
    order_id, item_id, stored_key, claimed = _seed_and_claim(
        session_factory, now=now, worker_id=worker_id
    )
    restaurant = FakeRestaurantClient(
        accept_body={
            "ticket_id": "ticket-burrito",
            "estimated_ready_at": raw_eta,
            "status": "cooking",
        }
    )
    settings = _settings()
    kitchen = KitchenHandlers(settings, restaurant)
    worker = Worker(
        settings,
        db_engine,
        handlers={"confirm": kitchen.confirm},
        worker_id=worker_id,
    )

    asyncio.run(worker.process(claimed))

    assert restaurant.accept_calls == [(stored_key, ["burrito"])]
    with session_factory() as session:
        order = session.get(Order, order_id)
        confirm_item = session.get(WorkItem, item_id)
        poll_item = session.scalars(
            select(WorkItem).where(WorkItem.order_id == order_id, WorkItem.work_type == "poll_cook")
        ).one()
        assert order is not None
        assert confirm_item is not None
        assert order.state == "confirmed"
        assert confirm_item.status == "completed"
        assert poll_item.idempotency_key == poll_cook_idempotency_key(order_id)
        assert poll_item.idempotency_key != stored_key
        assert poll_item.next_attempt_at == parsed_eta
        assert poll_item.next_attempt_at != now
        assert poll_item.status == "pending"
        payload = poll_item.payload
        assert isinstance(payload, dict)
        assert payload["ticket_id"] == "ticket-burrito"
        assert payload["estimated_ready_at"] == raw_eta
        assert payload["accept_key"] == stored_key == confirm_idempotency_key(order_id)


def test_confirm_deadline_compare_fails_the_order(
    db_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    now = datetime.now(UTC)
    worker_id = "kitchen-deadline"
    order_id, item_id, stored_key, claimed = _seed_and_claim(
        session_factory, now=now, worker_id=worker_id
    )
    past_deadline = now + timedelta(seconds=120)
    worker = Worker(
        _settings(),
        db_engine,
        handlers={"confirm": cast(WorkHandler, _never_confirm)},
        now_fn=lambda: past_deadline,
        worker_id=worker_id,
    )

    asyncio.run(worker.process(claimed))

    with session_factory() as session:
        order = session.get(Order, order_id)
        item = session.get(WorkItem, item_id)
        assert order is not None
        assert item is not None
        assert order.state == "failed"
        assert item.status == "failed"
        assert item.idempotency_key == stored_key
        assert item.lease_owner is None
        event = session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id,
                OrderEvent.cause == CAUSE_CONFIRM_DEADLINE,
            )
        ).one()
        assert event.applied is True
        assert event.to_state == "failed"


def test_poll_exhaustion_parks_the_item(session_factory: sessionmaker[Session]) -> None:
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        order = place_order(
            session,
            place_key=f"park-{uuid.uuid4()}",
            items=["burrito"],
            cohort_id=None,
            ttl_hours=TTL_HOURS,
            now=now,
        )
        order.state = "confirmed"
        order.version += 1
        confirm = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
        confirm.status = "completed"
        confirm.next_attempt_at = None
        poll = WorkItem(
            order_id=order.id,
            work_type="poll_cook",
            status="pending",
            idempotency_key=poll_cook_idempotency_key(order.id),
            attempt_count=29,
            next_attempt_at=now,
            payload={
                "ticket_id": "ticket-park",
                "estimated_ready_at": (now + timedelta(seconds=25)).isoformat(),
                "accept_key": confirm_idempotency_key(order.id),
            },
        )
        session.add(poll)
        session.flush()
        claimed = _claim(
            session, poll.id, now=now, worker_id="kitchen-park", work_types=("poll_cook",)
        )
        assert claimed is not None
        assert claimed.attempt_count == 30
        order_id, item_id = order.id, poll.id

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            HandlerResult(outcome="timeout"),
            settings=_settings(),
            counters=WorkerCounters(),
            now=now,
            rng=random.Random(0),
        )

    with session_factory() as session:
        parked_order = session.get(Order, order_id)
        item = session.get(WorkItem, item_id)
        assert parked_order is not None
        assert item is not None
        assert parked_order.state == "confirmed"
        assert item.status == "parked"
        assert item.park_owner == "kitchen-park"
        assert item.park_reason == "poll_budget_exhausted"
        assert item.park_next_action == "redrive"
        assert item.lease_owner is None
        assert item.lease_until is None
