"""Session-level GET /snapshot assembly. HTTP walk lives in test_snapshot_compose."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.api.schemas import SnapshotResponse
from order_pipeline.api.snapshot import (
    STAGE_NAMES,
    build_snapshot,
    duplicate_effects_from_ledgers,
    fetch_ledger_counts,
    order_id_from_ledger_key,
    percentile,
    retry_attempt_ids,
    snapshot_read_session,
)
from order_pipeline.intake import confirm_idempotency_key, place_order, void_idempotency_key
from order_pipeline.lifecycle import CAUSE_INVALID, CAUSE_ORPHANED
from order_pipeline.models import Attempt, Order, OrderEvent, WorkItem
from order_pipeline.worker.dispatch import dispatch_idempotency_key
from tests.conftest import hold_unclaimable

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


def test_duplicate_effects_counts_two_keys_for_the_same_order() -> None:
    """Minting a new retry key is the real duplicate; per-key n-1 cannot see it."""
    order_id = uuid.uuid4()
    restaurant = {
        confirm_idempotency_key(order_id): 1,
        f"({order_id}, confirm-retry)": 1,
    }
    courier = {dispatch_idempotency_key(order_id): 1}
    assert duplicate_effects_from_ledgers([restaurant, courier], {order_id}) == 1
    assert (
        duplicate_effects_from_ledgers(
            [{confirm_idempotency_key(order_id): 1}, {dispatch_idempotency_key(order_id): 1}],
            {order_id},
        )
        == 0
    )


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
        hold_unclaimable(session, order_id, outsider_id)

    assert snap.cohort_id == cohort
    assert snap.conservation.accepted == 1
    assert snap.conservation.in_flight == 1
    assert snap.conservation.residual == 0
    assert snap.startup_scan == 0
    assert snap.duplicate_attempts == 1
    assert snap.duplicate_effects == 0
    assert snap.invalid_transitions == 0
    assert snap.orphaned_tickets == 0
    assert snap.state_vs_last_order_events_mismatches == 0
    assert snap.currently_leased == 0
    assert snap.currently_leased_items == []
    assert len(snap.orders) == 1
    assert snap.orders[0].id == order_id
    assert snap.orders[0].state == "confirmed"
    assert snap.orders[0].items == ["chips"]
    assert tuple(snap.stages) == STAGE_NAMES
    assert snap.stages["confirmed"] == 1
    assert snap.backlog["confirm"] == 1
    assert snap.oldest_open.stage == "confirmed"
    assert snap.oldest_open.age_s is not None
    assert snap.http_429s.door == 0
    assert snap.http_429s.kitchen == 0
    assert snap.http_429s.courier == 0
    assert snap.accept_reject.accepted == 1
    assert snap.accept_reject.rejected == 0
    assert snap.parked_list == []
    assert snap.retry_rate >= 0
    assert snap.sim_http.restaurant.requests_per_min >= 0
    assert snap.sim_http.restaurant.timeout == 0
    assert snap.sim_http.restaurant.http_5xx == 0
    assert snap.sim_http.restaurant.http_429 == 0
    assert snap.outbound_slots.worker_replicas == 2
    assert snap.outbound_slots.restaurant.cap == 16
    assert snap.outbound_slots.courier.cap == 16
    assert snap.outbound_slots.task.cap == 48
    assert snap.no_progress_beyond_threshold.count >= 0
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
    assert snap.trace.attempts[0].lease_owner == "worker-a"
    assert snap.trace.attempts[1].lease_owner == "worker-b"
    assert snap.trace.attempts[0].work_item_id == snap.trace.attempts[1].work_item_id
    assert snap.trace.attempts[0].idempotency_key == snap.trace.attempts[1].idempotency_key
    assert snap.trace.attempts[0].idempotency_key == confirm_idempotency_key(order_id)


def test_snapshot_splits_sim_errors_and_reports_honest_fleet_slot_totals(
    session_factory: sessionmaker[Session],
) -> None:
    cohort = uuid.uuid4()
    now = datetime.now(UTC)
    with session_factory() as session:
        session.begin()
        try:
            kitchen_order = _place(session, cohort_id=cohort)
            courier_order = _place(session, cohort_id=cohort)
            session.flush()

            kitchen = session.scalars(
                select(WorkItem).where(WorkItem.order_id == kitchen_order.id)
            ).one()
            kitchen.status = "leased"
            kitchen.lease_owner = "worker-a"
            kitchen.lease_until = now + timedelta(seconds=10)

            courier_confirm = session.scalars(
                select(WorkItem).where(WorkItem.order_id == courier_order.id)
            ).one()
            courier_confirm.status = "completed"
            courier_confirm.next_attempt_at = None
            courier_order.state = "ready"
            courier = WorkItem(
                order_id=courier_order.id,
                work_type="dispatch",
                status="leased",
                idempotency_key=f"slot-dispatch-{courier_order.id}",
                attempt_count=1,
                next_attempt_at=now,
                lease_owner="worker-b",
                lease_until=now + timedelta(seconds=10),
            )
            session.add(courier)
            session.flush()

            for index, outcome in enumerate(
                ("timeout", "dropped", "unknown", "http_5xx", "http_429")
            ):
                session.add(
                    Attempt(
                        work_item_id=kitchen.id,
                        started_at=now - timedelta(seconds=index + 1),
                        ended_at=now,
                        lease_owner="worker-a",
                        outcome=outcome,
                    )
                )
            for index, outcome in enumerate(("http_5xx", "http_429")):
                session.add(
                    Attempt(
                        work_item_id=courier.id,
                        started_at=now - timedelta(seconds=index + 1),
                        ended_at=now,
                        lease_owner="worker-b",
                        outcome=outcome,
                    )
                )
            session.add(
                Attempt(
                    work_item_id=kitchen.id,
                    started_at=now - timedelta(seconds=61),
                    ended_at=now - timedelta(seconds=61),
                    lease_owner="worker-old",
                    outcome="http_5xx",
                )
            )
            session.flush()

            snap = build_snapshot(session, cohort_id=cohort, now=now, ledger_counts=())
        finally:
            session.rollback()

    assert snap.sim_http.restaurant.timeout == 3
    assert snap.sim_http.restaurant.http_5xx == 1
    assert snap.sim_http.restaurant.http_429 == 1
    assert snap.sim_http.courier.timeout == 0
    assert snap.sim_http.courier.http_5xx == 1
    assert snap.sim_http.courier.http_429 == 1
    assert snap.outbound_slots.worker_replicas == 2
    assert snap.outbound_slots.restaurant.model_dump() == {
        "used": 1,
        "cap": 16,
        "per_worker_cap": 8,
    }
    assert snap.outbound_slots.courier.model_dump() == {
        "used": 1,
        "cap": 16,
        "per_worker_cap": 8,
    }
    assert snap.outbound_slots.task.model_dump() == {
        "used": 2,
        "cap": 48,
        "per_worker_cap": 24,
    }


def test_retry_metrics_exclude_successful_polling_and_count_fault_reexecution(
    session_factory: sessionmaker[Session],
) -> None:
    cohort = uuid.uuid4()
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        order = _place(session, cohort_id=cohort)
        confirm = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
        confirm.status = "completed"
        poll = WorkItem(
            order_id=order.id,
            work_type="poll_cook",
            status="pending",
            idempotency_key=f"poll-metrics-{order.id}",
            attempt_count=4,
            next_attempt_at=now,
        )
        dispatch = WorkItem(
            order_id=order.id,
            work_type="dispatch",
            status="pending",
            idempotency_key=f"dispatch-metrics-{order.id}",
            attempt_count=2,
            next_attempt_at=now,
        )
        session.add_all([poll, dispatch])
        session.flush()

        rows = [
            Attempt(
                work_item_id=poll.id,
                started_at=now - timedelta(seconds=6 - index),
                ended_at=now,
                lease_owner="worker-a",
                outcome=outcome,
            )
            for index, outcome in enumerate(("ok", "ok", "timeout", "ok"))
        ]
        rows.extend(
            [
                Attempt(
                    work_item_id=dispatch.id,
                    started_at=now - timedelta(seconds=2 - index),
                    ended_at=now,
                    lease_owner="worker-b",
                    outcome=outcome,
                )
                for index, outcome in enumerate(("http_5xx", "ok"))
            ]
        )
        session.add_all(rows)
        session.flush()

        retries = retry_attempt_ids(rows)
        snap = build_snapshot(session, cohort_id=cohort, now=now, ledger_counts=())
        hold_unclaimable(session, order.id)

    assert retries == {rows[3].id, rows[5].id}
    assert snap.duplicate_attempts == 2
    assert snap.retry_rate == pytest.approx(2 / 6)


def test_snapshot_stages_split_queued_confirmed_from_cooking_being_prepared(
    session_factory: sessionmaker[Session],
) -> None:
    """Existing `/` stage cards: confirmed = queued, being prepared = cooking."""
    cohort = uuid.uuid4()
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        queued = _place(session, cohort_id=cohort)
        cooking = _place(session, cohort_id=cohort)
        queued.state = "confirmed"
        queued.version += 1
        cooking.state = "being_prepared"
        cooking.version += 1
        session.flush()
        snap = build_snapshot(session, cohort_id=cohort, now=now, ledger_counts=())
        hold_unclaimable(session, queued.id, cooking.id)
    assert snap.stages["confirmed"] == 1
    assert snap.stages["being prepared"] == 1
    assert snap.stages["placed"] == 0


def test_stretching_etas_measure_only_sim_rail_wait(
    session_factory: sessionmaker[Session],
) -> None:
    cohort = uuid.uuid4()
    now = datetime.now(UTC)
    with session_factory() as session:
        session.begin()
        try:
            quiet = _place(session, cohort_id=cohort)
            stretched = _place(session, cohort_id=cohort)
            session.flush()

            for order, rail_wait in ((quiet, 0), (stretched, 10)):
                order.state = "out_for_delivery"
                order.version += 1
                sim_accepted = now + timedelta(seconds=40)
                service_started = sim_accepted + timedelta(seconds=rail_wait)
                session.add(
                    WorkItem(
                        order_id=order.id,
                        work_type="dispatch",
                        status="pending",
                        idempotency_key=f"stretch-dispatch-{order.id}",
                        attempt_count=0,
                        next_attempt_at=now,
                        result={
                            "accepted_at": sim_accepted.isoformat(),
                            "service_started_at": service_started.isoformat(),
                            "estimated_ready_at": (
                                service_started + timedelta(seconds=12)
                            ).isoformat(),
                        },
                    )
                )

            session.flush()
            snap = build_snapshot(session, cohort_id=cohort, now=now, ledger_counts=())
        finally:
            session.rollback()

    assert snap.stretching_etas.count == 1
    assert snap.stretching_etas.max_stretch_s == 10.0


def test_stretching_etas_include_assigned_courier_while_order_stays_ready(
    session_factory: sessionmaker[Session],
) -> None:
    cohort = uuid.uuid4()
    now = datetime.now(UTC)
    with session_factory() as session:
        session.begin()
        try:
            assigned = _place(session, cohort_id=cohort)
            assigned.state = "ready"
            assigned.version += 1
            accepted_at = now
            pickup_at = now + timedelta(seconds=15)
            session.add(
                WorkItem(
                    order_id=assigned.id,
                    work_type="poll_ride",
                    status="pending",
                    idempotency_key=f"assigned-poll-{assigned.id}",
                    attempt_count=0,
                    next_attempt_at=pickup_at,
                    payload={
                        "accepted_at": accepted_at.isoformat(),
                        "service_started_at": pickup_at.isoformat(),
                        "estimated_ready_at": (pickup_at + timedelta(seconds=20)).isoformat(),
                    },
                )
            )
            session.flush()
            snap = build_snapshot(session, cohort_id=cohort, now=now, ledger_counts=())
        finally:
            session.rollback()

    assert snap.stages["ready"] == 1
    assert snap.stretching_etas.count == 1
    assert snap.stretching_etas.max_stretch_s == 15.0


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
            assert len(snap.currently_leased_items) == 1
            leased_row = snap.currently_leased_items[0]
            assert leased_row.id == live.id
            assert leased_row.order_id == leased.id
            assert leased_row.work_type == live.work_type
            assert leased_row.owner == "worker-1"
            assert leased_row.lease_until == live.lease_until
            assert snap.conservation.parked == 1
            assert snap.conservation.residual == 1
            assert snap.state_vs_last_order_events_mismatches >= 1
            assert snap.invalid_transitions == 0
            assert len(snap.parked_list) == 1
            assert snap.parked_list[0].id == parked.id
            assert snap.parked_list[0].order_id == parked.order_id
            assert snap.parked_list[0].owner == "worker-1"
            assert snap.parked_list[0].reason == "retry_budget_exhausted"
            assert snap.parked_list[0].next_action == "redrive"
            assert snap.backlog["confirm"] == 1
            assert snap.http_429s.door == 0
        finally:
            session.rollback()


def test_void_ledger_keys_are_not_duplicate_effects() -> None:
    order_id = uuid.uuid4()
    restaurant = {
        confirm_idempotency_key(order_id): 1,
        void_idempotency_key(order_id): 1,
    }
    assert duplicate_effects_from_ledgers([restaurant], {order_id}) == 0


def test_orphaned_tickets_are_cohort_filtered(session_factory: sessionmaker[Session]) -> None:
    cohort = uuid.uuid4()
    other = uuid.uuid4()
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        order = _place(session, cohort_id=cohort)
        outsider = _place(session, cohort_id=other)
        session.add(
            OrderEvent(
                order_id=order.id,
                from_state="cancelled",
                to_state="cancelled",
                actor="worker",
                cause=CAUSE_ORPHANED,
                timestamp=now,
                applied=False,
            )
        )
        session.add(
            OrderEvent(
                order_id=outsider.id,
                from_state="cancelled",
                to_state="cancelled",
                actor="worker",
                cause=CAUSE_ORPHANED,
                timestamp=now,
                applied=False,
            )
        )
        session.flush()
        snap = build_snapshot(session, cohort_id=cohort, now=now, ledger_counts=())
        hold_unclaimable(session, order.id, outsider.id)
    assert snap.orphaned_tickets == 1
    assert snap.invalid_transitions == 0


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
        hold_unclaimable(session, order.id)
    assert snap.invalid_transitions == 1


def test_fetch_ledger_counts_unreachable_is_not_ok() -> None:
    counts, ok = fetch_ledger_counts("http://127.0.0.1:1", timeout_s=0.2)
    assert ok is False
    assert counts == {}


def test_snapshot_duplicate_effects_none_when_ledgers_unavailable(
    session_factory: sessionmaker[Session],
) -> None:
    cohort = uuid.uuid4()
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        order = _place(session, cohort_id=cohort)
        snap = build_snapshot(
            session,
            cohort_id=cohort,
            now=now,
            ledger_counts=(),
            ledgers_ok=False,
            door_429s=3,
        )
        hold_unclaimable(session, order.id)
    assert snap.duplicate_effects is None
    assert snap.conservation.residual == 0
    assert snap.http_429s.door == 3
    assert snap.accept_reject.rejected == 3
    assert snap.accept_reject.accepted == 1


def _confirm_like_worker(
    factory: sessionmaker[Session], order_id: uuid.UUID, now: datetime
) -> None:
    with factory.begin() as session:
        order = session.get(Order, order_id)
        assert order is not None
        session.add(
            OrderEvent(
                order_id=order_id,
                from_state=order.state,
                to_state="confirmed",
                actor="worker",
                cause="confirm",
                timestamp=now,
                applied=True,
            )
        )
        order.state = "confirmed"
        order.version += 1


def _snapshot_with_interleaved_commit(
    session: Session,
    *,
    cohort_id: uuid.UUID,
    now: datetime,
    on_after_orders: Callable[[], None],
) -> SnapshotResponse:
    return build_snapshot(
        session,
        cohort_id=cohort_id,
        now=now,
        ledger_counts=(),
        after_orders=on_after_orders,
    )


def test_snapshot_read_session_is_repeatable_read_readonly(db_engine: Engine) -> None:
    with snapshot_read_session(db_engine) as session:
        isolation = session.execute(text("SHOW transaction_isolation")).scalar_one()
        readonly = session.execute(text("SHOW transaction_read_only")).scalar_one()
    assert isolation == "repeatable read"
    assert readonly == "on"


def test_repeatable_read_snapshot_is_self_consistent_across_interleaved_commit(
    db_engine: Engine,
    session_factory: sessionmaker[Session],
) -> None:
    """A worker commit between orders and events must not mix pre/post views."""
    cohort = uuid.uuid4()
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        order = _place(session, cohort_id=cohort)
        order_id = order.id
        hold_unclaimable(session, order_id)

    def interleave() -> None:
        _confirm_like_worker(session_factory, order_id, now + timedelta(seconds=1))

    with snapshot_read_session(db_engine) as session:
        snap = _snapshot_with_interleaved_commit(
            session, cohort_id=cohort, now=now, on_after_orders=interleave
        )

    assert snap.state_vs_last_order_events_mismatches == 0
    assert snap.stages["placed"] == 1
    assert snap.stages.get("confirmed", 0) == 0

    with snapshot_read_session(db_engine) as session:
        after = build_snapshot(session, cohort_id=cohort, now=now, ledger_counts=())
    assert after.stages["confirmed"] == 1
    assert after.state_vs_last_order_events_mismatches == 0


def test_read_committed_snapshot_can_mix_pre_and_post_commit_views(
    db_engine: Engine,
    session_factory: sessionmaker[Session],
) -> None:
    """Characterize the false mismatch: READ COMMITTED is what REPEATABLE READ fixes."""
    cohort = uuid.uuid4()
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        order = _place(session, cohort_id=cohort)
        order_id = order.id
        hold_unclaimable(session, order_id)

    def interleave() -> None:
        _confirm_like_worker(session_factory, order_id, now + timedelta(seconds=1))

    with Session(db_engine, expire_on_commit=False) as session:
        snap = _snapshot_with_interleaved_commit(
            session, cohort_id=cohort, now=now, on_after_orders=interleave
        )

    assert snap.state_vs_last_order_events_mismatches >= 1
    assert snap.stages["placed"] == 1


def test_get_snapshot_opens_repeatable_read_before_postgres_queries() -> None:
    from pathlib import Path

    app = (Path(__file__).resolve().parents[1] / "src/order_pipeline/api/app.py").read_text()
    snap = (Path(__file__).resolve().parents[1] / "src/order_pipeline/api/snapshot.py").read_text()
    endpoint = app.split("def get_snapshot", 1)[1].split("def get_order", 1)[0]
    assert "snapshot_read_session(engine)" in endpoint
    assert "SessionLocal()" not in endpoint
    assert 'isolation_level="REPEATABLE READ"' in snap
    assert "SET TRANSACTION READ ONLY" in snap
