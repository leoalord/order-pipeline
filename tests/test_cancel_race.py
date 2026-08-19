"""Bonus A: cancel wins vs in-flight confirm → void, or orphan when fail_void exhausts."""

from __future__ import annotations

import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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
WAIT_TIMEOUT_S = 120.0
POLL_EVERY_S = 0.2
BARRIER_TIMEOUT_S = 20.0
VOID_TIMEOUT_S = 30.0
CONFIRMED_WINDOW_ATTEMPTS = 5


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


def _orphan_count(session: Session, order_id: uuid.UUID) -> int:
    return len(
        session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id,
                OrderEvent.cause == CAUSE_ORPHANED,
            )
        ).all()
    )


def _post_void(order_id: uuid.UUID, accept_key: str, ticket_id: str) -> dict[str, Any]:
    try:
        response = httpx.post(
            f"{RSIM_URL}/void",
            json={"accept_key": accept_key, "ticket_id": ticket_id},
            headers={"Idempotency-Key": void_idempotency_key(order_id)},
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"restaurant void failed: {exc}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _ledger_counts() -> dict[str, int]:
    ledger = httpx.get(f"{RSIM_URL}/admin/ledger", timeout=5.0)
    assert ledger.status_code == 200, ledger.text
    counts = ledger.json()["counts"]
    assert isinstance(counts, dict)
    return {str(key): int(value) for key, value in counts.items()}


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


def _wait_for_state(order_id: str, state: str, *, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        response = httpx.get(f"{API_URL}/orders/{order_id}", timeout=5.0)
        assert response.status_code == 200, response.text
        last = str(response.json()["state"])
        if last == state or last in {"cancelled", "failed", "delivered"}:
            return last
        time.sleep(POLL_EVERY_S)
    return last


def _cancel_while_confirmed() -> tuple[str, uuid.UUID]:
    """Place and cancel through the API until one cancel lands while confirmed.

    Confirm completes in milliseconds and `poll_cook` pivots the order shortly
    after, so a live cancel can legitimately arrive too late and 409. Each
    attempt gets its own cohort so a late one cannot pollute the assertions.
    """
    for _ in range(CONFIRMED_WINDOW_ATTEMPTS):
        cohort = uuid.uuid4()
        placed = httpx.post(
            f"{API_URL}/orders",
            json={"items": ["burrito"], "cohort_id": str(cohort)},
            headers={"Idempotency-Key": f"confirmed-cancel-{uuid.uuid4()}"},
            timeout=5.0,
        )
        assert placed.status_code == 201, placed.text
        order_id = placed.json()["id"]
        if _wait_for_state(order_id, "confirmed", timeout_s=WAIT_TIMEOUT_S) != "confirmed":
            continue
        cancelled = httpx.post(f"{API_URL}/orders/{order_id}/cancel", timeout=5.0)
        if cancelled.status_code == 409:
            continue
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["state"] == "cancelled"
        return order_id, cohort
    pytest.fail(f"no cancel landed while confirmed in {CONFIRMED_WINDOW_ATTEMPTS} attempts")


def test_http_cancel_of_a_confirmed_order_voids_the_live_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    """End to end on the running stack: cancelling a confirmed order voids its ticket.

    This is the regression guard for the reproduction — before compensation
    covered this path the order cancelled cleanly, the correctness pane stayed
    green, and the food cooked anyway.
    """
    mix_off()
    order_id, cohort = _cancel_while_confirmed()
    order_uuid = uuid.UUID(order_id)
    accept_key = confirm_idempotency_key(order_uuid)
    void_key = void_idempotency_key(order_uuid)

    counts: dict[str, int] = {}
    deadline = time.monotonic() + VOID_TIMEOUT_S
    while time.monotonic() < deadline:
        counts = _ledger_counts()
        if counts.get(void_key, 0) >= 1:
            break
        time.sleep(POLL_EVERY_S)
    else:
        pytest.fail(f"no void effect for {order_id} within {VOID_TIMEOUT_S}s")

    assert counts[accept_key] == 1
    assert counts[void_key] == 1

    with session_factory() as session:
        void_item = _void_work(session, order_uuid)
        assert void_item is not None
        assert void_item.status == "completed"
        # voided=true is the flag the kitchen rail reads when it skips this
        # accept key, so the pan is released instead of cooked out.
        assert isinstance(void_item.result, dict)
        assert void_item.result["voided"] is True
        assert _invalid_count(session, order_uuid) == 0
        assert _orphan_count(session, order_uuid) == 0

    # Void is a compensation record and a rail release, not an oven switch.
    walked = httpx.get(f"{RSIM_URL}/keys/{accept_key}", timeout=5.0)
    assert walked.status_code == 200, walked.text
    assert walked.json()["ticket_id"]

    fetched = httpx.get(f"{API_URL}/orders/{order_id}", timeout=5.0)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["state"] == "cancelled"

    snap = httpx.get(f"{API_URL}/snapshot", params={"cohort_id": str(cohort)}, timeout=5.0)
    assert snap.status_code == 200, snap.text
    body = snap.json()
    assert body["invalid_transitions"] == 0
    assert body["orphaned_tickets"] == 0
    assert body["conservation"]["residual"] == 0


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


def test_poll_cook_supersession_does_not_enqueue_a_second_void(
    session_factory: sessionmaker[Session],
) -> None:
    """The confirm-side void already covers this order; the poll loser adds nothing.

    Cancelling a confirmed order compensates its ticket up front, so a
    superseded `poll_cook` must settle quietly rather than queue a duplicate
    under the same `(order_id, void)` key.
    """
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        order = place_order(
            session,
            place_key=f"no-second-void-{uuid.uuid4()}",
            items=["chips"],
            cohort_id=uuid.uuid4(),
            ttl_hours=TTL_HOURS,
        )
        confirm = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
        # A confirmed order always has a confirm that was claimed and completed.
        confirm.status = "completed"
        confirm.attempt_count = 1
        confirm.result = {"ticket_id": "ticket-poll-loser"}
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
        confirm_key = confirm.idempotency_key

    with session_factory.begin() as session:
        cancelled = cancel_order(session, order_id, now=now)
        assert cancelled.outcome is CancelOutcome.APPLIED
        void_item = _void_work(session, order_id)
        assert void_item is not None
        assert void_item.idempotency_key == void_idempotency_key(order_id)
        assert isinstance(void_item.payload, dict)
        assert void_item.payload["accept_key"] == confirm_key
        assert void_item.payload["ticket_id"] == "ticket-poll-loser"
        hold_unclaimable(session, order_id)

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
        # one_or_none() is the duplicate check: a second void would raise here.
        assert _void_work(session, order_id) is not None
        assert _invalid_count(session, order_id) == 0


def test_cancel_from_confirmed_compensates_the_live_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    """The reproduction: a completed confirm leaves a ticket that cancel must void."""
    mix_off()
    now = datetime.now(UTC)
    items = ["chips"]
    with session_factory.begin() as session:
        order_id, item_id, claimed = _place_claimed(session, now=now, cohort_id=uuid.uuid4())
        confirm_key = confirm_idempotency_key(order_id)

    ticket = _accept_ticket(confirm_key, items)

    # Let the confirm complete first: order confirmed, ticket cooking, no void.
    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            _confirm_result(ticket),
            settings=_settings(),
            counters=WorkerCounters(),
            now=now,
            rng=random.Random(0),
        )
        hold_unclaimable(session, order_id)
        order = session.get(Order, order_id)
        assert order is not None
        assert order.state == "confirmed"
        assert _void_work(session, order_id) is None

    with session_factory.begin() as session:
        cancelled = cancel_order(session, order_id, now=now)
        assert cancelled.outcome is CancelOutcome.APPLIED
        void_item = _void_work(session, order_id)
        assert void_item is not None
        assert void_item.idempotency_key == void_idempotency_key(order_id)
        assert isinstance(void_item.payload, dict)
        assert void_item.payload["accept_key"] == confirm_key
        assert void_item.payload["ticket_id"] == ticket["ticket_id"]
        # The confirm already returned, so this void is due immediately.
        assert void_item.next_attempt_at is not None
        assert void_item.next_attempt_at <= now
        claimed_void = _claim(session, void_item.id, now=now, worker_id="worker-void")

    voided = _post_void(order_id, confirm_key, ticket["ticket_id"])
    assert voided["voided"] is True

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed_void,
            HandlerResult(outcome="ok", result_payload=voided),
            settings=_settings(),
            counters=WorkerCounters(),
            now=now,
            rng=random.Random(0),
        )
        hold_unclaimable(session, order_id)

    counts = _ledger_counts()
    assert counts.get(confirm_key) == 1
    assert counts.get(void_idempotency_key(order_id)) == 1

    with session_factory() as session:
        order = session.get(Order, order_id)
        confirm_item = session.get(WorkItem, item_id)
        void_item = _void_work(session, order_id)
        assert order is not None
        assert confirm_item is not None
        assert void_item is not None
        assert order.state == "cancelled"
        assert confirm_item.status == "completed"
        assert void_item.status == "completed"
        assert _invalid_count(session, order_id) == 0
        assert _orphan_count(session, order_id) == 0


