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
    AcceptReject,
    Conservation,
    E2eLatency,
    Http429s,
    NoProgress,
    OldestOpen,
    OrderTrace,
    OutboundSlots,
    ParkedRow,
    SimHttp,
    SimHttpLane,
    SlotUse,
    SnapshotResponse,
    StretchingEtas,
    TerminalRates,
    TraceAttempt,
    TraceEvent,
)
from order_pipeline.lifecycle import CAUSE_INVALID
from order_pipeline.models import Attempt, Order, OrderEvent, WorkItem

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
NO_PROGRESS_THRESHOLD_S = 90.0
BACKLOG_TYPES = ("confirm", "poll_cook", "dispatch", "poll_ride")
BACKLOG_STATUSES = frozenset({"pending", "leased"})
KITCHEN_WORK = frozenset({"confirm", "poll_cook", "submit"})
COURIER_WORK = frozenset({"dispatch", "poll_ride"})
UNKNOWN_TIMEOUT_OUTCOMES = frozenset({"timeout", "dropped", "unknown"})


def retry_attempt_ids(attempts: Sequence[Attempt]) -> set[UUID]:
    """Return attempts that follow a failed, unknown, or abandoned call.

    Successful not-ready polls schedule another observation of the dependency;
    they are normal polling, not retries. A later attempt is retry traffic only
    when the preceding call on that work item did not complete successfully.
    """
    by_work: dict[UUID, list[Attempt]] = {}
    for attempt in attempts:
        by_work.setdefault(attempt.work_item_id, []).append(attempt)

    retry_ids: set[UUID] = set()
    for rows in by_work.values():
        ordered = sorted(rows, key=lambda row: (_aware(row.started_at), row.id))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if previous.outcome != "ok":
                retry_ids.add(current.id)
    return retry_ids


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
    """Extra sim-ledger rows per order in this cohort, per ledger.

    The ledger primary key is the exact idempotency key, so counting
    ``n - 1`` per key can never see two tickets under two retry keys.
    Group by order_id inside each sim instead. One confirm and one
    dispatch in *different* ledgers is still zero extras.
    """
    extra = 0
    for counts in ledger_counts:
        per_order: dict[UUID, int] = {}
        for key, n in counts.items():
            order_id = order_id_from_ledger_key(key)
            if order_id is None or order_id not in cohort_order_ids:
                continue
            per_order[order_id] = per_order.get(order_id, 0) + int(n)
        extra += sum(max(0, total - 1) for total in per_order.values())
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


def fetch_ledger_counts(base_url: str, *, timeout_s: float = 2.0) -> tuple[dict[str, int], bool]:
    """GET /admin/ledger from one sim. ``ok`` is False when the sim is unreachable.

    Callers must not treat a failed fetch as duplicate_effects = 0.
    """
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/admin/ledger", timeout=timeout_s)
        response.raise_for_status()
    except httpx.HTTPError:
        return {}, False
    body = response.json()
    if not isinstance(body, dict):
        return {}, False
    raw = body.get("counts")
    if not isinstance(raw, dict):
        return {}, False
    counts: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, int) and not isinstance(value, bool):
            counts[str(key)] = value
    return counts, True


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _empty_stages() -> dict[str, int]:
    return {name: 0 for name in STAGE_NAMES}


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed)


def _time_from_blob(blob: object, field: str) -> datetime | None:
    if not isinstance(blob, dict):
        return None
    return _parse_iso(blob.get(field))


def _rail_wait_s(item: WorkItem) -> float | None:
    for blob in (item.result, item.payload):
        accepted = _time_from_blob(blob, "accepted_at")
        started = _time_from_blob(blob, "service_started_at")
        if accepted is not None and started is not None:
            return max(0.0, (started - accepted).total_seconds())
    return None


def _empty_backlog() -> dict[str, int]:
    return {name: 0 for name in BACKLOG_TYPES}


def _sim_lane(
    attempts: Sequence[Attempt],
    work_by_id: Mapping[UUID, WorkItem],
    work_types: frozenset[str],
    window_start: datetime,
) -> SimHttpLane:
    lane_rows = [
        attempt
        for attempt in attempts
        if (item := work_by_id.get(attempt.work_item_id)) is not None
        and item.work_type in work_types
    ]
    window_rows = [row for row in lane_rows if _aware(row.started_at) >= window_start]
    latencies: list[float] = []
    for attempt in window_rows:
        if attempt.ended_at is None:
            continue
        latencies.append((_aware(attempt.ended_at) - _aware(attempt.started_at)).total_seconds())
    return SimHttpLane(
        requests_per_min=float(len(window_rows)),
        latency_p50_s=percentile(latencies, 50),
        latency_p95_s=percentile(latencies, 95),
        # Timeout, a deliberately dropped response, and an unclassified
        # transport failure all have the same retry meaning: outcome unknown.
        timeout=sum(1 for row in window_rows if row.outcome in UNKNOWN_TIMEOUT_OUTCOMES),
        http_5xx=sum(1 for row in window_rows if row.outcome == "http_5xx"),
        http_429=sum(1 for row in window_rows if row.outcome == "http_429"),
    )


