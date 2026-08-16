"""Pre-pivot diner cancel. Race vs in-flight confirm (void_ticket) stays in bonus A."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from order_pipeline.models import Order, OrderEvent, WorkItem
from order_pipeline.worker.finalize import CAUSE_INVALID

CAUSE_CANCEL = "cancel"
ACTOR_API = "api"
LEGAL_FROM = ("placed", "confirmed")
OPEN_WORK_STATUSES = ("pending", "leased")


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

    Not void_ticket: in-flight kitchen HTTP may still finish; worker supersession
    must not count that as invalid.
    """
    session.execute(
        update(WorkItem)
        .where(
            WorkItem.order_id == order_id,
            WorkItem.status.in_(OPEN_WORK_STATUSES),
        )
        .values(status="cancelled", lease_owner=None, lease_until=None)
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
    if order.state not in LEGAL_FROM:
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
