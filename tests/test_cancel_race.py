"""Bonus A: cancel wins vs in-flight confirm → void, or orphan when fail_void exhausts."""

from __future__ import annotations

import random
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.cancel import CancelOutcome, cancel_order
from order_pipeline.intake import confirm_idempotency_key, place_order, void_idempotency_key
from order_pipeline.lifecycle import CAUSE_INVALID, CAUSE_ORPHANED
from order_pipeline.models import Order, OrderEvent, WorkItem
from order_pipeline.worker.claim import claim_next
from order_pipeline.worker.counters import WorkerCounters
from order_pipeline.worker.finalize import CAUSE_SUPERSEDED, finalize_claim
from order_pipeline.worker.plugin import ClaimedWork, GuardedTransition, HandlerResult
from order_pipeline.worker.settings import WorkerSettings
from tests.conftest import UNCLAIMABLE_AT, hold_unclaimable
from tests.sim_admin import RSIM_URL, mix_off, post_sim_faults

TTL_HOURS = 48
API_URL = "http://localhost:8000"
TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/order_pipeline"
ORPHAN_TIMEOUT_S = 40.0
POLL_EVERY_S = 0.2


def _settings() -> WorkerSettings:
    return WorkerSettings(
        database_url=TEST_DATABASE_URL,
        restaurant_base_url="http://localhost:8081",
    )


def _claim(session: Session, item_id: uuid.UUID, *, now: datetime, worker_id: str) -> ClaimedWork:
    claimed = claim_next(
        session,
        now=now,
        lease_s=15.0,
        worker_id=worker_id,
        work_types=("confirm", "submit", "poll_cook", "void_ticket"),
        work_item_id=item_id,
    )
    assert claimed is not None
    return claimed


def _place_claimed(
    session: Session,
    *,
    now: datetime,
    cohort_id: uuid.UUID,
    items: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, ClaimedWork]:
    order = place_order(
        session,
        place_key=f"cancel-race-{uuid.uuid4()}",
        items=items or ["chips"],
        cohort_id=cohort_id,
        ttl_hours=TTL_HOURS,
        now=now,
    )
    item = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
    claimed = _claim(session, item.id, now=now, worker_id="worker-race")
    return order.id, item.id, claimed


def _confirm_result(ticket: dict[str, Any]) -> HandlerResult:
    return HandlerResult(
        outcome="ok",
        transition=GuardedTransition(
            expected_state="placed",
            to_state="confirmed",
            cause="confirm",
        ),
        result_payload=ticket,
    )


def _accept_ticket(confirm_key: str, items: list[str]) -> dict[str, Any]:
    try:
        response = httpx.post(
            f"{RSIM_URL}/accept",
            json={"items": items},
            headers={"Idempotency-Key": confirm_key},
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"restaurant accept failed: {exc}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return {
        "ticket_id": body["ticket_id"],
        "estimated_ready_at": body["estimated_ready_at"],
        "accept_key": confirm_key,
    }


def _void_work(session: Session, order_id: uuid.UUID) -> WorkItem | None:
    return session.scalars(
        select(WorkItem).where(
            WorkItem.order_id == order_id,
            WorkItem.work_type == "void_ticket",
        )
    ).one_or_none()


def _invalid_count(session: Session, order_id: uuid.UUID) -> int:
    return len(
        session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id,
                OrderEvent.cause == CAUSE_INVALID,
            )
        ).all()
    )