def test_cancel_with_a_dead_loser_still_compensates(
    session_factory: sessionmaker[Session],
) -> None:
    """Compensation cannot depend on the losing worker surviving to finalize.

    The worker claims confirm, the restaurant writes the ticket, and the worker
    dies — it never settles as supersession. Cancel has already queued the void
    at the call boundary, so the ticket is still compensated.
    """
    mix_off()
    now = datetime.now(UTC)
    items = ["chips"]
    with session_factory.begin() as session:
        order_id, item_id, _claimed = _place_claimed(session, now=now, cohort_id=uuid.uuid4())
        confirm_key = confirm_idempotency_key(order_id)

    # The restaurant write lands; the worker that made it never comes back.
    ticket = _accept_ticket(confirm_key, items)

    with session_factory.begin() as session:
        cancelled = cancel_order(session, order_id, now=now)
        assert cancelled.outcome is CancelOutcome.APPLIED
        confirm_item = session.get(WorkItem, item_id)
        void_item = _void_work(session, order_id)
        assert confirm_item is not None
        assert void_item is not None
        assert confirm_item.lease_until is not None
        # Not due until the lease that covers that one outbound call has expired:
        # a void running in front of the accept would burn the key on a no-op.
        assert void_item.next_attempt_at == confirm_item.lease_until
        assert (
            claim_next(
                session,
                now=now,
                lease_s=15.0,
                worker_id="worker-too-early",
                work_types=("void_ticket",),
                work_item_id=void_item.id,
            )
            is None
        )
        boundary = confirm_item.lease_until
        # The dead worker's confirm is cancelled, not merely unleased: an expired
        # lease must not let a survivor replay the accept onto a cancelled order.
        assert (
            claim_next(
                session,
                now=boundary,
                lease_s=15.0,
                worker_id="worker-survivor",
                work_types=("confirm",),
                work_item_id=item_id,
            )
            is None
        )
        claimed_void = _claim(session, void_item.id, now=boundary, worker_id="worker-void")

    voided = _post_void(order_id, confirm_key, ticket["ticket_id"])
    assert voided["voided"] is True

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed_void,
            HandlerResult(outcome="ok", result_payload=voided),
            settings=_settings(),
            counters=WorkerCounters(),
            now=boundary,
            rng=random.Random(0),
        )
        hold_unclaimable(session, order_id)

    counts = _ledger_counts()
    assert counts.get(confirm_key) == 1
    assert counts.get(void_idempotency_key(order_id)) == 1

    with session_factory() as session:
        order = session.get(Order, order_id)
        confirm_item = session.get(WorkItem, item_id)
        void_item = _void_work(session, order_id)
        assert order is not None
        assert confirm_item is not None
        assert void_item is not None
        # No resurrection: the abandoned confirm never advances the order.
        assert order.state == "cancelled"
        assert confirm_item.status == "cancelled"
        assert void_item.status == "completed"
        assert _invalid_count(session, order_id) == 0
        assert _orphan_count(session, order_id) == 0


