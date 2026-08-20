"""SKIP LOCKED claim + attempt-at-claim INSERT (outcome NULL). Short txn only."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from order_pipeline.models import Attempt, Order, WorkItem
from order_pipeline.worker.log import log_worker_event
from order_pipeline.worker.plugin import ClaimedWork


def _eligible(now: datetime) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    due = or_(WorkItem.next_attempt_at.is_(None), WorkItem.next_attempt_at <= now)
    claimable = or_(
        WorkItem.status == "pending",
        and_(
            WorkItem.status == "leased",
            WorkItem.lease_until.is_not(None),
            WorkItem.lease_until < now,
        ),
    )
    return due, claimable


def claim_next(
    session: Session,
    *,
    now: datetime,
    lease_s: float,
    worker_id: str,
    work_types: Sequence[str],
    work_item_id: UUID | None = None,
) -> ClaimedWork | None:
    """Lock one due item with SKIP LOCKED, stamp the lease, INSERT a NULL-outcome attempt.

    Does not write order_events. Caller must commit this short txn before any HTTP.
    """
    if not work_types:
        return None

    due, claimable = _eligible(now)
    filters = [claimable, due, WorkItem.work_type.in_(tuple(work_types))]
    if work_item_id is not None:
        stmt = (
            select(WorkItem)
            .where(WorkItem.id == work_item_id, *filters)
            .with_for_update(skip_locked=True)
        )
    else:
        stmt = (
            select(WorkItem)
            .where(*filters)
            .order_by(WorkItem.next_attempt_at.asc().nulls_last())
            .limit(1)
            .with_for_update(skip_locked=True)
        )

    item = session.scalars(stmt).one_or_none()
    if item is None:
        return None

    order = session.get(Order, item.order_id)
    if order is None:
        raise RuntimeError(f"work item {item.id} points at a missing order")

    reclaiming = item.status == "leased"
    item.status = "leased"
    item.lease_owner = worker_id
    item.lease_until = now + timedelta(seconds=lease_s)
    item.attempt_count += 1

    attempt = Attempt(
        work_item_id=item.id,
        started_at=now,
        lease_owner=worker_id,
        outcome=None,
        ended_at=None,
    )
    session.add(attempt)
    session.flush()

    claimed = ClaimedWork(
        work_item_id=item.id,
        order_id=item.order_id,
        work_type=item.work_type,
        idempotency_key=item.idempotency_key,
        attempt_id=attempt.id,
        lease_owner=worker_id,
        attempt_count=item.attempt_count,
        payload=item.payload,
        order_state=order.state,
        order_version=order.version,
        accepted_at=order.accepted_at,
        items=order.items,
    )
    log_worker_event(
        "reclaim" if reclaiming else "claim",
        worker_id=worker_id,
        work_item_id=claimed.work_item_id,
        order_id=claimed.order_id,
        work_type=claimed.work_type,
        lease_owner=claimed.lease_owner,
        attempt_id=claimed.attempt_id,
        attempt_count=claimed.attempt_count,
    )
    return claimed
