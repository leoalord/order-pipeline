"""Dispatch + poll-ride handlers: stored key, GET-by-key, park-on-exhaust."""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.intake import place_order
from order_pipeline.models import Order, OrderEvent, WorkItem
from order_pipeline.worker.chassis import Worker
from order_pipeline.worker.claim import claim_next
from order_pipeline.worker.counters import WorkerCounters
from order_pipeline.worker.dispatch import (
    CAUSE_DELIVERED,
    CAUSE_DISPATCH,
    CourierHandlers,
    dispatch_idempotency_key,
    parse_ready_at,
    poll_ride_idempotency_key,
)
from order_pipeline.worker.finalize import finalize_claim
from order_pipeline.worker.kitchen import KitchenHandlers, poll_cook_idempotency_key
from order_pipeline.worker.plugin import (
    ClaimedWork,
    HandlerResult,
    WorkDisposition,
)
from order_pipeline.worker.settings import WorkerSettings

TTL_HOURS = 48
TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/order_pipeline"


def _settings() -> WorkerSettings:
    return WorkerSettings(
        database_url=TEST_DATABASE_URL,
        restaurant_base_url="http://restaurant:8081",
    )


class FakeCourierClient:
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
        self.accept_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_calls: list[str] = []

    async def accept(self, *, idempotency_key: str, body: dict[str, Any]) -> httpx.Response:
        self.accept_calls.append((idempotency_key, body))
        return httpx.Response(self.accept_status, json=self.accept_body or {})

    async def get_by_key(self, idempotency_key: str) -> httpx.Response:
        self.get_calls.append(idempotency_key)
        return httpx.Response(self.poll_status, json=self.poll_body or {})


class FakeRestaurantClient:
    def __init__(self, *, poll_body: dict[str, Any] | None = None) -> None:
        self.poll_body = poll_body or {}
        self.get_calls: list[str] = []

    async def accept(self, *, idempotency_key: str, items: list[str]) -> httpx.Response:
        raise AssertionError("confirm must not run in this test")

    async def get_by_key(self, idempotency_key: str) -> httpx.Response:
        self.get_calls.append(idempotency_key)
        return httpx.Response(200, json=self.poll_body)


def _claim(
    session: Session,
    item_id: uuid.UUID,
    *,
    now: datetime,
    worker_id: str,
    work_types: tuple[str, ...],
) -> ClaimedWork | None:
    return claim_next(
        session,
        now=now,
        lease_s=15.0,
        worker_id=worker_id,
        work_types=work_types,
        work_item_id=item_id,
    )


def _seed_ready_dispatch(
    factory: sessionmaker[Session],
    *,
    now: datetime,
    worker_id: str,
    attempt_count: int = 0,
) -> tuple[uuid.UUID, uuid.UUID, str, ClaimedWork]:
    with factory.begin() as session:
        order = place_order(
            session,
            place_key=f"dispatch-{uuid.uuid4()}",
            items=["chips"],
            cohort_id=None,
            ttl_hours=TTL_HOURS,
            now=now,
        )
        order.state = "ready"
        order.version += 1
        confirm = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
        confirm.status = "completed"
        confirm.next_attempt_at = None
        stored_key = dispatch_idempotency_key(order.id)
        item = WorkItem(
            order_id=order.id,
            work_type="dispatch",
            status="pending",
            idempotency_key=stored_key,
            attempt_count=attempt_count,
            next_attempt_at=now,
        )
        session.add(item)
        session.flush()
        claimed = _claim(session, item.id, now=now, worker_id=worker_id, work_types=("dispatch",))
        assert claimed is not None
        return order.id, item.id, stored_key, claimed


def _seed_ride(
    factory: sessionmaker[Session],
    *,
    now: datetime,
    worker_id: str,
    attempt_count: int = 0,
    accept_key: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str, ClaimedWork]:
    with factory.begin() as session:
        order = place_order(
            session,
            place_key=f"ride-{uuid.uuid4()}",
            items=["chips"],
            cohort_id=None,
            ttl_hours=TTL_HOURS,
            now=now,
        )
        order.state = "out_for_delivery"
        order.version += 1
        confirm = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
        confirm.status = "completed"
        confirm.next_attempt_at = None
        dispatch_key = accept_key or dispatch_idempotency_key(order.id)
        ride_key = poll_ride_idempotency_key(order.id)
        item = WorkItem(
            order_id=order.id,
            work_type="poll_ride",
            status="pending",
            idempotency_key=ride_key,
            attempt_count=attempt_count,
            next_attempt_at=now,
            payload={
                "ticket_id": "ticket-ride",
                "estimated_ready_at": (now + timedelta(seconds=12)).isoformat(),
                "accept_key": dispatch_key,
            },
        )
        session.add(item)
        session.flush()
        claimed = _claim(session, item.id, now=now, worker_id=worker_id, work_types=("poll_ride",))
        assert claimed is not None
        return order.id, item.id, dispatch_key, claimed