def build_snapshot(
    session: Session,
    *,
    cohort_id: UUID,
    now: datetime,
    ledger_counts: Sequence[Mapping[str, int]],
    order_id: UUID | None = None,
    ledgers_ok: bool = True,
    door_429s: int = 0,
    worker_replicas: int = 2,
    worker_dep_cap_rsim: int = 8,
    worker_dep_cap_csim: int = 8,
    worker_task_capacity: int = 24,
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
    parked_order_ids = {item.order_id for item in work_items if item.status == "parked"}
    in_flight_ids = {order.id for order in orders if order.state not in TERMINAL_STATES}
    in_flight = len(in_flight_ids)
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

    retry_ids = retry_attempt_ids(attempts)
    duplicate_attempts = len(retry_ids)

    at = _aware(now)
    currently_leased = 0
    restaurant_slots_used = 0
    courier_slots_used = 0
    for item in work_items:
        if item.status != "leased" or item.lease_until is None:
            continue
        if _aware(item.lease_until) > at:
            currently_leased += 1
            if item.work_type in KITCHEN_WORK:
                restaurant_slots_used += 1
            elif item.work_type in COURIER_WORK:
                courier_slots_used += 1

    latencies: list[float] = []
    for order in orders:
        if order.state != "delivered":
            continue
        finished = delivered_at.get(order.id)
        if finished is None:
            continue
        latencies.append((_aware(finished) - _aware(order.accepted_at)).total_seconds())

    work_by_id = {item.id: item for item in work_items}
    orders_by_id = {order.id: order for order in orders}
    trace: OrderTrace | None = None
    if order_id is not None:
        trace = _trace(
            order_id=order_id,
            in_cohort=order_id in cohort_order_ids,
            events=events,
            attempts=attempts,
            work_by_id=work_by_id,
        )

    backlog = _empty_backlog()
    for item in work_items:
        if item.status not in BACKLOG_STATUSES or item.work_type not in backlog:
            continue
        backlog[item.work_type] += 1

    window_attempts = [row for row in attempts if _aware(row.started_at) >= window_start]
    retries = sum(1 for row in window_attempts if row.id in retry_ids)
    retry_rate = (retries / len(window_attempts)) if window_attempts else 0.0

    oldest_order: Order | None = None
    for order in orders:
        if order.state in TERMINAL_STATES:
            continue
        if oldest_order is None or _aware(order.accepted_at) < _aware(oldest_order.accepted_at):
            oldest_order = order
    oldest_open = OldestOpen(
        age_s=(
            (at - _aware(oldest_order.accepted_at)).total_seconds()
            if oldest_order is not None
            else None
        ),
        stage=(STATE_TO_STAGE.get(oldest_order.state) if oldest_order is not None else None),
    )

    kitchen_429s = courier_429s = 0
    for attempt in attempts:
        if attempt.outcome != "http_429":
            continue
        work_item = work_by_id.get(attempt.work_item_id)
        if work_item is None:
            continue
        work_type = work_item.work_type
        if work_type in KITCHEN_WORK:
            kitchen_429s += 1
        elif work_type in COURIER_WORK:
            courier_429s += 1

    stretch_by_order: dict[UUID, float] = {}
    for item in work_items:
        live = orders_by_id.get(item.order_id)
        if live is None or live.state in TERMINAL_STATES:
            continue
        if live.state in {"placed", "confirmed", "being_prepared"}:
            relevant_types = KITCHEN_WORK
        elif live.state == "out_for_delivery":
            relevant_types = COURIER_WORK
        else:
            continue
        if item.work_type not in relevant_types:
            continue
        stretch = _rail_wait_s(item)
        if stretch is None:
            continue
        prev_stretch = stretch_by_order.get(live.id)
        if prev_stretch is None or stretch > prev_stretch:
            stretch_by_order[live.id] = stretch
    stretching = [value for value in stretch_by_order.values() if value > 0]
    stretching_etas = StretchingEtas(
        count=len(stretching),
        max_stretch_s=max(stretching) if stretching else None,
    )

    parked_list = [
        ParkedRow(
            order_id=item.order_id,
            work_type=item.work_type,
            owner=item.park_owner,
            reason=item.park_reason,
            next_action=item.park_next_action,
        )
        for item in work_items
        if item.status == "parked"
    ]

    stalled = 0
    for order in orders:
        if order.state in TERMINAL_STATES:
            continue
        last = last_applied.get(order.id)
        stamp = _aware(last.timestamp) if last is not None else _aware(order.accepted_at)
        if (at - stamp).total_seconds() > NO_PROGRESS_THRESHOLD_S:
            stalled += 1

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
        duplicate_effects=(
            duplicate_effects_from_ledgers(ledger_counts, cohort_order_ids) if ledgers_ok else None
        ),
        startup_scan=sum(1 for order in orders if order.id not in orders_with_work),
        invalid_transitions=invalid_transitions,
        state_vs_last_order_events_mismatches=mismatches,
        currently_leased=currently_leased,
        trace=trace,
        accept_reject=AcceptReject(accepted=accepted, rejected=door_429s),
        backlog=backlog,
        retry_rate=retry_rate,
        oldest_open=oldest_open,
        http_429s=Http429s(door=door_429s, kitchen=kitchen_429s, courier=courier_429s),
        stretching_etas=stretching_etas,
        parked_list=parked_list,
        sim_http=SimHttp(
            restaurant=_sim_lane(attempts, work_by_id, KITCHEN_WORK, window_start),
            courier=_sim_lane(attempts, work_by_id, COURIER_WORK, window_start),
        ),
        outbound_slots=OutboundSlots(
            worker_replicas=worker_replicas,
            restaurant=SlotUse(
                used=restaurant_slots_used,
                cap=worker_dep_cap_rsim * worker_replicas,
                per_worker_cap=worker_dep_cap_rsim,
            ),
            courier=SlotUse(
                used=courier_slots_used,
                cap=worker_dep_cap_csim * worker_replicas,
                per_worker_cap=worker_dep_cap_csim,
            ),
            task=SlotUse(
                used=currently_leased,
                cap=worker_task_capacity * worker_replicas,
                per_worker_cap=worker_task_capacity,
            ),
        ),
        no_progress_beyond_threshold=NoProgress(
            threshold_s=NO_PROGRESS_THRESHOLD_S,
            count=stalled,
        ),
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
