"""Session-level GET /snapshot assembly. HTTP walk lives in test_snapshot_compose."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.api.snapshot import (
    STAGE_NAMES,
    build_snapshot,
    duplicate_effects_from_ledgers,
    order_id_from_ledger_key,
    percentile,
)
from order_pipeline.intake import confirm_idempotency_key, place_order
from order_pipeline.models import Attempt, Order, OrderEvent, WorkItem
from order_pipeline.worker.dispatch import dispatch_idempotency_key
from order_pipeline.worker.finalize import CAUSE_INVALID

TTL_HOURS = 48


def _place(session: Session, *, cohort_id: uuid.UUID) -> Order:
    return place_order(
        session,
        place_key=f"snap-{uuid.uuid4()}",
        items=["chips"],
        cohort_id=cohort_id,
        ttl_hours=TTL_HOURS,
    )


def test_percentile_empty_and_single() -> None:
    assert percentile([], 50) is None
    assert percentile([12.0], 95) == 12.0
    assert percentile([1.0, 2.0, 3.0], 50) == 2.0


def test_duplicate_effects_count_ledger_extras_for_cohort_only() -> None:
    in_cohort = uuid.uuid4()
    other = uuid.uuid4()
    restaurant = {confirm_idempotency_key(in_cohort): 2, confirm_idempotency_key(other): 4}
    courier = {dispatch_idempotency_key(in_cohort): 1}
    assert duplicate_effects_from_ledgers([restaurant, courier], {in_cohort}) == 1
    assert duplicate_effects_from_ledgers([restaurant, courier], {other}) == 3
    assert order_id_from_ledger_key(confirm_idempotency_key(in_cohort)) == in_cohort


def test_snapshot_fields_cohort_filter_and_trace_null_attempts(
    session_factory: sessionmaker[Session],
) -> None:
    cohort = uuid.uuid4()
    other = uuid.uuid4()
    with session_factory.begin() as session:
        order = _place(session, cohort_id=cohort)
        outsider = _place(session, cohort_id=other)
        now = datetime.now(UTC) + timedelta(seconds=1)
        item = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
        abandoned = Attempt(
            work_item_id=item.id,
            started_at=now - timedelta(seconds=5),
            ended_at=None,
            lease_owner="worker-a",
            outcome=None,
        )
        survivor = Attempt(
            work_item_id=item.id,
            started_at=now - timedelta(seconds=2),
            ended_at=now,
            lease_owner="worker-b",
            outcome="ok",
        )
        session.add_all([abandoned, survivor])
        session.add(
            OrderEvent(
                order_id=order.id,
                from_state="placed",
                to_state="confirmed",
                actor="worker",
                cause="confirm",
                timestamp=now,
                applied=True,
            )
        )
        order.state = "confirmed"
        order.version += 1
        order_id = order.id
        outsider_id = outsider.id
        session.flush()

        snap = build_snapshot(
            session,
            cohort_id=cohort,
            now=now,
            ledger_counts=(
                {confirm_idempotency_key(order_id): 1, confirm_idempotency_key(outsider_id): 9},
            ),
            order_id=order_id,
        )

    assert snap.cohort_id == cohort
    assert snap.conservation.accepted == 1
    assert snap.conservation.in_flight == 1
    assert snap.conservation.residual == 0
    assert snap.startup_scan == 0
    assert snap.duplicate_attempts == 1
    assert snap.duplicate_effects == 0
    assert snap.invalid_transitions == 0
    assert snap.state_vs_last_order_events_mismatches == 0
    assert snap.currently_leased == 0
    assert tuple(snap.stages) == STAGE_NAMES
    assert snap.stages["confirmed"] == 1
    assert snap.trace is not None
    assert snap.trace.order_id == order_id
    outcomes = [row.outcome for row in snap.trace.attempts]
    assert None in outcomes
    assert "ok" in outcomes
    assert len(snap.trace.attempts) == 2
    confirmed = [
        event
        for event in snap.trace.order_events
        if event.to_state == "confirmed" and event.applied
    ]
    assert len(confirmed) == 1
    assert all(row.started_at is not None for row in snap.trace.attempts)
    assert snap.trace.attempts[0].ended_at is None
    assert snap.trace.attempts[1].ended_at is not None


def test_startup_scan_and_mismatch_and_leased_and_parked_outside(
    session_factory: sessionmaker[Session],
) -> None:
    cohort = uuid.uuid4()
    now = datetime.now(UTC)
    with session_factory() as session:
        session.begin()
        try:
            orphan = _place(session, cohort_id=cohort)
            leased = _place(session, cohort_id=cohort)
            parked_terminal = _place(session, cohort_id=cohort)
            session.flush()

            for item in session.scalars(select(WorkItem).where(WorkItem.order_id == orphan.id)):
                session.delete(item)
            session.flush()

            live = session.scalars(select(WorkItem).where(WorkItem.order_id == leased.id)).one()
            live.status = "leased"
            live.lease_owner = "worker-1"
            live.lease_until = now + timedelta(seconds=10)

            parked = session.scalars(
                select(WorkItem).where(WorkItem.order_id == parked_terminal.id)
            ).one()
            parked.status = "parked"
            parked.park_owner = "worker-1"
            parked.park_reason = "retry_budget_exhausted"
            parked.park_next_action = "redrive"
            parked_order = session.get(Order, parked_terminal.id)
            assert parked_order is not None
            parked_order.state = "delivered"

            mismatched = session.get(Order, leased.id)
            assert mismatched is not None
            mismatched.state = "ready"

            session.flush()
            snap = build_snapshot(
                session,
                cohort_id=cohort,
                now=now,
                ledger_counts=(),
                order_id=leased.id,
            )
            assert snap.startup_scan == 1
            assert snap.currently_leased == 1
            assert snap.conservation.parked == 1
            assert snap.conservation.residual == 1
            assert snap.state_vs_last_order_events_mismatches >= 1
            assert snap.invalid_transitions == 0
        finally:
            session.rollback()


def test_invalid_transition_events_are_counted(session_factory: sessionmaker[Session]) -> None:
    cohort = uuid.uuid4()
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        order = _place(session, cohort_id=cohort)
        session.add(
            OrderEvent(
                order_id=order.id,
                from_state="being_prepared",
                to_state="cancelled",
                actor="api",
                cause=CAUSE_INVALID,
                timestamp=now,
                applied=False,
            )
        )
        session.flush()
        snap = build_snapshot(session, cohort_id=cohort, now=now, ledger_counts=())
    assert snap.invalid_transitions == 1
