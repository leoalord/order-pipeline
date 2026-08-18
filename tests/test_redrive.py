"""Redrive reuses a parked durable job and resets only its queue eligibility."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.intake import place_order
from order_pipeline.models import Attempt, Order, WorkItem
from order_pipeline.redrive import (
    WorkItemNotFound,
    WorkItemNotParked,
    WorkItemOrderTerminal,
    redrive_work_item,
)

TTL_HOURS = 48


def _parked_item(session: Session, *, now: datetime) -> tuple[uuid.UUID, str, uuid.UUID]:
    order = place_order(
        session,
        place_key=f"redrive-{uuid.uuid4()}",
        items=["chips"],
        cohort_id=uuid.uuid4(),
        ttl_hours=TTL_HOURS,
    )
    item = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
    item.status = "parked"
    item.attempt_count = 5
    item.next_attempt_at = now + timedelta(hours=1)
    item.lease_owner = None
    item.lease_until = None
    item.park_owner = "worker-old"
    item.park_reason = "retry_budget_exhausted"
    item.park_next_action = "redrive"
    item.payload = {"kept": "payload"}
    item.result = {"kept": "result"}
    session.add(
        Attempt(
            work_item_id=item.id,
            started_at=now - timedelta(seconds=2),
            ended_at=now,
            lease_owner="worker-old",
            outcome="timeout",
        )
    )
    session.flush()
    return item.id, item.idempotency_key, order.id


def test_redrive_resets_parked_budget_and_keeps_job_identity_and_history(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        item_id, stored_key, order_id = _parked_item(session, now=now)

    with session_factory.begin() as session:
        redriven = redrive_work_item(session, item_id, now=now)
        assert redriven.id == item_id
        assert redriven.order_id == order_id
        assert redriven.idempotency_key == stored_key
        assert redriven.status == "pending"
        assert redriven.attempt_count == 0
        assert redriven.next_attempt_at == now
        assert redriven.lease_owner is None
        assert redriven.lease_until is None
        assert redriven.park_owner is None
        assert redriven.park_reason is None
        assert redriven.park_next_action is None
        assert redriven.payload == {"kept": "payload"}
        assert redriven.result == {"kept": "result"}

    with session_factory() as session:
        stored = session.get(WorkItem, item_id)
        assert stored is not None
        attempts = session.scalars(select(Attempt).where(Attempt.work_item_id == item_id)).all()
        assert len(attempts) == 1
        assert attempts[0].outcome == "timeout"


def test_redrive_rejects_missing_and_nonparked_items(
    session_factory: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        with pytest.raises(WorkItemNotFound):
            redrive_work_item(session, uuid.uuid4(), now=now)

        order = place_order(
            session,
            place_key=f"redrive-nonparked-{uuid.uuid4()}",
            items=["chips"],
            cohort_id=uuid.uuid4(),
            ttl_hours=TTL_HOURS,
        )
        item = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
        with pytest.raises(WorkItemNotParked):
            redrive_work_item(session, item.id, now=now)


@pytest.mark.parametrize("terminal_state", ["delivered", "cancelled", "failed"])
def test_redrive_rejects_parked_work_for_terminal_order(
    session_factory: sessionmaker[Session],
    terminal_state: str,
) -> None:
    now = datetime.now(UTC)
    with session_factory.begin() as session:
        item_id, _, order_id = _parked_item(session, now=now)
        order = session.get(Order, order_id)
        assert order is not None
        order.state = terminal_state

    with session_factory.begin() as session:
        with pytest.raises(WorkItemOrderTerminal):
            redrive_work_item(session, item_id, now=now)

    with session_factory() as session:
        stored = session.get(WorkItem, item_id)
        assert stored is not None
        assert stored.status == "parked"
        assert stored.attempt_count == 5
