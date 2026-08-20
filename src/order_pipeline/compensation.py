"""Kitchen compensation: one idempotent `void_ticket` per order, however we learn.

Three paths can discover that the restaurant may hold a ticket for an order
that will never complete:

1. a cancel from `confirmed` — the accept already returned and the ticket cooks;
2. a cancel that supersedes an in-flight confirm — the losing worker's call may
   still be on the wire, and that worker may die before it can finalize;
3. a confirm that crosses its deadline — the order fails explicitly, but the
   stable accept key may already have produced a ticket.

All three enqueue through `enqueue_void_ticket` under the deterministic
`(order_id, void)` key, so concurrent triggers converge on one work item and
one restaurant effect. Compensation is a work item, not a saga: the diner
guarantee (cancelled stays cancelled) and the kitchen-side remainder are
tracked separately, and void exhaustion surfaces as an orphaned ticket.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from order_pipeline.intake import CONFIRM_WORK_TYPE, void_idempotency_key
from order_pipeline.models import WorkItem

VOID_TICKET_WORK_TYPE = "void_ticket"

# Work types that call the restaurant's accept endpoint under the order's
# stored key. Only these can have left a ticket behind.
CONFIRM_WORK_TYPES = frozenset({CONFIRM_WORK_TYPE, "submit"})


@dataclass(frozen=True)
class KitchenEffect:
    """What the accept key may have left in the kitchen, and when voiding is safe.

    `ready_at` is the call boundary, not a delay knob — see `_call_boundary`.
    """

    accept_key: str
    ready_at: datetime
    payload: dict[str, Any]


def _call_boundary(item: WorkItem, now: datetime) -> datetime:
    """Earliest moment a void cannot overtake the accept it has to compensate.

    A lease covers exactly one outbound call and `WorkerSettings` asserts
    `lease_s > sim_timeout_s`, so once `lease_until` passes the accept has
    returned, timed out, or died with its worker: the ticket is either in the
    restaurant ledger already or it never will be. Voiding before that boundary
    would burn `(order_id, void)` on the sim's replayable `absent` no-op and
    leave the ticket that lands a moment later live forever.
    """
    if item.lease_until is not None and item.lease_until > now:
        return item.lease_until
    return now


def _ticket_payload(item: WorkItem) -> dict[str, Any]:
    """Carry `ticket_id` through when the confirm already stored its ticket."""
    result = item.result
    if not isinstance(result, dict):
        return {}
    ticket_id = result.get("ticket_id")
    if isinstance(ticket_id, str) and ticket_id:
        return {"ticket_id": ticket_id}
    return {}


def kitchen_effect_at_cancel(
    session: Session,
    order_id: UUID,
    *,
    order_state: str,
    now: datetime,
) -> KitchenEffect | None:
    """Read whether cancelling this order can strand a ticket. Call before `_cancel_open_work`.

    The lease and status this reads are the pre-cancel ones; `_cancel_open_work`
    rewrites both. Returns None only when the accept key provably never reached
    the restaurant: the claim increments `attempt_count` and commits before any
    HTTP, so a zero count means no call was ever started. `confirmed` is
    belt-and-braces — only a successful accept can produce that state.
    """
    item = session.scalars(
        select(WorkItem).where(
            WorkItem.order_id == order_id,
            WorkItem.work_type.in_(tuple(CONFIRM_WORK_TYPES)),
        )
    ).one_or_none()
    if item is None:
        return None
    if item.attempt_count == 0 and order_state != "confirmed":
        return None
    return KitchenEffect(
        accept_key=item.idempotency_key,
        ready_at=_call_boundary(item, now),
        payload=_ticket_payload(item),
    )


def enqueue_void_ticket(
    session: Session,
    *,
    order_id: UUID,
    effect: KitchenEffect,
) -> WorkItem:
    """Create the one `void_ticket` for this order, or pull the existing one forward.

    The caller must already hold the order row lock — `cancel_order` takes it
    with `SELECT ... FOR UPDATE`, `finalize_claim` with
    `session.get(Order, ..., with_for_update=True)` — and must take it *before*
    touching `work_items`. That one lock order (order, then work item, the same
    one `redrive_work_item` uses) is what serializes two concurrent triggers.
    `UNIQUE(idempotency_key)` stays the backstop; without the lock the paths
    interleave and it raises `IntegrityError`, surfacing as a 500 from
    `POST /orders/{id}/cancel` instead of a clean no-op.
    """
    key = void_idempotency_key(order_id)
    payload: dict[str, Any] = {**effect.payload, "accept_key": effect.accept_key}
    existing = session.scalars(
        select(WorkItem).where(WorkItem.idempotency_key == key)
    ).one_or_none()
    if existing is not None:
        _merge_into_pending(existing, effect.ready_at, payload)
        return existing

    item = WorkItem(
        order_id=order_id,
        work_type=VOID_TICKET_WORK_TYPE,
        status="pending",
        idempotency_key=key,
        attempt_count=0,
        next_attempt_at=effect.ready_at,
        payload=payload,
    )
    session.add(item)
    session.flush()
    return item


def _merge_into_pending(
    item: WorkItem,
    ready_at: datetime,
    payload: dict[str, Any],
) -> None:
    """A second trigger sharpens the queued void; it never re-arms a settled one.

    Cancel enqueues at the call boundary because the confirm may still be on the
    wire. When the live loser then finalizes, that boundary has demonstrably
    passed, so it pulls the same item forward and adds the `ticket_id` it just
    learned. Time only ever moves earlier, and a void already claimed,
    completed, or cancelled is left alone.
    """
    if item.status != "pending":
        return
    stored = dict(item.payload) if isinstance(item.payload, dict) else {}
    merged = {**stored, **payload}
    if merged != stored:
        item.payload = merged
    if item.next_attempt_at is None or ready_at < item.next_attempt_at:
        item.next_attempt_at = ready_at
