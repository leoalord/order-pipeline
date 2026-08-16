"""Worker chassis: SKIP LOCKED, attempt-at-claim, guards, 4xx vs 429, rsim cap."""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.intake import place_order
from order_pipeline.models import Attempt, Order, OrderEvent, WorkItem
from order_pipeline.worker.claim import claim_next
from order_pipeline.worker.classify import classify_status
from order_pipeline.worker.counters import WorkerCounters
from order_pipeline.worker.deps import DepCaps
from order_pipeline.worker.finalize import (
    CAUSE_INVALID,
    CAUSE_PERMANENT_4XX,
    CAUSE_SUPERSEDED,
    finalize_claim,
)
from order_pipeline.worker.plugin import (
    ClaimedWork,
    GuardedTransition,
    HandlerResult,
    WorkDisposition,
)
from order_pipeline.worker.settings import WorkerSettings

TTL_HOURS = 48
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/order_pipeline",
)
LEASE_LIFECYCLE_CAUSES = frozenset(
    {
        "lease",
        "lease_acquired",
        "lease_dropped",
        "lease_expired",
        "lease_taken",
        "lease_released",
        "reclaim",
    }
)


def _settings(*, dep_cap_rsim: int = 8) -> WorkerSettings:
    return WorkerSettings(
        database_url=TEST_DATABASE_URL,
        restaurant_base_url="http://restaurant:8081",
        dep_cap_rsim=dep_cap_rsim,
    )


def _seed_confirm(factory: sessionmaker[Session]) -> tuple[uuid.UUID, uuid.UUID, str]:
    with factory.begin() as session:
        order = place_order(
            session,
            place_key=f"worker-{uuid.uuid4()}",
            items=["burrito"],
            cohort_id=None,
            ttl_hours=TTL_HOURS,
        )
        item = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
        return order.id, item.id, item.idempotency_key


def _claim(
    session: Session,
    item_id: uuid.UUID,
    *,
    now: datetime,
    worker_id: str,
    lease_s: float = 15.0,
) -> ClaimedWork | None:
    return claim_next(
        session,
        now=now,
        lease_s=lease_s,
        worker_id=worker_id,
        work_types=("confirm",),
        work_item_id=item_id,
    )


def test_classify_4xx_permanent_429_transient() -> None:
    assert classify_status(409) == "http_4xx"
    assert classify_status(400) == "http_4xx"
    assert classify_status(404) == "http_4xx"
    assert classify_status(429) == "http_429"
    assert classify_status(503) == "http_5xx"
    assert classify_status(200) == "ok"


def test_two_connection_skip_locked_no_double_claim(
    db_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    _, item_id, _ = _seed_confirm(session_factory)
    now = datetime.now(UTC)

    with db_engine.connect() as conn_a, db_engine.connect() as conn_b:
        trans_a = conn_a.begin()
        trans_b = conn_b.begin()
        sess_a = Session(bind=conn_a, expire_on_commit=False)
        sess_b = Session(bind=conn_b, expire_on_commit=False)
        try:
            claimed_a = _claim(sess_a, item_id, now=now, worker_id="worker-a")
            claimed_b = _claim(sess_b, item_id, now=now, worker_id="worker-b")
            winner = claimed_a if claimed_a is not None else claimed_b
            loser = claimed_b if claimed_a is not None else claimed_a
            assert winner is not None
            assert loser is None
            assert winner.work_item_id == item_id
        finally:
            sess_a.close()
            sess_b.close()
            trans_a.rollback()
            trans_b.rollback()


def test_lease_loss_leaves_null_attempt_and_reclaim_shares_key(
    session_factory: sessionmaker[Session],
) -> None:
    order_id, item_id, stored_key = _seed_confirm(session_factory)
    now = datetime.now(UTC)

    with session_factory.begin() as session:
        claimed = _claim(session, item_id, now=now, worker_id="worker-a")
        assert claimed is not None
        first_attempt_id = claimed.attempt_id
        assert claimed.idempotency_key == stored_key

    later = now + timedelta(seconds=1)
    with session_factory.begin() as session:
        item = session.get(WorkItem, item_id)
        assert item is not None
        item.lease_until = now - timedelta(seconds=1)
        session.flush()
        reclaimed = _claim(session, item_id, now=later, worker_id="worker-b")
        assert reclaimed is not None
        second_attempt_id = reclaimed.attempt_id
        assert reclaimed.idempotency_key == stored_key
        assert second_attempt_id != first_attempt_id

    with session_factory() as session:
        first = session.get(Attempt, first_attempt_id)
        second = session.get(Attempt, second_attempt_id)
        assert first is not None
        assert second is not None
        assert first.outcome is None
        assert first.ended_at is None
        assert second.outcome is None
        assert first.work_item_id == item_id
        assert second.work_item_id == item_id
        events = session.scalars(select(OrderEvent).where(OrderEvent.order_id == order_id)).all()
        assert {event.cause for event in events} == {"place"}
        assert all(event.cause not in LEASE_LIFECYCLE_CAUSES for event in events)
        assert len(events) == 1


def test_stale_version_zero_rows_counts_invalid_and_appends_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    order_id, item_id, _ = _seed_confirm(session_factory)
    now = datetime.now(UTC)
    settings = _settings()
    rng = random.Random(0)
    counters = WorkerCounters()

    with session_factory.begin() as session:
        claimed = _claim(session, item_id, now=now, worker_id="worker-a")
        assert claimed is not None

    with session_factory.begin() as session:
        order = session.get(Order, order_id)
        assert order is not None
        order.version += 1
        stale_version = order.version
        stale_state = order.state

    result = HandlerResult(
        outcome="ok",
        disposition=WorkDisposition.COMPLETE,
        transition=GuardedTransition(
            expected_state="placed",
            to_state="confirmed",
            cause="confirm",
        ),
    )
    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            result,
            settings=settings,
            counters=counters,
            now=now,
            rng=rng,
        )

    assert counters.invalid_transitions == 1
    with session_factory() as session:
        order = session.get(Order, order_id)
        item = session.get(WorkItem, item_id)
        attempt = session.get(Attempt, claimed.attempt_id)
        assert order is not None
        assert item is not None
        assert attempt is not None
        assert order.state == stale_state
        assert order.version == stale_version
        assert item.status == "failed"
        assert item.lease_owner is None
        assert attempt.outcome == "ok"
        evidence = session.scalars(
            select(OrderEvent).where(OrderEvent.order_id == order_id, OrderEvent.applied.is_(False))
        ).all()
        assert len(evidence) == 1
        assert evidence[0].cause == CAUSE_INVALID
        assert evidence[0].to_state == "confirmed"


