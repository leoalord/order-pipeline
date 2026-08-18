"""Post-HTTP short txn: classify policy, guarded UPDATE, events, work-item disposition."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from order_pipeline.intake import void_idempotency_key
from order_pipeline.lifecycle import CAUSE_INVALID, CAUSE_ORPHANED, is_legal_transition
from order_pipeline.models import Attempt, Order, OrderEvent, WorkItem
from order_pipeline.worker.backoff import full_jitter_delay_s
from order_pipeline.worker.classify import PERMANENT_OUTCOMES
from order_pipeline.worker.counters import WorkerCounters
from order_pipeline.worker.plugin import (
    ClaimedWork,
    GuardedTransition,
    HandlerResult,
    NextWork,
    WorkDisposition,
)
from order_pipeline.worker.settings import WorkerSettings
from order_pipeline.worker.stop_rules import (
    CONFIRM_WORK_TYPES,
    confirm_deadline_exceeded,
    count_budget_exhausted,
    count_budget_for,
)

CAUSE_SUPERSEDED = "superseded_by_cancel"
CAUSE_PERMANENT_4XX = "permanent_4xx"
CAUSE_CONFIRM_DEADLINE = "confirm_deadline"
PARK_REASON_GUARD_REJECTED = "guarded_transition_rejected"
PARK_NEXT_ACTION_GUARD_REJECTED = "inspect_then_redrive"
TERMINAL_ORDER_STATES = frozenset({"delivered", "cancelled", "failed"})
VOID_TICKET_WORK_TYPE = "void_ticket"


def _orphan_if_void_exhausted(
    claimed: ClaimedWork,
    settings: WorkerSettings,
    result: HandlerResult,
) -> HandlerResult | None:
    """Void exhaustion records an orphan; it must not park or fail the cancelled order."""
    if claimed.work_type != VOID_TICKET_WORK_TYPE:
        return None
    budget = count_budget_for(claimed.work_type, settings)
    if budget is None or not count_budget_exhausted(claimed.attempt_count, budget):
        return None
    payload = result.result_payload if isinstance(result.result_payload, dict) else {}
    return HandlerResult(
        outcome=result.outcome,
        disposition=WorkDisposition.COMPLETE,
        result_payload={**payload, "orphaned_ticket": True},
    )


def _park_if_budget_exhausted(
    claimed: ClaimedWork,
    settings: WorkerSettings,
    result: HandlerResult,
) -> HandlerResult | None:
    """Count-bounded stop rule. Applies to failed *and* successful not-ready retries."""
    if claimed.work_type == VOID_TICKET_WORK_TYPE:
        return None
    budget = count_budget_for(claimed.work_type, settings)
    if budget is None or not count_budget_exhausted(claimed.attempt_count, budget):
        return None
    reason = (
        "poll_budget_exhausted"
        if claimed.work_type in {"poll_cook", "poll_ride"}
        else "retry_budget_exhausted"
    )
    return HandlerResult(
        outcome=result.outcome,
        disposition=WorkDisposition.PARK,
        transition=result.transition,
        result_payload=result.result_payload,
        park_reason=reason,
        park_next_action="redrive",
    )


def apply_policy(
    result: HandlerResult,
    claimed: ClaimedWork,
    settings: WorkerSettings,
    now: datetime,
) -> HandlerResult:
    """Chassis owns 4xx-permanent vs 429-retry. Handlers must not rebuild this branch."""
    # A call may start just before the confirm deadline and return after it.
    # Re-check before accepting even a successful response so time-bounded
    # confirm work cannot cross its 120s clock through that narrow edge.
    if claimed.work_type in CONFIRM_WORK_TYPES and confirm_deadline_exceeded(
        claimed.accepted_at, now, settings.confirm_deadline_s
    ):
        return HandlerResult(
            outcome=result.outcome,
            disposition=WorkDisposition.FAIL_ORDER,
            transition=GuardedTransition(
                expected_state=claimed.order_state,
                to_state="failed",
                cause=CAUSE_CONFIRM_DEADLINE,
            ),
            result_payload=result.result_payload,
        )

    if result.outcome in PERMANENT_OUTCOMES:
        if claimed.work_type == VOID_TICKET_WORK_TYPE:
            payload = result.result_payload if isinstance(result.result_payload, dict) else {}
            return HandlerResult(
                outcome=result.outcome,
                disposition=WorkDisposition.COMPLETE,
                result_payload={**payload, "orphaned_ticket": True},
            )
        return HandlerResult(
            outcome=result.outcome,
            disposition=WorkDisposition.FAIL_ORDER,
            transition=GuardedTransition(
                expected_state=claimed.order_state,
                to_state="failed",
                cause=CAUSE_PERMANENT_4XX,
            ),
            result_payload=result.result_payload,
        )

    if result.outcome == "ok":
        disposition = result.disposition or WorkDisposition.COMPLETE
        if disposition is WorkDisposition.RETRY:
            parked = _park_if_budget_exhausted(claimed, settings, result)
            if parked is not None:
                return parked
        return HandlerResult(
            outcome=result.outcome,
            disposition=disposition,
            transition=result.transition,
            next_work=result.next_work,
            result_payload=result.result_payload,
            park_reason=result.park_reason,
            park_next_action=result.park_next_action,
            next_attempt_at=result.next_attempt_at,
        )

    orphaned = _orphan_if_void_exhausted(claimed, settings, result)
    if orphaned is not None:
        return orphaned

    parked = _park_if_budget_exhausted(claimed, settings, result)
    if parked is not None:
        return parked

    return HandlerResult(
        outcome=result.outcome,
        disposition=WorkDisposition.RETRY,
        result_payload=result.result_payload,
    )


def _release_lease(item: WorkItem) -> None:
    item.lease_owner = None
    item.lease_until = None


def _append_evidence(
    session: Session,
    *,
    order_id: UUID,
    from_state: str,
    to_state: str,
    cause: str,
    now: datetime,
) -> None:
    session.add(
        OrderEvent(
            order_id=order_id,
            from_state=from_state,
            to_state=to_state,
            actor="worker",
            cause=cause,
            timestamp=now,
            applied=False,
        )
    )


def apply_guarded_transition(
    session: Session,
    claimed: ClaimedWork,
    transition: GuardedTransition,
    counters: WorkerCounters,
    now: datetime,
) -> bool:
    """Conditional UPDATE. Zero rows are never applied. Cancel race is supersession, not invalid."""
    if not is_legal_transition(transition.expected_state, transition.to_state):
        order = session.get(Order, claimed.order_id)
        current_state = order.state if order is not None else claimed.order_state
        counters.invalid_transitions += 1
        _append_evidence(
            session,
            order_id=claimed.order_id,
            from_state=current_state,
            to_state=transition.to_state,
            cause=CAUSE_INVALID,
            now=now,
        )
        return False

    executed = session.execute(
        update(Order)
        .where(
            Order.id == claimed.order_id,
            Order.state == transition.expected_state,
            Order.version == claimed.order_version,
        )
        .values(state=transition.to_state, version=Order.version + 1)
    )
    if getattr(executed, "rowcount", 0) == 1:
        session.add(
            OrderEvent(
                order_id=claimed.order_id,
                from_state=transition.expected_state,
                to_state=transition.to_state,
                actor=transition.actor,
                cause=transition.cause,
                timestamp=now,
                applied=True,
            )
        )
        return True

    order = session.get(Order, claimed.order_id)
    current_state = order.state if order is not None else claimed.order_state
    if current_state == "cancelled":
        _append_evidence(
            session,
            order_id=claimed.order_id,
            from_state=current_state,
            to_state=transition.to_state,
            cause=CAUSE_SUPERSEDED,
            now=now,
        )
        return False

    counters.invalid_transitions += 1
    _append_evidence(
        session,
        order_id=claimed.order_id,
        from_state=current_state,
        to_state=transition.to_state,
        cause=CAUSE_INVALID,
        now=now,
    )
    return False


def _is_orphan_payload(payload: object) -> bool:
    return isinstance(payload, dict) and payload.get("orphaned_ticket") is True


def _enqueue_void_ticket(
    session: Session,
    claimed: ClaimedWork,
    policy: HandlerResult,
    now: datetime,
) -> None:
    key = void_idempotency_key(claimed.order_id)
    existing = session.scalars(
        select(WorkItem).where(WorkItem.idempotency_key == key)
    ).one_or_none()
    if existing is not None:
        return
    payload = dict(policy.result_payload) if isinstance(policy.result_payload, dict) else {}
    # Even an unknown/failed confirm has a stable stored key. The restaurant
    # resolves it to the applied ticket or completes an idempotent no-op.
    payload["accept_key"] = claimed.idempotency_key
    session.add(
        WorkItem(
            order_id=claimed.order_id,
            work_type=VOID_TICKET_WORK_TYPE,
            status="pending",
            idempotency_key=key,
            attempt_count=0,
            next_attempt_at=now,
            payload=payload,
        )
    )


def _enqueue_next(session: Session, claimed: ClaimedWork, nxt: NextWork, now: datetime) -> None:
    session.add(
        WorkItem(
            order_id=claimed.order_id,
            work_type=nxt.work_type,
            status="pending",
            idempotency_key=nxt.idempotency_key,
            attempt_count=0,
            next_attempt_at=nxt.next_attempt_at or now,
            payload=nxt.payload,
        )
    )


def finalize_claim(
    session: Session,
    claimed: ClaimedWork,
    result: HandlerResult,
    *,
    settings: WorkerSettings,
    counters: WorkerCounters,
    now: datetime,
    rng: random.Random,
) -> None:
    """Finalize the attempt opened at claim. Never rewrite a stolen claimant's NULL row."""
    policy = apply_policy(result, claimed, settings, now)
    attempt = session.get(Attempt, claimed.attempt_id)
    if attempt is None:
        raise RuntimeError(f"attempt {claimed.attempt_id} missing at finalize")
    if attempt.outcome is not None:
        return

    item = session.get(WorkItem, claimed.work_item_id)
    if item is None:
        return
    if item.lease_owner != claimed.lease_owner:
        # Reclaim gap: leave this attempt NULL. Do not stamp a late outcome.
        return

    attempt.outcome = policy.outcome
    attempt.ended_at = now

    applied = True
    if policy.transition is not None:
        applied = apply_guarded_transition(session, claimed, policy.transition, counters, now)

    # Cancel keeps lease_owner on in-flight work so this finalize can settle.
    # Re-read the order after the guarded UPDATE: a leased RETRY/PARK has no
    # transition, so the cancelled check must not live only inside `if not applied`.
    # Lock the order (same first lock as cancel/redrive). Do not lock the work
    # item here — the earlier unlocked get is only a lease-owner check, and
    # locking it first would invert order → work_item.
    # Skip only when the order is already terminal for a reason we did not just
    # apply (cancel). A 4xx/deadline fail that we just wrote must still FAIL_ORDER.
    order = session.get(Order, claimed.order_id, with_for_update=True)
    just_applied_terminal = (
        applied
        and policy.transition is not None
        and order is not None
        and policy.transition.to_state == order.state
        and order.state in TERMINAL_ORDER_STATES
    )
    if (
        order is not None
        and order.state in TERMINAL_ORDER_STATES
        and not just_applied_terminal
        and claimed.work_type != VOID_TICKET_WORK_TYPE
    ):
        _release_lease(item)
        item.status = "cancelled"
        if claimed.work_type in CONFIRM_WORK_TYPES and order.state == "cancelled":
            _enqueue_void_ticket(session, claimed, policy, now)
        return

    if not applied:
        _release_lease(item)
        item.status = "parked"
        item.park_owner = claimed.lease_owner
        item.park_reason = PARK_REASON_GUARD_REJECTED
        item.park_next_action = PARK_NEXT_ACTION_GUARD_REJECTED
        return

    disposition = policy.disposition or WorkDisposition.COMPLETE
    if disposition is WorkDisposition.FAIL_ORDER:
        _release_lease(item)
        item.status = "failed"
        item.result = policy.result_payload
        return
    if disposition is WorkDisposition.PARK:
        _release_lease(item)
        item.status = "parked"
        item.park_owner = claimed.lease_owner
        item.park_reason = policy.park_reason
        item.park_next_action = policy.park_next_action
        return
    if disposition is WorkDisposition.RETRY:
        _release_lease(item)
        item.status = "pending"
        if policy.next_attempt_at is not None:
            item.next_attempt_at = policy.next_attempt_at
        else:
            delay = full_jitter_delay_s(item.attempt_count, settings, rng)
            item.next_attempt_at = now + timedelta(seconds=delay)
        return

    _release_lease(item)
    item.status = "completed"
    item.result = policy.result_payload
    if claimed.work_type == VOID_TICKET_WORK_TYPE and _is_orphan_payload(policy.result_payload):
        current = order.state if order is not None else claimed.order_state
        _append_evidence(
            session,
            order_id=claimed.order_id,
            from_state=current,
            to_state=current,
            cause=CAUSE_ORPHANED,
            now=now,
        )
    for nxt in policy.next_work:
        _enqueue_next(session, claimed, nxt, now)