def test_cancel_during_in_flight_confirm_enqueues_void_and_voids_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    """Hold confirm after the kitchen write, then cancel — not a live wall-clock race."""
    mix_off()
    cohort = uuid.uuid4()
    now = datetime.now(UTC)
    items = ["chips"]
    with session_factory.begin() as session:
        order_id, item_id, claimed = _place_claimed(session, now=now, cohort_id=cohort, items=items)
        confirm_key = confirm_idempotency_key(order_id)

    ticket = _accept_ticket(confirm_key, items)

    with session_factory.begin() as session:
        cancelled = cancel_order(session, order_id, now=now)
        assert cancelled.outcome is CancelOutcome.APPLIED
        item = session.get(WorkItem, item_id)
        assert item is not None
        assert item.status == "cancelled"
        assert item.lease_owner == "worker-race"

    counters = WorkerCounters()
    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            _confirm_result(ticket),
            settings=_settings(),
            counters=counters,
            now=now,
            rng=random.Random(0),
        )
        void_item = _void_work(session, order_id)
        assert void_item is not None
        void_item.next_attempt_at = UNCLAIMABLE_AT

    assert counters.invalid_transitions == 0
    with session_factory.begin() as session:
        order = session.get(Order, order_id)
        confirm_item = session.get(WorkItem, item_id)
        void_item = _void_work(session, order_id)
        assert order is not None
        assert confirm_item is not None
        assert void_item is not None
        assert order.state == "cancelled"
        assert confirm_item.status == "cancelled"
        assert void_item.idempotency_key == void_idempotency_key(order_id)
        assert void_item.status == "pending"
        assert _invalid_count(session, order_id) == 0
        evidence = session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id,
                OrderEvent.cause == CAUSE_SUPERSEDED,
            )
        ).all()
        assert len(evidence) == 1
        claimed_void = _claim(session, void_item.id, now=UNCLAIMABLE_AT, worker_id="worker-void")

    try:
        response = httpx.post(
            f"{RSIM_URL}/void",
            json={"accept_key": confirm_key, "ticket_id": ticket["ticket_id"]},
            headers={"Idempotency-Key": void_idempotency_key(order_id)},
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"restaurant void failed: {exc}")
    assert response.status_code == 200, response.text
    assert response.json()["voided"] is True

    replay = httpx.post(
        f"{RSIM_URL}/void",
        json={"accept_key": confirm_key, "ticket_id": ticket["ticket_id"]},
        headers={"Idempotency-Key": void_idempotency_key(order_id)},
        timeout=5.0,
    )
    assert replay.status_code == 200, replay.text

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed_void,
            HandlerResult(outcome="ok", result_payload=response.json()),
            settings=_settings(),
            counters=counters,
            now=now,
            rng=random.Random(0),
        )
        hold_unclaimable(session, order_id)

    assert counters.invalid_transitions == 0
    with session_factory() as session:
        order = session.get(Order, order_id)
        void_item = _void_work(session, order_id)
        assert order is not None
        assert void_item is not None
        assert order.state == "cancelled"
        assert void_item.status == "completed"
        assert _invalid_count(session, order_id) == 0
        assert (
            session.scalars(
                select(OrderEvent).where(
                    OrderEvent.order_id == order_id,
                    OrderEvent.cause == CAUSE_ORPHANED,
                )
            ).all()
            == []
        )

    ledger = httpx.get(f"{RSIM_URL}/admin/ledger", timeout=5.0)
    assert ledger.status_code == 200, ledger.text
    counts = ledger.json()["counts"]
    assert counts.get(confirm_key) == 1
    assert counts.get(void_idempotency_key(order_id)) == 1

    snap = httpx.get(f"{API_URL}/snapshot", params={"cohort_id": str(cohort)}, timeout=5.0)
    assert snap.status_code == 200, snap.text
    body = snap.json()
    assert body["invalid_transitions"] == 0
    assert body["orphaned_tickets"] == 0


