"""GET /snapshot — one additive JSON the dashboard polls. Every query filters cohort_id."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from order_pipeline.api.schemas import (
    Conservation,
    E2eLatency,
    OrderTrace,
    SnapshotResponse,
    TerminalRates,
    TraceAttempt,
    TraceEvent,
)
from order_pipeline.models import Attempt, Order, OrderEvent, WorkItem

CAUSE_INVALID = "invalid_transition"

# Assignment names. Code stores being_prepared / out_for_delivery; JSON uses these keys.
STAGE_NAMES = (
    "placed",
    "confirmed",
    "being prepared",
    "ready",
    "out for delivery",
    "delivered",
)

STATE_TO_STAGE = {
    "placed": "placed",
    "confirmed": "confirmed",
    "being_prepared": "being prepared",
    "ready": "ready",
    "out_for_delivery": "out for delivery",
    "delivered": "delivered",
}

TERMINAL_STATES = frozenset({"delivered", "cancelled", "failed"})
RATE_WINDOW = timedelta(seconds=60)


def order_id_from_ledger_key(key: str) -> UUID | None:
    """Parse `({order_id}, confirm)` / `({order_id}, dispatch)` sim ledger keys."""
    if not (key.startswith("(") and key.endswith(")")):
        return None
    head, sep, _tail = key[1:-1].partition(", ")
    if not sep:
        return None
    try:
        return UUID(head)
    except ValueError:
        return None


def duplicate_effects_from_ledgers(
    ledger_counts: Sequence[Mapping[str, int]],
    cohort_order_ids: set[UUID],
) -> int:
    """Extra sim-ledger rows per key for this cohort. Not a Postgres count."""
    extra = 0
    for counts in ledger_counts:
        for key, n in counts.items():
            order_id = order_id_from_ledger_key(key)
            if order_id is None or order_id not in cohort_order_ids:
                continue
            extra += max(0, int(n) - 1)
    return extra


def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear interpolation of the p-th percentile. None when there are no samples."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (p / 100.0) * (len(ordered) - 1)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return ordered[lo]
    weight = k - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def fetch_ledger_counts(base_url: str, *, timeout_s: float = 2.0) -> dict[str, int]:
    """GET /admin/ledger from one sim. Unreachable sims contribute no effects."""
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/admin/ledger", timeout=timeout_s)
        response.raise_for_status()
    except httpx.HTTPError:
        return {}
    body = response.json()
    if not isinstance(body, dict):
        return {}
    raw = body.get("counts")
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int) and not isinstance(value, bool):
            counts[str(key)] = value
    return counts


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _empty_stages() -> dict[str, int]:
    return {name: 0 for name in STAGE_NAMES}


def build_snapshot(
    session: Session,
    *,
    cohort_id: UUID,
    now: datetime,
    ledger_counts: Sequence[Mapping[str, int]],
    order_id: UUID | None = None,
) -> SnapshotResponse:
    """Assemble the snapshot from Postgres + already-fetched sim ledgers. No HTTP here."""
    orders = list(session.scalars(select(Order).where(Order.cohort_id == cohort_id)))
    cohort_order_ids = {order.id for order in orders}

    events: list[OrderEvent] = []
    work_items: list[WorkItem] = []
    attempts: list[Attempt] = []
    if cohort_order_ids:
        events = list(
            session.scalars(select(OrderEvent).where(OrderEvent.order_id.in_(cohort_order_ids)))
        )
        work_items = list(
            session.scalars(select(WorkItem).where(WorkItem.order_id.in_(cohort_order_ids)))
        )
        work_ids = [item.id for item in work_items]
        if work_ids:
            attempts = list(
                session.scalars(select(Attempt).where(Attempt.work_item_id.in_(work_ids)))
            )

    stages = _empty_stages()
    delivered = cancelled = failed = 0
    for order in orders:
        stage = STATE_TO_STAGE.get(order.state)
        if stage is not None:
            stages[stage] += 1
        if order.state == "delivered":
            delivered += 1
        elif order.state == "cancelled":
            cancelled += 1
        elif order.state == "failed":
            failed += 1

    accepted = len(orders)
    in_flight = accepted - delivered - cancelled - failed
    parked_order_ids = {item.order_id for item in work_items if item.status == "parked"}
    in_flight_ids = {order.id for order in orders if order.state not in TERMINAL_STATES}
    parked_outside = len(parked_order_ids - in_flight_ids)
    residual = accepted - delivered - cancelled - failed - in_flight + parked_outside

    window_start = _aware(now) - RATE_WINDOW
    rate_delivered = rate_cancelled = rate_failed = 0
    delivered_at: dict[UUID, datetime] = {}
    last_applied: dict[UUID, OrderEvent] = {}
    invalid_transitions = 0
    for event in events:
        if event.cause == CAUSE_INVALID:
            invalid_transitions += 1
        if not event.applied:
            continue
        stamp = _aware(event.timestamp)
        previous = last_applied.get(event.order_id)
        if previous is None or (stamp, event.id) > (_aware(previous.timestamp), previous.id):
            last_applied[event.order_id] = event
        if event.to_state == "delivered":
            current = delivered_at.get(event.order_id)
            if current is None or stamp > _aware(current):
                delivered_at[event.order_id] = event.timestamp
        if stamp >= window_start:
            if event.to_state == "delivered":
                rate_delivered += 1
            elif event.to_state == "cancelled":
                rate_cancelled += 1
            elif event.to_state == "failed":
                rate_failed += 1

    mismatches = 0
    orders_with_work = {item.order_id for item in work_items}
    for order in orders:
        last = last_applied.get(order.id)
        if last is None or last.to_state != order.state:
            mismatches += 1

    attempts_by_work: dict[UUID, int] = {}
    for attempt in attempts:
        attempts_by_work[attempt.work_item_id] = attempts_by_work.get(attempt.work_item_id, 0) + 1
    duplicate_attempts = sum(max(0, count - 1) for count in attempts_by_work.values())

    at = _aware(now)
    currently_leased = 0
    for item in work_items:
        if item.status != "leased" or item.lease_until is None:
            continue
        if _aware(item.lease_until) > at:
            currently_leased += 1

    latencies: list[float] = []
    for order in orders:
        if order.state != "delivered":
            continue
        finished = delivered_at.get(order.id)
        if finished is None:
            continue
        latencies.append((_aware(finished) - _aware(order.accepted_at)).total_seconds())

    work_by_id = {item.id: item for item in work_items}
    trace: OrderTrace | None = None
    if order_id is not None:
        trace = _trace(
            order_id=order_id,
            in_cohort=order_id in cohort_order_ids,
            events=events,
            attempts=attempts,
            work_by_id=work_by_id,
        )

    return SnapshotResponse(
        cohort_id=cohort_id,
        stages=stages,
        terminal_rates_per_min=TerminalRates(
            delivered=float(rate_delivered),
            cancelled=float(rate_cancelled),
            failed=float(rate_failed),
        ),
        e2e_latency_s=E2eLatency(p50=percentile(latencies, 50), p95=percentile(latencies, 95)),
        conservation=Conservation(
            accepted=accepted,
            delivered=delivered,
            cancelled=cancelled,
            failed=failed,
            in_flight=in_flight,
            parked=len(parked_order_ids),
            residual=residual,
        ),
        duplicate_attempts=duplicate_attempts,
        duplicate_effects=duplicate_effects_from_ledgers(ledger_counts, cohort_order_ids),
        startup_scan=sum(1 for order in orders if order.id not in orders_with_work),
        invalid_transitions=invalid_transitions,
        state_vs_last_order_events_mismatches=mismatches,
        currently_leased=currently_leased,
        trace=trace,
    )


def _trace(
    *,
    order_id: UUID,
    in_cohort: bool,
    events: Sequence[OrderEvent],
    attempts: Sequence[Attempt],
    work_by_id: Mapping[UUID, WorkItem],
) -> OrderTrace:
    if not in_cohort:
        return OrderTrace(order_id=order_id, order_events=[], attempts=[])
    order_events = sorted(
        (event for event in events if event.order_id == order_id),
        key=lambda event: (_aware(event.timestamp), event.id),
    )
    order_work_ids = {item_id for item_id, item in work_by_id.items() if item.order_id == order_id}
    order_attempts = sorted(
        (attempt for attempt in attempts if attempt.work_item_id in order_work_ids),
        key=lambda attempt: (_aware(attempt.started_at), attempt.id),
    )
    return OrderTrace(
        order_id=order_id,
        order_events=[
            TraceEvent(
                id=event.id,
                from_state=event.from_state,
                to_state=event.to_state,
                actor=event.actor,
                cause=event.cause,
                timestamp=event.timestamp,
                applied=event.applied,
            )
            for event in order_events
        ],
        attempts=[
            TraceAttempt(
                id=attempt.id,
                work_item_id=attempt.work_item_id,
                work_type=work_by_id[attempt.work_item_id].work_type,
                started_at=attempt.started_at,
                ended_at=attempt.ended_at,
                lease_owner=attempt.lease_owner,
                outcome=attempt.outcome,
            )
            for attempt in order_attempts
        ],
    )
