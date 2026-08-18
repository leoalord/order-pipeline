"""Operator redrive for parked work items.

Redrive reuses the same durable job. It resets queue eligibility and the
count-bounded attempt budget, but deliberately keeps the work-item id,
idempotency key, payload/result, and historical attempt rows.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from order_pipeline.models import Order, WorkItem

TERMINAL_ORDER_STATES = frozenset({"delivered", "cancelled", "failed"})


class WorkItemNotFound(Exception):
    pass


class WorkItemNotParked(Exception):
    pass


class WorkItemOrderTerminal(Exception):
    pass


def redrive_work_item(session: Session, work_item_id: UUID, *, now: datetime) -> WorkItem:
    """Make one parked item immediately claimable again, preserving its operation key."""
    order_id = session.scalar(select(WorkItem.order_id).where(WorkItem.id == work_item_id))
    if order_id is None:
        raise WorkItemNotFound

    # Lock in the same order as cancel_order (order, then work item) so a cancel
    # racing Redrive cannot leave pending work behind on a terminal order.
    order = session.scalars(
        select(Order).where(Order.id == order_id).with_for_update()
    ).one_or_none()
    item = session.scalars(
        select(WorkItem).where(WorkItem.id == work_item_id).with_for_update()
    ).one_or_none()
    if order is None or item is None:
        raise WorkItemNotFound
    if item.status != "parked":
        raise WorkItemNotParked
    if order.state in TERMINAL_ORDER_STATES:
        raise WorkItemOrderTerminal

    item.status = "pending"
    item.attempt_count = 0
    item.next_attempt_at = now
    item.lease_owner = None
    item.lease_until = None
    item.park_owner = None
    item.park_reason = None
    item.park_next_action = None
    session.flush()
    return item