def test_racing_compensation_triggers_create_one_void(
    session_factory: sessionmaker[Session],
) -> None:
    """Cancel and the losing finalize fire together: one work item, one effect.

    Both hold the order row lock before touching `work_items`, so they
    serialize. Without that shared lock order they interleave and the
    `UNIQUE(idempotency_key)` backstop raises `IntegrityError` — a 500 from
    `POST /orders/{id}/cancel` instead of a clean no-op.
    """
    mix_off()
    now = datetime.now(UTC)
    items = ["chips"]
    with session_factory.begin() as session:
        order_id, _item_id, claimed = _place_claimed(session, now=now, cohort_id=uuid.uuid4())
        confirm_key = confirm_idempotency_key(order_id)

    ticket = _accept_ticket(confirm_key, items)
    start = threading.Barrier(2)

    def run_cancel() -> None:
        start.wait(timeout=BARRIER_TIMEOUT_S)
        with session_factory.begin() as session:
            cancel_order(session, order_id, now=now)

    def run_finalize() -> None:
        start.wait(timeout=BARRIER_TIMEOUT_S)
        with session_factory.begin() as session:
            finalize_claim(
                session,
                claimed,
                _confirm_result(ticket),
                settings=_settings(),
                counters=WorkerCounters(),
                now=now,
                rng=random.Random(0),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_cancel), pool.submit(run_finalize)]
        for future in futures:
            # An IntegrityError from the unique backstop would surface here.
            future.result(timeout=BARRIER_TIMEOUT_S)

    with session_factory.begin() as session:
        # one_or_none() is the assertion: two rows would raise.
        void_item = _void_work(session, order_id)
        assert void_item is not None
        assert void_item.idempotency_key == void_idempotency_key(order_id)
        claimed_void = _claim(session, void_item.id, now=UNCLAIMABLE_AT, worker_id="worker-void")

    voided = _post_void(order_id, confirm_key, ticket["ticket_id"])
    assert voided["voided"] is True

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed_void,
            HandlerResult(outcome="ok", result_payload=voided),
            settings=_settings(),
            counters=WorkerCounters(),
            now=now,
            rng=random.Random(0),
        )
        hold_unclaimable(session, order_id)

    counts = _ledger_counts()
    assert counts.get(confirm_key) == 1
    assert counts.get(void_idempotency_key(order_id)) == 1

    with session_factory() as session:
        order = session.get(Order, order_id)
        assert order is not None
        assert order.state == "cancelled"
        assert _invalid_count(session, order_id) == 0
        assert _orphan_count(session, order_id) == 0


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


def test_permanent_void_failure_records_an_orphan(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        order = place_order(
            session,
            place_key=f"void-permanent-{uuid.uuid4()}",
            items=["chips"],
            cohort_id=uuid.uuid4(),
            ttl_hours=TTL_HOURS,
            now=now,
        )
        assert cancel_order(session, order.id, now=now).outcome is CancelOutcome.APPLIED
        void_item = WorkItem(
            order_id=order.id,
            work_type="void_ticket",
            status="pending",
            idempotency_key=void_idempotency_key(order.id),
            attempt_count=0,
            next_attempt_at=now,
            payload={"accept_key": confirm_idempotency_key(order.id)},
        )
        session.add(void_item)
        session.flush()
        claimed = _claim(session, void_item.id, now=now, worker_id="worker-void")
        order_id = order.id

    with session_factory.begin() as session:
        finalize_claim(
            session,
            claimed,
            HandlerResult(outcome="http_4xx"),
            settings=_settings(),
            counters=WorkerCounters(),
            now=now,
            rng=random.Random(0),
        )

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
        assert voided.result == {"orphaned_ticket": True}
        assert len(orphans) == 1
        assert _invalid_count(session, order_id) == 0