def test_ready_enqueues_dispatch_work_item(
    db_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    now = datetime.now(UTC)
    worker_id = "enqueue-dispatch"
    with session_factory.begin() as session:
        order = place_order(
            session,
            place_key=f"ready-{uuid.uuid4()}",
            items=["chips"],
            cohort_id=None,
            ttl_hours=TTL_HOURS,
            now=now,
        )
        order.state = "being_prepared"
        order.version += 1
        confirm = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
        confirm.status = "completed"
        confirm.next_attempt_at = None
        accept_key = confirm.idempotency_key
        poll = WorkItem(
            order_id=order.id,
            work_type="poll_cook",
            status="pending",
            idempotency_key=poll_cook_idempotency_key(order.id),
            attempt_count=0,
            next_attempt_at=now,
            payload={
                "ticket_id": "ticket-chips",
                "estimated_ready_at": now.isoformat(),
                "accept_key": accept_key,
            },
        )
        session.add(poll)
        session.flush()
        claimed = _claim(session, poll.id, now=now, worker_id=worker_id, work_types=("poll_cook",))
        assert claimed is not None
        order_id, poll_id = order.id, poll.id

    restaurant = FakeRestaurantClient(poll_body={"status": "ready"})
    kitchen = KitchenHandlers(_settings(), restaurant, now_fn=lambda: now)
    worker = Worker(
        _settings(),
        db_engine,
        handlers={"poll_cook": kitchen.poll_cook},
        worker_id=worker_id,
        now_fn=lambda: now,
    )
    asyncio.run(worker.process(claimed))

    with session_factory() as session:
        stored = session.get(Order, order_id)
        dispatch_item = session.scalars(
            select(WorkItem).where(WorkItem.order_id == order_id, WorkItem.work_type == "dispatch")
        ).one()
        poll_item = session.get(WorkItem, poll_id)
        assert stored is not None
        assert poll_item is not None
        assert stored.state == "ready"
        assert poll_item.status == "completed"
        assert dispatch_item.idempotency_key == dispatch_idempotency_key(order_id)
        assert dispatch_item.status == "pending"
        assert dispatch_item.next_attempt_at == now + timedelta(seconds=_settings().poll_interval_s)


def test_dispatch_uses_stored_key_and_schedules_ride_at_eta(
    db_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    now = datetime.now(UTC)
    eta = now + timedelta(seconds=12)
    raw_eta = eta.isoformat().replace("+00:00", "Z")
    parsed_eta = parse_ready_at(raw_eta)
    worker_id = "dispatch-eta"
    order_id, item_id, stored_key, claimed = _seed_ready_dispatch(
        session_factory, now=now, worker_id=worker_id
    )
    courier = FakeCourierClient(
        accept_body={
            "ticket_id": "ticket-near",
            "estimated_ready_at": raw_eta,
            "status": "en_route",
        }
    )
    rides = CourierHandlers(_settings(), courier, now_fn=lambda: now)
    worker = Worker(
        _settings(),
        db_engine,
        handlers={"dispatch": rides.dispatch},
        worker_id=worker_id,
        now_fn=lambda: now,
    )

    asyncio.run(worker.process(claimed))

    assert len(courier.accept_calls) == 1
    assert courier.accept_calls[0][0] == stored_key
    assert courier.accept_calls[0][1]["band"] == "near"
    with session_factory() as session:
        order = session.get(Order, order_id)
        dispatch_item = session.get(WorkItem, item_id)
        ride_item = session.scalars(
            select(WorkItem).where(WorkItem.order_id == order_id, WorkItem.work_type == "poll_ride")
        ).one()
        event = session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id,
                OrderEvent.cause == CAUSE_DISPATCH,
                OrderEvent.applied.is_(True),
            )
        ).one()
        assert order is not None
        assert dispatch_item is not None
        assert order.state == "out_for_delivery"
        assert dispatch_item.status == "completed"
        assert dispatch_item.idempotency_key == stored_key == dispatch_idempotency_key(order_id)
        assert ride_item.idempotency_key == poll_ride_idempotency_key(order_id)
        assert ride_item.idempotency_key != stored_key
        assert ride_item.next_attempt_at == parsed_eta
        payload = ride_item.payload
        assert isinstance(payload, dict)
        assert payload["ticket_id"] == "ticket-near"
        assert payload["accept_key"] == stored_key
        assert event.to_state == "out_for_delivery"


