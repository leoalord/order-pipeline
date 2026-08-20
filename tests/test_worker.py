"""Worker chassis: SKIP LOCKED, attempt-at-claim, guards, 4xx vs 429, rsim cap."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import threading
import time
import uuid
import warnings
from datetime import UTC, datetime, timedelta
from typing import cast

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.cancel import CancelOutcome, cancel_order
from order_pipeline.intake import place_order, void_idempotency_key
from order_pipeline.lifecycle import CAUSE_INVALID
from order_pipeline.models import Attempt, Order, OrderEvent, WorkItem
from order_pipeline.worker.backoff import full_jitter_delay_s
from order_pipeline.worker.chassis import Worker
from order_pipeline.worker.claim import claim_next
from order_pipeline.worker.classify import (
    PERMANENT_OUTCOMES,
    TRANSIENT_OUTCOMES,
    classify_exception,
    classify_status,
)
from order_pipeline.worker.counters import WorkerCounters
from order_pipeline.worker.deps import DepCaps
from order_pipeline.worker.finalize import (
    CAUSE_PERMANENT_4XX,
    CAUSE_SUPERSEDED,
    PARK_NEXT_ACTION_GUARD_REJECTED,
    PARK_REASON_GUARD_REJECTED,
    finalize_claim,
)
from order_pipeline.worker.health import create_health_app
from order_pipeline.worker.http import courier_base_url
from order_pipeline.worker.plugin import (
    ClaimedWork,
    GuardedTransition,
    HandlerResult,
    WorkDisposition,
    WorkHandler,
)
from order_pipeline.worker.settings import WorkerSettings
from tests.conftest import UNCLAIMABLE_AT, hold_unclaimable

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


def _settings(
    *,
    dep_cap_rsim: int = 8,
    dep_cap_csim: int = 8,
    task_capacity: int = 24,
) -> WorkerSettings:
    return WorkerSettings(
        database_url=TEST_DATABASE_URL,
        restaurant_base_url="http://restaurant:8081",
        dep_cap_rsim=dep_cap_rsim,
        dep_cap_csim=dep_cap_csim,
        task_capacity=task_capacity,
    )


def _seed_confirm_row(
    session: Session,
    *,
    next_attempt_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    order = place_order(
        session,
        place_key=f"worker-{uuid.uuid4()}",
        items=["burrito"],
        cohort_id=None,
        ttl_hours=TTL_HOURS,
    )
    item = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
    # Default far-future so live compose workers cannot steal before the test claims.
    item.next_attempt_at = UNCLAIMABLE_AT if next_attempt_at is None else next_attempt_at
    session.flush()
    return order.id, item.id, item.idempotency_key


def _seed_confirm(
    factory: sessionmaker[Session],
    *,
    next_attempt_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    with factory.begin() as session:
        return _seed_confirm_row(session, next_attempt_at=next_attempt_at)


def _claim(
    session: Session,
    item_id: uuid.UUID,
    *,
    now: datetime,
    worker_id: str,
    lease_s: float = 15.0,
) -> ClaimedWork | None:
    item = session.get(WorkItem, item_id)
    if item is not None:
        item.next_attempt_at = now
        session.flush()
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
    # Keep the compose workers from claiming this committed fixture while the
    # test opens its two competing connections. Both test claimers use the
    # same future logical clock, so only they consider the item due.
    now = datetime.now(UTC) + timedelta(days=1)
    _, item_id, _ = _seed_confirm(session_factory, next_attempt_at=now)

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
        assert item.status == "parked"
        assert item.lease_owner is None
        assert item.park_owner == "worker-a"
        assert item.park_reason == PARK_REASON_GUARD_REJECTED
        assert item.park_next_action == PARK_NEXT_ACTION_GUARD_REJECTED
        assert attempt.outcome == "ok"
        evidence = session.scalars(
            select(OrderEvent).where(OrderEvent.order_id == order_id, OrderEvent.applied.is_(False))
        ).all()
        assert len(evidence) == 1
        assert evidence[0].cause == CAUSE_INVALID
        assert evidence[0].to_state == "confirmed"


def test_illegal_transition_is_rejected_before_guarded_update_and_parks_work(
    session_factory: sessionmaker[Session],
) -> None:
    order_id, item_id, _ = _seed_confirm(session_factory)
    now = datetime.now(UTC)
    counters = WorkerCounters()

    with session_factory.begin() as session:
        claimed = _claim(session, item_id, now=now, worker_id="worker-a")
        assert claimed is not None

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            HandlerResult(
                outcome="ok",
                transition=GuardedTransition(
                    expected_state="placed",
                    to_state="delivered",
                    cause="bad_handler_result",
                ),
            ),
            settings=_settings(),
            counters=counters,
            now=now,
            rng=random.Random(0),
        )

    assert counters.invalid_transitions == 1
    with session_factory() as session:
        order = session.get(Order, order_id)
        item = session.get(WorkItem, item_id)
        assert order is not None
        assert item is not None
        assert order.state == "placed"
        assert order.version == 1
        assert item.status == "parked"
        assert item.park_owner == "worker-a"
        assert item.park_reason == PARK_REASON_GUARD_REJECTED
        assert item.park_next_action == PARK_NEXT_ACTION_GUARD_REJECTED
        evidence = session.scalars(
            select(OrderEvent).where(OrderEvent.order_id == order_id, OrderEvent.applied.is_(False))
        ).one()
        assert evidence.from_state == "placed"
        assert evidence.to_state == "delivered"
        assert evidence.cause == CAUSE_INVALID


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
        cancelled = cancel_order(session, order_id, now=now)
        assert cancelled.outcome is CancelOutcome.APPLIED
        item = session.get(WorkItem, item_id)
        assert item is not None
        assert item.status == "cancelled"
        assert item.lease_owner == "worker-a"

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
        hold_unclaimable(session, order_id)

    assert counters.invalid_transitions == 0
    with session_factory() as session:
        order = session.get(Order, order_id)
        item = session.get(WorkItem, item_id)
        attempt = session.get(Attempt, claimed.attempt_id)
        assert order is not None
        assert item is not None
        assert attempt is not None
        assert order.state == "cancelled"
        assert item.status == "cancelled"
        assert item.lease_owner is None
        assert attempt.outcome == "ok"
        evidence = session.scalars(
            select(OrderEvent).where(OrderEvent.order_id == order_id, OrderEvent.applied.is_(False))
        ).all()
        assert len(evidence) == 1
        assert evidence[0].cause == CAUSE_SUPERSEDED
        voids = session.scalars(
            select(WorkItem).where(
                WorkItem.order_id == order_id,
                WorkItem.work_type == "void_ticket",
            )
        ).all()
        assert len(voids) == 1
        assert voids[0].idempotency_key == void_idempotency_key(order_id)


@pytest.mark.parametrize("outcome", ["http_5xx", "dropped", "timeout"])
def test_cancel_then_failed_confirm_enqueues_void_and_stays_cancelled(
    session_factory: sessionmaker[Session],
    outcome: str,
) -> None:
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        order_id, item_id, _ = _seed_confirm_row(session)
        claimed = _claim(session, item_id, now=now, worker_id="worker-a")
        assert claimed is not None

    with session_factory.begin() as session:
        cancelled = cancel_order(session, order_id, now=now)
        assert cancelled.outcome is CancelOutcome.APPLIED

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            HandlerResult(outcome=outcome),
            settings=_settings(),
            counters=WorkerCounters(),
            now=now,
            rng=random.Random(0),
        )
        hold_unclaimable(session, order_id)

    with session_factory() as session:
        order = session.get(Order, order_id)
        item = session.get(WorkItem, item_id)
        voids = session.scalars(
            select(WorkItem).where(
                WorkItem.order_id == order_id,
                WorkItem.work_type == "void_ticket",
            )
        ).all()
        assert order is not None
        assert item is not None
        assert order.state == "cancelled"
        assert item.status == "cancelled"
        assert item.lease_owner is None
        assert item.lease_until is None
        assert len(voids) == 1
        assert voids[0].status == "pending"
        assert voids[0].idempotency_key == void_idempotency_key(order_id)
        assert voids[0].payload == {"accept_key": claimed.idempotency_key}
        later = claim_next(
            session,
            now=now + timedelta(seconds=1),
            lease_s=15.0,
            worker_id="worker-b",
            work_types=("confirm",),
            work_item_id=item_id,
        )
        assert later is None


def test_cancel_then_leased_park_keeps_work_cancelled(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        order_id, item_id, _ = _seed_confirm_row(session)
        claimed = _claim(session, item_id, now=now, worker_id="worker-a")
        assert claimed is not None

    with session_factory.begin() as session:
        cancelled = cancel_order(session, order_id, now=now)
        assert cancelled.outcome is CancelOutcome.APPLIED

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            HandlerResult(
                outcome="ok",
                disposition=WorkDisposition.PARK,
                park_reason="retry_budget_exhausted",
                park_next_action="redrive",
            ),
            settings=_settings(),
            counters=WorkerCounters(),
            now=now,
            rng=random.Random(0),
        )

    with session_factory() as session:
        order = session.get(Order, order_id)
        item = session.get(WorkItem, item_id)
        assert order is not None
        assert item is not None
        assert order.state == "cancelled"
        assert item.status == "cancelled"
        assert item.lease_owner is None
        assert item.park_reason is None


def test_stale_finalize_after_reclaim_leaves_null_attempt(
    session_factory: sessionmaker[Session],
) -> None:
    order_id, item_id, stored_key = _seed_confirm(session_factory)
    now = datetime.now(UTC)
    settings = _settings()
    confirm_ok = HandlerResult(
        outcome="ok",
        transition=GuardedTransition(
            expected_state="placed",
            to_state="confirmed",
            cause="confirm",
        ),
    )

    with session_factory.begin() as session:
        claimed_a = _claim(session, item_id, now=now, worker_id="worker-a")
        assert claimed_a is not None
        first_attempt_id = claimed_a.attempt_id

    later = now + timedelta(seconds=1)
    with session_factory.begin() as session:
        item = session.get(WorkItem, item_id)
        assert item is not None
        item.lease_until = now - timedelta(seconds=1)
        claimed_b = _claim(session, item_id, now=later, worker_id="worker-b")
        assert claimed_b is not None
        second_attempt_id = claimed_b.attempt_id
        assert claimed_b.idempotency_key == stored_key
        assert second_attempt_id != first_attempt_id

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed_a,
            confirm_ok,
            settings=settings,
            counters=WorkerCounters(),
            now=later,
            rng=random.Random(0),
        )

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed_b,
            confirm_ok,
            settings=settings,
            counters=WorkerCounters(),
            now=later,
            rng=random.Random(1),
        )

    with session_factory() as session:
        first = session.get(Attempt, first_attempt_id)
        second = session.get(Attempt, second_attempt_id)
        order = session.get(Order, order_id)
        assert first is not None
        assert second is not None
        assert order is not None
        assert first.outcome is None
        assert first.ended_at is None
        assert second.outcome == "ok"
        assert order.state == "confirmed"
        confirmed = session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id,
                OrderEvent.to_state == "confirmed",
                OrderEvent.applied.is_(True),
            )
        ).all()
        assert len(confirmed) == 1


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
    """Busy 429 stays transient: retry, same stored key, order not failed."""
    assert classify_status(429) == "http_429"
    assert "http_429" in TRANSIENT_OUTCOMES
    assert "http_429" not in PERMANENT_OUTCOMES
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


def test_blackout_timeout_is_unknown_and_retries_same_stored_key_with_full_jitter(
    session_factory: sessionmaker[Session],
) -> None:
    """Scenario 2 retry: unknown timeout, unleased bounded jitter, same DB key."""
    # Keep live compose workers away from the fixture until this test's logical
    # claimer has finished, without moving the confirm beyond its 120s clock.
    logical_now = datetime.now(UTC) + timedelta(seconds=5)
    order_id, item_id, stored_key = _seed_confirm(
        session_factory,
        next_attempt_at=logical_now,
    )
    settings = _settings()
    assert classify_exception(httpx.ReadTimeout("restaurant blackout")) == "timeout"

    with session_factory.begin() as session:
        claimed = _claim(
            session,
            item_id,
            now=logical_now,
            worker_id="outage-timeout",
        )
        assert claimed is not None
        assert claimed.idempotency_key == stored_key

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            HandlerResult(outcome="timeout"),
            settings=settings,
            counters=WorkerCounters(),
            now=logical_now,
            rng=random.Random(7),
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
        assert (
            logical_now
            < item.next_attempt_at
            <= (logical_now + timedelta(seconds=settings.backoff_cap_s))
        )
        assert attempt.outcome == "timeout"

    rng = random.Random(19)
    samples = [full_jitter_delay_s(attempt, settings, rng) for attempt in range(1, 13)]
    ceilings = [
        min(settings.backoff_cap_s, settings.backoff_base_s * (2 ** (attempt - 1)))
        for attempt in range(1, 13)
    ]
    assert all(0 <= delay <= ceiling for delay, ceiling in zip(samples, ceilings, strict=True))
    assert max(samples) <= settings.backoff_cap_s
    assert len({round(delay, 4) for delay in samples}) > 1


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

    asyncio.run(_run())


def test_csim_semaphore_respects_dep_cap_only() -> None:
    async def _run() -> None:
        settings = _settings(dep_cap_rsim=8, dep_cap_csim=2)
        caps = DepCaps(settings)
        lock = asyncio.Lock()

        csim_current = 0
        csim_max = 0

        async def csim_one() -> None:
            nonlocal csim_current, csim_max
            async with caps.csim():
                async with lock:
                    csim_current += 1
                    csim_max = max(csim_max, csim_current)
                await asyncio.sleep(0.05)
                async with lock:
                    csim_current -= 1

        rsim_current = 0
        rsim_max = 0

        async def rsim_one() -> None:
            nonlocal rsim_current, rsim_max
            async with caps.rsim():
                async with lock:
                    rsim_current += 1
                    rsim_max = max(rsim_max, rsim_current)
                await asyncio.sleep(0.05)
                async with lock:
                    rsim_current -= 1

        await asyncio.gather(
            *[csim_one() for _ in range(6)],
            *[rsim_one() for _ in range(6)],
        )
        assert csim_max == 2
        assert rsim_max == 6

    asyncio.run(_run())


def test_eligible_types_excludes_full_dependency() -> None:
    caps = DepCaps(_settings(dep_cap_rsim=1, dep_cap_csim=1))
    registered = ("confirm", "poll_cook", "void_ticket", "dispatch", "poll_ride")
    assert set(caps.eligible_types(registered)) == set(registered)
    caps.admit("confirm")
    eligible = caps.eligible_types(registered)
    assert "confirm" not in eligible
    assert "poll_cook" not in eligible
    assert "void_ticket" not in eligible
    assert "dispatch" in eligible
    assert "poll_ride" in eligible
    caps.admit("dispatch")
    assert caps.eligible_types(registered) == ()
    caps.release_admit("confirm")
    assert "confirm" in caps.eligible_types(registered)
    assert "dispatch" not in caps.eligible_types(registered)


def test_run_does_not_claim_kitchen_when_rsim_is_full(db_engine: Engine) -> None:
    settings = _settings(dep_cap_rsim=1, dep_cap_csim=1, task_capacity=4)
    caps = DepCaps(settings)
    caps.admit("confirm")
    seen: list[tuple[str, ...]] = []

    async def unused(_claimed: ClaimedWork) -> HandlerResult:
        raise AssertionError("handler must not run")

    worker = Worker(
        settings,
        db_engine,
        handlers={
            "confirm": cast(WorkHandler, unused),
            "poll_cook": cast(WorkHandler, unused),
            "dispatch": cast(WorkHandler, unused),
            "poll_ride": cast(WorkHandler, unused),
        },
        caps=caps,
        worker_id="admit-filter",
        idle_s=0.01,
    )

    def spy_claim(
        *,
        work_item_id: uuid.UUID | None = None,
        work_types: tuple[str, ...] | None = None,
    ) -> ClaimedWork | None:
        assert work_item_id is None
        assert work_types is not None
        seen.append(work_types)
        return None

    worker.claim = spy_claim  # type: ignore[method-assign]

    async def _run() -> None:
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())
    assert seen
    for types in seen:
        assert "confirm" not in types
        assert "poll_cook" not in types
        assert "dispatch" in types
        assert "poll_ride" in types


def test_courier_base_url_is_compose_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_COURIER_BASE_URL", raising=False)
    assert courier_base_url() == "http://courier:8082"
    monkeypatch.setenv("WORKER_COURIER_BASE_URL", "http://localhost:8082")
    assert courier_base_url() == "http://localhost:8082"
    assert courier_base_url(override="http://csim:8082") == "http://csim:8082"


def _operational_error(message: str) -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception(message))


def test_worker_engine_validates_stale_pooled_connections() -> None:
    """Pre-ping, and a pool big enough for every thread that can hold a connection."""
    from order_pipeline.worker.__main__ import WORKER_DB_POOL_HEADROOM, create_worker_engine

    settings = _settings()
    engine = create_worker_engine(settings)
    try:
        assert getattr(engine.pool, "_pre_ping", False) is True
        # Off-loop finalize made concurrent checkouts possible; the default
        # 5 + 10 pool is smaller than the threads that can demand one.
        assert engine.pool.size() == settings.finalize_workers + WORKER_DB_POOL_HEADROOM
        assert getattr(engine.pool, "_max_overflow", None) == 0
    finally:
        engine.dispose()


def test_run_survives_pool_exhaustion(caplog: pytest.LogCaptureFixture) -> None:
    """A saturated pool raises sqlalchemy.exc.TimeoutError from claim, not OperationalError.

    Before the transient tuple covered it, this exited run() -- and under
    restart: "no" that is the replica gone for the rest of the demo.
    """
    engine = create_engine(
        TEST_DATABASE_URL,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.25,
    )
    worker = Worker(
        _settings(),
        engine,
        # A synthetic type so this never competes for real rows with a running stack:
        # claim still has to reach the pool, and finds nothing once it gets there.
        handlers={"__never_claimed__": cast(WorkHandler, _never_called)},
        worker_id="pool-exhausted",
        idle_s=0.01,
        rng=random.Random(0),
    )
    caplog.set_level(logging.WARNING, logger="order_pipeline.worker")

    def _retries() -> int:
        return sum(record.message.startswith("claim_retry") for record in caplog.records)

    async def _run() -> None:
        hold = engine.connect()  # occupy the only slot, like a stuck finalize thread
        task = asyncio.create_task(worker.run())
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and _retries() == 0:
                await asyncio.sleep(0.02)
            assert _retries() > 0, f"pool exhaustion never backed off: {caplog.text}"
            assert not task.done(), "run() exited on a pool checkout timeout"
            hold.close()
            await asyncio.sleep(0.5)
            settled = _retries()
            await asyncio.sleep(0.5)
            assert _retries() == settled, "claims still failing after the pool recovered"
            assert not task.done()
        finally:
            hold.close()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    try:
        asyncio.run(_run())
    finally:
        engine.dispose()


async def _never_called(_item: ClaimedWork) -> HandlerResult:
    raise AssertionError("no work should be claimed in this test")


def _stub_claim(work_type: str = "dispatch") -> ClaimedWork:
    return ClaimedWork(
        work_item_id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        work_type=work_type,
        idempotency_key="stub",
        attempt_id=uuid.uuid4(),
        lease_owner="starvation",
        attempt_count=1,
        payload=None,
        order_state="ready",
        order_version=1,
        accepted_at=datetime.now(UTC),
    )


def test_stuck_finalizes_do_not_starve_the_claim_loop(db_engine: Engine) -> None:
    """Finalize and claim must not share one thread pool.

    On the shared default executor, enough stuck commits fill every thread and
    run() blocks at its own `await` -- the replica is alive, claims nothing, and
    the claim-retry backoff is unreachable because control never returns to it.
    """
    settings = _settings()
    release = threading.Event()
    claims = {"n": 0}
    # More stuck finalizes than the *default* executor has threads, which is what
    # sharing it with claim used to mean.
    saturating = min(32, (os.cpu_count() or 1) + 4) + 4

    async def handler(_item: ClaimedWork) -> HandlerResult:
        return HandlerResult(outcome="ok")

    worker = Worker(
        settings,
        db_engine,
        handlers={"dispatch": cast(WorkHandler, handler)},
        worker_id="starvation",
        idle_s=0.01,
    )

    def blocked_finalize(_claimed: ClaimedWork, _result: HandlerResult) -> None:
        release.wait(timeout=10.0)

    def counting_claim(
        *,
        work_item_id: uuid.UUID | None = None,
        work_types: tuple[str, ...] | None = None,
    ) -> ClaimedWork | None:
        claims["n"] += 1
        return None  # nothing to claim; the point is that the loop keeps turning

    worker.finalize = blocked_finalize  # type: ignore[assignment]
    worker.claim = counting_claim  # type: ignore[method-assign]

    async def _run() -> None:
        stuck = [asyncio.create_task(worker.process(_stub_claim())) for _ in range(saturating)]
        await asyncio.sleep(0.2)
        assert not any(task.done() for task in stuck), "finalize did not actually block"
        before = claims["n"]
        loop_task = asyncio.create_task(worker.run())
        try:
            await asyncio.sleep(0.5)
            assert claims["n"] > before, (
                f"claim loop starved by {saturating} stuck finalizes (claims stuck at {before})"
            )
        finally:
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await loop_task
            release.set()
            await asyncio.gather(*stuck)

    try:
        asyncio.run(_run())
    finally:
        release.set()


def test_run_survives_transient_claim_error_then_processes(
    db_engine: Engine,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    order_id, item_id, _ = _seed_confirm(session_factory)
    now = datetime.now(UTC)
    worker_id = "survive-claim"
    with session_factory.begin() as session:
        claimed = _claim(session, item_id, now=now, worker_id=worker_id)
        assert claimed is not None

    processed = asyncio.Event()
    seen: list[uuid.UUID] = []
    claims = {"n": 0}

    async def handler(item: ClaimedWork) -> HandlerResult:
        seen.append(item.work_item_id)
        processed.set()
        return HandlerResult(outcome="ok")

    settings = _settings(dep_cap_rsim=1, dep_cap_csim=1, task_capacity=4)
    worker = Worker(
        settings,
        db_engine,
        handlers={"confirm": cast(WorkHandler, handler)},
        worker_id=worker_id,
        idle_s=0.01,
        rng=random.Random(0),
    )

    def flaky_claim(
        *,
        work_item_id: uuid.UUID | None = None,
        work_types: tuple[str, ...] | None = None,
    ) -> ClaimedWork | None:
        assert work_item_id is None
        assert work_types is not None
        claims["n"] += 1
        if claims["n"] == 1:
            raise _operational_error("server closed the connection")
        if claims["n"] == 2:
            return claimed
        return None

    worker.claim = flaky_claim  # type: ignore[method-assign]
    caplog.set_level(logging.WARNING, logger="order_pipeline.worker")

    async def _run() -> None:
        task = asyncio.create_task(worker.run())
        try:
            await asyncio.wait_for(processed.wait(), timeout=3.0)
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    asyncio.run(_run())
    assert seen == [item_id]
    assert any(record.message.startswith("claim_retry") for record in caplog.records)
    with session_factory() as session:
        item = session.get(WorkItem, item_id)
        assert item is not None
        assert item.status == "completed"
        hold_unclaimable(session, order_id)
        session.commit()


def test_finalize_failure_releases_capacity_is_observed_and_reclaimable(
    db_engine: Engine,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    order_id, item_id, stored_key = _seed_confirm(session_factory)
    now = datetime.now(UTC)
    worker_id = "finalize-boom"
    with session_factory.begin() as session:
        claimed = _claim(session, item_id, now=now, worker_id=worker_id)
        assert claimed is not None
        first_attempt_id = claimed.attempt_id

    settings = _settings(dep_cap_rsim=1, dep_cap_csim=1, task_capacity=4)
    caps = DepCaps(settings)
    registered = ("confirm", "dispatch")
    idle_claims = {"n": 0}

    async def handler(_item: ClaimedWork) -> HandlerResult:
        return HandlerResult(outcome="ok")

    worker = Worker(
        settings,
        db_engine,
        handlers={"confirm": cast(WorkHandler, handler)},
        caps=caps,
        worker_id=worker_id,
        idle_s=0.01,
        rng=random.Random(0),
    )

    def boom_finalize(_claimed: ClaimedWork, _result: HandlerResult) -> None:
        raise _operational_error("could not commit")

    def claim_once(
        *,
        work_item_id: uuid.UUID | None = None,
        work_types: tuple[str, ...] | None = None,
    ) -> ClaimedWork | None:
        assert work_item_id is None
        assert work_types is not None
        if idle_claims["n"] == 0:
            idle_claims["n"] = 1
            return claimed
        idle_claims["n"] += 1
        return None

    worker.finalize = boom_finalize  # type: ignore[assignment]
    worker.claim = claim_once  # type: ignore[method-assign]
    caplog.set_level(logging.ERROR, logger="order_pipeline.worker")

    async def _run() -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            task = asyncio.create_task(worker.run())
            try:
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    observed = any(
                        record.message.startswith("process_failed")
                        and str(item_id) in record.message
                        and str(order_id) in record.message
                        and worker_id in record.message
                        for record in caplog.records
                    )
                    if observed and idle_claims["n"] > 1:
                        break
                    await asyncio.sleep(0.02)
                else:
                    pytest.fail(f"finalize failure was not observed: {caplog.text}")
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            leaked = [
                warning for warning in caught if "never retrieved" in str(warning.message).lower()
            ]
            assert leaked == []

    asyncio.run(_run())
    assert "confirm" in caps.eligible_types(registered)
    assert idle_claims["n"] > 1

    later = now + timedelta(seconds=20)
    with session_factory.begin() as session:
        item = session.get(WorkItem, item_id)
        assert item is not None
        assert item.status == "leased"
        item.lease_until = now - timedelta(seconds=1)
        session.flush()
        reclaimed = _claim(session, item_id, now=later, worker_id="finalize-survivor")
        assert reclaimed is not None
        assert reclaimed.idempotency_key == stored_key
        assert reclaimed.attempt_id != first_attempt_id
        hold_unclaimable(session, order_id)

    with session_factory() as session:
        first = session.get(Attempt, first_attempt_id)
        assert first is not None
        assert first.outcome is None


def test_finalize_db_io_does_not_stall_event_loop(
    db_engine: Engine,
    session_factory: sessionmaker[Session],
) -> None:
    order_id, item_id, _ = _seed_confirm(session_factory)
    now = datetime.now(UTC)
    worker_id = "finalize-off-loop"
    with session_factory.begin() as session:
        claimed = _claim(session, item_id, now=now, worker_id=worker_id)
        assert claimed is not None

    started = threading.Event()
    release = threading.Event()
    progressed = threading.Event()

    async def handler(_item: ClaimedWork) -> HandlerResult:
        return HandlerResult(outcome="ok")

    worker = Worker(
        _settings(),
        db_engine,
        handlers={"confirm": cast(WorkHandler, handler)},
        worker_id=worker_id,
        idle_s=0.01,
    )

    def blocked_finalize(_claimed: ClaimedWork, _result: HandlerResult) -> None:
        started.set()
        release.wait(timeout=5.0)

    worker.finalize = blocked_finalize  # type: ignore[assignment]
    health_app = create_health_app(db_engine)

    def _unstick() -> None:
        if not progressed.wait(timeout=2.0):
            release.set()

    watchdog = threading.Thread(target=_unstick, daemon=True)
    watchdog.start()

    async def _run() -> None:
        process_task = asyncio.create_task(worker.process(claimed))
        t0 = time.monotonic()
        await asyncio.to_thread(started.wait, 3.0)
        assert started.is_set()
        assert time.monotonic() - t0 < 0.5
        await asyncio.sleep(0.05)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=0.05)
        transport = httpx.ASGITransport(app=health_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://worker") as client:
            response = await asyncio.wait_for(client.get("/health"), timeout=0.5)
        assert response.status_code == 200
        progressed.set()
        release.set()
        await asyncio.wait_for(process_task, timeout=2.0)

    try:
        asyncio.run(_run())
    finally:
        release.set()
        with session_factory.begin() as session:
            hold_unclaimable(session, order_id)
    assert progressed.is_set()