def test_fail_void_exhausts_to_orphan_then_clear(
    session_factory: sessionmaker[Session],
) -> None:
    """fail_void sticks; the snapshot `/` polls must show the orphan; always clear."""
    mix_off()
    post_sim_faults(RSIM_URL, {"mode": "fail_void", "mix": "off"})
    cohort = uuid.uuid4()
    now = datetime.now(UTC)
    items = ["chips"]
    try:
        with session_factory.begin() as session:
            order_id, item_id, claimed = _place_claimed(
                session, now=now, cohort_id=cohort, items=items
            )
            confirm_key = confirm_idempotency_key(order_id)

        ticket = _accept_ticket(confirm_key, items)
        with session_factory.begin() as session:
            cancelled = cancel_order(session, order_id, now=now)
            assert cancelled.outcome is CancelOutcome.APPLIED

        counters = WorkerCounters()
        with session_factory.begin() as session:
            finalize_claim(
                session,
                claimed,
                _confirm_result(ticket),
                settings=_settings(),
                counters=counters,
                now=now,
                rng=random.Random(0),
            )
        assert counters.invalid_transitions == 0

        deadline = time.monotonic() + ORPHAN_TIMEOUT_S
        last = 0
        while time.monotonic() < deadline:
            snap = httpx.get(
                f"{API_URL}/snapshot",
                params={"cohort_id": str(cohort)},
                timeout=5.0,
            )
            assert snap.status_code == 200, snap.text
            last = snap.json()["orphaned_tickets"]
            if last >= 1:
                assert snap.json()["invalid_transitions"] == 0
                break
            time.sleep(POLL_EVERY_S)
        else:
            pytest.fail(f"orphaned_tickets stayed {last} within {ORPHAN_TIMEOUT_S}s")

        order = httpx.get(f"{API_URL}/orders/{order_id}", timeout=5.0)
        assert order.status_code == 200, order.text
        assert order.json()["state"] == "cancelled"
        with session_factory() as session:
            assert _invalid_count(session, order_id) == 0
            void_item = _void_work(session, order_id)
            assert void_item is not None
            assert void_item.status == "completed"
    finally:
        cleared = post_sim_faults(RSIM_URL, {"mode": "clear", "mix": "off"})
        assert cleared["mode"] == "off", cleared


def test_poll_cook_supersession_does_not_enqueue_void(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        order = place_order(
            session,
            place_key=f"no-void-{uuid.uuid4()}",
            items=["chips"],
            cohort_id=uuid.uuid4(),
            ttl_hours=TTL_HOURS,
        )
        confirm = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
        confirm.status = "completed"
        order.state = "confirmed"
        order.version += 1
        poll = WorkItem(
            order_id=order.id,
            work_type="poll_cook",
            status="pending",
            idempotency_key=f"({order.id}, poll_cook)",
            attempt_count=0,
            next_attempt_at=now,
        )
        session.add(poll)
        session.flush()
        claimed = _claim(session, poll.id, now=now, worker_id="worker-poll")
        order_id = order.id

    with session_factory.begin() as session:
        cancelled = cancel_order(session, order_id, now=now)
        assert cancelled.outcome is CancelOutcome.APPLIED

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            HandlerResult(
                outcome="ok",
                transition=GuardedTransition(
                    expected_state="confirmed",
                    to_state="being_prepared",
                    cause="cooking_started",
                ),
            ),
            settings=_settings(),
            counters=WorkerCounters(),
            now=now,
            rng=random.Random(0),
        )
        hold_unclaimable(session, order_id)

    with session_factory() as session:
        stored = session.get(Order, order_id)
        assert stored is not None
        assert stored.state == "cancelled"
        assert _void_work(session, order_id) is None
        assert _invalid_count(session, order_id) == 0


def test_void_budget_uses_settings_not_literal(session_factory: sessionmaker[Session]) -> None:
    now = datetime.now(UTC)
    settings = WorkerSettings(
        database_url=TEST_DATABASE_URL,
        restaurant_base_url="http://localhost:8081",
        void_retries=2,
    )
    with session_factory.begin() as session:
        order = place_order(
            session,
            place_key=f"void-budget-{uuid.uuid4()}",
            items=["chips"],
            cohort_id=uuid.uuid4(),
            ttl_hours=TTL_HOURS,
        )
        cancel_order(session, order.id, now=now)
        void_item = WorkItem(
            order_id=order.id,
            work_type="void_ticket",
            status="pending",
            idempotency_key=void_idempotency_key(order.id),
            attempt_count=1,
            next_attempt_at=now,
        )
        session.add(void_item)
        session.flush()
        claimed = _claim(session, void_item.id, now=now, worker_id="worker-void")
        assert claimed.attempt_count == 2
        order_id = order.id

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            HandlerResult(outcome="http_5xx"),
            settings=settings,
            counters=WorkerCounters(),
            now=now,
            rng=random.Random(0),
        )
        hold_unclaimable(session, order_id)

    with session_factory() as session:
        stored = session.get(Order, order_id)
        voided = _void_work(session, order_id)
        orphans = session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id,
                OrderEvent.cause == CAUSE_ORPHANED,
            )
        ).all()
        assert stored is not None
        assert voided is not None
        assert stored.state == "cancelled"
        assert voided.status == "completed"
        assert len(orphans) == 1
        assert _invalid_count(session, order_id) == 0