def test_poll_ride_reuses_dispatch_key_not_queue_key(
    db_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    now = datetime.now(UTC)
    worker_id = "ride-key"
    order_id, item_id, dispatch_key, claimed = _seed_ride(
        session_factory, now=now, worker_id=worker_id
    )
    courier = FakeCourierClient(poll_body={"status": "delivered", "ticket_id": "ticket-ride"})
    rides = CourierHandlers(_settings(), courier, now_fn=lambda: now)
    worker = Worker(
        _settings(),
        db_engine,
        handlers={"poll_ride": rides.poll_ride},
        worker_id=worker_id,
        now_fn=lambda: now,
    )

    asyncio.run(worker.process(claimed))

    assert courier.get_calls == [dispatch_key]
    assert dispatch_key != poll_ride_idempotency_key(order_id)
    with session_factory() as session:
        order = session.get(Order, order_id)
        ride_item = session.get(WorkItem, item_id)
        event = session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id,
                OrderEvent.cause == CAUSE_DELIVERED,
                OrderEvent.applied.is_(True),
            )
        ).one()
        assert order is not None
        assert ride_item is not None
        assert order.state == "delivered"
        assert ride_item.status == "completed"
        assert event.from_state == "out_for_delivery"
        assert event.to_state == "delivered"


def test_poll_ride_en_route_does_not_invent_a_stage(
    db_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    now = datetime.now(UTC)
    worker_id = "ride-en-route"
    order_id, item_id, _, claimed = _seed_ride(session_factory, now=now, worker_id=worker_id)
    courier = FakeCourierClient(poll_body={"status": "en_route"})
    rides = CourierHandlers(_settings(), courier, now_fn=lambda: now)
    worker = Worker(
        _settings(),
        db_engine,
        handlers={"poll_ride": rides.poll_ride},
        worker_id=worker_id,
        now_fn=lambda: now,
    )

    asyncio.run(worker.process(claimed))

    with session_factory() as session:
        order = session.get(Order, order_id)
        ride_item = session.get(WorkItem, item_id)
        assert order is not None
        assert ride_item is not None
        assert order.state == "out_for_delivery"
        assert ride_item.status == "pending"
        assert ride_item.next_attempt_at == now + timedelta(seconds=3)
        delivered = session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id, OrderEvent.to_state == "delivered"
            )
        ).all()
        assert delivered == []


def test_dispatch_exhaustion_parks_the_item(session_factory: sessionmaker[Session]) -> None:
    now = datetime.now(UTC)
    order_id, item_id, stored_key, claimed = _seed_ready_dispatch(
        session_factory, now=now, worker_id="dispatch-park", attempt_count=4
    )
    assert claimed.attempt_count == 5

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
        assert parked_order.state == "ready"
        assert item.status == "parked"
        assert item.idempotency_key == stored_key
        assert item.park_owner == "dispatch-park"
        assert item.park_reason == "retry_budget_exhausted"
        assert item.park_next_action == "redrive"
        assert item.lease_owner is None
        assert item.lease_until is None


def test_ride_poll_exhaustion_parks_the_item(session_factory: sessionmaker[Session]) -> None:
    now = datetime.now(UTC)
    order_id, item_id, _, claimed = _seed_ride(
        session_factory, now=now, worker_id="ride-park", attempt_count=29
    )
    assert claimed.attempt_count == 30

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            HandlerResult(
                outcome="ok",
                disposition=WorkDisposition.RETRY,
                next_attempt_at=now + timedelta(seconds=3),
            ),
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
        assert parked_order.state == "out_for_delivery"
        assert item.status == "parked"
        assert item.park_reason == "poll_budget_exhausted"
        assert item.park_next_action == "redrive"
        assert item.lease_owner is None
        assert item.lease_until is None
