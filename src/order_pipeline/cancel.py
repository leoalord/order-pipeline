"""Pre-pivot diner cancel. In-flight confirm finalize enqueues void_ticket."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from order_pipeline.lifecycle import CAUSE_INVALID, is_legal_transition
from order_pipeline.models import Order, OrderEvent, WorkItem

CAUSE_CANCEL = "cancel"
ACTOR_API = "api"


class OrderNotFound(Exception):
    """No row for this order id."""


class CancelOutcome(Enum):
    APPLIED = "applied"
    REPLAY = "replay"
    REJECTED = "rejected"


@dataclass(frozen=True)
class CancelResult:
    order: Order
    outcome: CancelOutcome


def _cancel_open_work(session: Session, order_id: UUID) -> None:
    """Stop quiet pre-pivot cancel from leaving claimable confirm/poll work.

    Pending and parked work are cancelled and unleased. Leased work is marked
    cancelled but keeps lease_owner so the in-flight worker can finalize as
    supersession (0-row UPDATE, not invalid). status=cancelled already blocks
    reclaim and prevents a later operator redrive from reviving terminal work.
    """
    session.execute(
        update(WorkItem)
        .where(
            WorkItem.order_id == order_id,
            WorkItem.status.in_(("pending", "parked")),
        )
        .values(
            status="cancelled",
            lease_owner=None,
            lease_until=None,
            park_owner=None,
            park_reason=None,
            park_next_action=None,
        )
    )
    session.execute(
        update(WorkItem)
        .where(WorkItem.order_id == order_id, WorkItem.status == "leased")
        .values(status="cancelled")
    )


def cancel_order(
    session: Session,
    order_id: UUID,
    *,
    now: datetime | None = None,
) -> CancelResult:
    """Guarded placed/confirmed → cancelled. Illegal after being_prepared is evidence only."""
    at = now or datetime.now(UTC)
    order = session.scalars(
        select(Order).where(Order.id == order_id).with_for_update()
    ).one_or_none()
    if order is None:
        raise OrderNotFound()
    if order.state == "cancelled":
        return CancelResult(order=order, outcome=CancelOutcome.REPLAY)
    if not is_legal_transition(order.state, "cancelled"):
        session.add(
            OrderEvent(
                order_id=order.id,
                from_state=order.state,
                to_state="cancelled",
                actor=ACTOR_API,
                cause=CAUSE_INVALID,
                timestamp=at,
                applied=False,
            )
        )
        return CancelResult(order=order, outcome=CancelOutcome.REJECTED)

    expected_state = order.state
    executed = session.execute(
        update(Order)
        .where(
            Order.id == order.id,
            Order.state == expected_state,
            Order.version == order.version,
        )
        .values(state="cancelled", version=Order.version + 1)
    )
    if getattr(executed, "rowcount", 0) != 1:
        session.add(
            OrderEvent(
                order_id=order.id,
                from_state=order.state,
                to_state="cancelled",
                actor=ACTOR_API,
                cause=CAUSE_INVALID,
                timestamp=at,
                applied=False,
            )
        )
        return CancelResult(order=order, outcome=CancelOutcome.REJECTED)

    session.add(
        OrderEvent(
            order_id=order.id,
            from_state=expected_state,
            to_state="cancelled",
            actor=ACTOR_API,
            cause=CAUSE_CANCEL,
            timestamp=at,
            applied=True,
        )
    )
    _cancel_open_work(session, order.id)
    session.refresh(order)
    return CancelResult(order=order, outcome=CancelOutcome.APPLIED)
