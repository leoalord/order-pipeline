"""Post-HTTP short txn: classify policy, guarded UPDATE, events, work-item disposition."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.orm import Session

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

CAUSE_INVALID = "invalid_transition"
CAUSE_SUPERSEDED = "superseded_by_cancel"
CAUSE_PERMANENT_4XX = "permanent_4xx"
CAUSE_CONFIRM_DEADLINE = "confirm_deadline"


def _park_if_budget_exhausted(
    claimed: ClaimedWork,
    settings: WorkerSettings,
    result: HandlerResult,
) -> HandlerResult | None:
    """Count-bounded stop rule. Applies to failed *and* successful not-ready retries."""
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
    if result.outcome in PERMANENT_OUTCOMES:
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

    if not applied:
        _release_lease(item)
        order = session.get(Order, claimed.order_id)
        item.status = "cancelled" if order is not None and order.state == "cancelled" else "failed"
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
    for nxt in policy.next_work:
        _enqueue_next(session, claimed, nxt, now)