def test_cancelled_zero_rows_is_supersession_not_invalid(
    session_factory: sessionmaker[Session],
) -> None:
    order_id, item_id, _ = _seed_confirm(session_factory)
    now = datetime.now(UTC)
    settings = _settings()
    counters = WorkerCounters()

    with session_factory.begin() as session:
        claimed = _claim(session, item_id, now=now, worker_id="worker-a")
        assert claimed is not None

    with session_factory.begin() as session:
        order = session.get(Order, order_id)
        assert order is not None
        order.state = "cancelled"
        order.version += 1

    result = HandlerResult(
        outcome="ok",
        transition=GuardedTransition(
            expected_state="placed",
            to_state="confirmed",
            cause="confirm",
        ),
    )
    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            result,
            settings=settings,
            counters=counters,
            now=now,
            rng=random.Random(0),
        )

    assert counters.invalid_transitions == 0
    with session_factory() as session:
        order = session.get(Order, order_id)
        item = session.get(WorkItem, item_id)
        assert order is not None
        assert item is not None
        assert order.state == "cancelled"
        assert item.status == "cancelled"
        evidence = session.scalars(
            select(OrderEvent).where(OrderEvent.order_id == order_id, OrderEvent.applied.is_(False))
        ).all()
        assert len(evidence) == 1
        assert evidence[0].cause == CAUSE_SUPERSEDED


def test_business_4xx_fails_order_no_retry(session_factory: sessionmaker[Session]) -> None:
    order_id, item_id, stored_key = _seed_confirm(session_factory)
    now = datetime.now(UTC)
    settings = _settings()

    with session_factory.begin() as session:
        claimed = _claim(session, item_id, now=now, worker_id="worker-a")
        assert claimed is not None

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            HandlerResult(outcome=classify_status(409)),
            settings=settings,
            counters=WorkerCounters(),
            now=now,
            rng=random.Random(0),
        )

    with session_factory() as session:
        order = session.get(Order, order_id)
        item = session.get(WorkItem, item_id)
        attempt = session.get(Attempt, claimed.attempt_id)
        assert order is not None
        assert item is not None
        assert attempt is not None
        assert order.state == "failed"
        assert item.status == "failed"
        assert item.idempotency_key == stored_key
        assert item.lease_owner is None
        assert item.lease_until is None
        assert attempt.outcome == "http_4xx"
        applied = session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id, OrderEvent.cause == CAUSE_PERMANENT_4XX
            )
        ).one()
        assert applied.applied is True
        assert applied.to_state == "failed"


def test_429_retries_same_stored_key(session_factory: sessionmaker[Session]) -> None:
    order_id, item_id, stored_key = _seed_confirm(session_factory)
    now = datetime.now(UTC)
    settings = _settings()

    with session_factory.begin() as session:
        claimed = _claim(session, item_id, now=now, worker_id="worker-a")
        assert claimed is not None

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            HandlerResult(outcome=classify_status(429)),
            settings=settings,
            counters=WorkerCounters(),
            now=now,
            rng=random.Random(0),
        )

    with session_factory() as session:
        order = session.get(Order, order_id)
        item = session.get(WorkItem, item_id)
        attempt = session.get(Attempt, claimed.attempt_id)
        assert order is not None
        assert item is not None
        assert attempt is not None
        assert order.state == "placed"
        assert item.status == "pending"
        assert item.idempotency_key == stored_key
        assert item.lease_owner is None
        assert item.lease_until is None
        assert item.next_attempt_at is not None
        assert item.next_attempt_at > now
        assert item.next_attempt_at <= now + timedelta(seconds=settings.backoff_cap_s)
        assert attempt.outcome == "http_429"
        causes = {
            event.cause
            for event in session.scalars(select(OrderEvent).where(OrderEvent.order_id == order_id))
        }
        assert causes == {"place"}


def test_rsim_semaphore_respects_dep_cap() -> None:
    async def _run() -> None:
        settings = _settings(dep_cap_rsim=2)
        caps = DepCaps(settings)
        current = 0
        max_seen = 0
        lock = asyncio.Lock()

        async def one() -> None:
            nonlocal current, max_seen
            async with caps.rsim():
                async with lock:
                    current += 1
                    max_seen = max(max_seen, current)
                await asyncio.sleep(0.05)
                async with lock:
                    current -= 1

        await asyncio.gather(*[one() for _ in range(6)])
        assert max_seen == 2

        csim_current = 0
        csim_max = 0

        async def csim_one() -> None:
            nonlocal csim_current, csim_max
            async with caps.csim():
                async with lock:
                    csim_current += 1
                    csim_max = max(csim_max, csim_current)
                await asyncio.sleep(0.02)
                async with lock:
                    csim_current -= 1

        await asyncio.gather(*[csim_one() for _ in range(6)])
        assert csim_max == 6

    asyncio.run(_run())
