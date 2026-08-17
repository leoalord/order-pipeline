"""Operator redrive for parked work items.

Redrive reuses the same durable job. It resets queue eligibility and the
count-bounded attempt budget, but deliberately keeps the work-item id,
idempotency key, payload/result, and historical attempt rows.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from order_pipeline.models import WorkItem


class WorkItemNotFound(Exception):
    pass


class WorkItemNotParked(Exception):
    pass


def redrive_work_item(session: Session, work_item_id: UUID, *, now: datetime) -> WorkItem:
    """Make one parked item immediately claimable again, preserving its operation key."""
    item = session.get(WorkItem, work_item_id, with_for_update=True)
    if item is None:
        raise WorkItemNotFound
    if item.status != "parked":
        raise WorkItemNotParked

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
