"""Compose: GET /snapshot after a delivered walk. Mix stays off."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.api.snapshot import STAGE_NAMES, duplicate_effects_from_ledgers
from order_pipeline.intake import DEFAULT_COHORT_ID, confirm_idempotency_key
from order_pipeline.models import Attempt, Order, OrderEvent, WorkItem
from order_pipeline.worker.dispatch import dispatch_idempotency_key
from tests.conftest import hold_unclaimable
from tests.sim_admin import mix_off, mix_on

API_URL = "http://localhost:8000"
RSIM_URL = "http://localhost:8081"
CSIM_URL = "http://localhost:8082"
WALK_TIMEOUT_S = 180.0
POLL_EVERY_S = 0.05
CONFIRM_TIMEOUT_S = 40.0
REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FIELDS = (
    "cohort_id",
    "stages",
    "terminal_rates_per_min",
    "e2e_latency_s",
    "conservation",
    "duplicate_attempts",
    "duplicate_effects",
    "startup_scan",
    "invalid_transitions",
    "state_vs_last_order_events_mismatches",
    "currently_leased",
    "currently_leased_items",
    "trace",
    "accept_reject",
    "backlog",
    "retry_rate",
    "oldest_open",
    "oldest_unparked",
    "http_429s",
    "stretching_etas",
    "parked_list",
    "sim_http",
    "outbound_slots",
    "no_progress_beyond_threshold",
    "orphaned_tickets",
)


def _http(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> httpx.Response:
    try:
        return httpx.request(method, url, json=json, headers=headers, timeout=timeout)
    except httpx.RequestError as exc:
        pytest.fail(f"request failed {method} {url}: {exc}")


def _snapshot(
    *,
    cohort_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    params: dict[str, str] = {}
    if cohort_id is not None:
        params["cohort_id"] = str(cohort_id)
    if order_id is not None:
        params["order_id"] = str(order_id)
    try:
        response = httpx.get(f"{API_URL}/snapshot", params=params or None, timeout=10.0)
    except httpx.RequestError as exc:
        pytest.fail(f"GET /snapshot failed: {exc}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _place_chips(*, cohort_id: uuid.UUID | None = None, prefix: str = "snap") -> uuid.UUID:
    payload: dict[str, Any] = {"items": ["chips"]}
    if cohort_id is not None:
        payload["cohort_id"] = str(cohort_id)
    posted = _http(
        "POST",
        f"{API_URL}/orders",
        json=payload,
        headers={"Idempotency-Key": f"{prefix}-{uuid.uuid4()}"},
    )
    assert posted.status_code == 201, posted.text
    return uuid.UUID(posted.json()["id"])


def _wait_state(order_id: uuid.UUID, wanted: frozenset[str], *, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    last = "placed"
    while time.monotonic() < deadline:
        got = _http("GET", f"{API_URL}/orders/{order_id}")
        assert got.status_code == 200, got.text
        last = got.json()["state"]
        assert isinstance(last, str)
        if last in wanted:
            return last
        if last in {"failed", "cancelled"}:
            pytest.fail(f"order {order_id} left the path early: {last}")
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"order {order_id} did not reach {wanted} within {timeout_s}s; last={last}")


def _ledger(url: str) -> dict[str, int]:
    response = _http("GET", f"{url}/admin/ledger")
    assert response.status_code == 200, response.text
    counts = response.json()["counts"]
    assert isinstance(counts, dict)
    return {str(key): int(value) for key, value in counts.items()}


def _assert_lite_fields(body: dict[str, Any]) -> None:
    for name in REQUIRED_FIELDS:
        assert name in body, name
    stages = body["stages"]
    assert isinstance(stages, dict)
    assert set(stages) == set(STAGE_NAMES)
    conservation = body["conservation"]
    assert conservation["residual"] == 0
    assert conservation["parked"] <= conservation["in_flight"]
    assert conservation["accepted"] == (
        conservation["delivered"]
        + conservation["cancelled"]
        + conservation["failed"]
        + conservation["in_flight"]
    )
    assert body["startup_scan"] == 0
    rates = body["terminal_rates_per_min"]
    assert set(rates) == {"delivered", "cancelled", "failed"}
    e2e = body["e2e_latency_s"]
    assert "p50" in e2e and "p95" in e2e
    for lane in ("restaurant", "courier"):
        assert {"timeout", "http_5xx", "http_429"} <= set(body["sim_http"][lane])
    slots = body["outbound_slots"]
    assert slots["worker_replicas"] == 2
    assert slots["restaurant"]["cap"] == 16
    assert slots["courier"]["cap"] == 16
    assert slots["task"]["cap"] == 48


def test_snapshot_walk_shows_every_named_stage(
    session_factory: sessionmaker[Session],
) -> None:
    mix_off()
    cohort_id = uuid.uuid4()
    order_id = _place_chips(cohort_id=cohort_id, prefix="snap-walk")
    _wait_state(order_id, frozenset({"delivered"}), timeout_s=WALK_TIMEOUT_S)

    body = _snapshot(cohort_id=cohort_id, order_id=order_id)
    _assert_lite_fields(body)
    assert body["cohort_id"] == str(cohort_id)
    assert body["stages"]["delivered"] >= 1
    assert body["conservation"]["accepted"] == 1
    assert body["conservation"]["delivered"] == 1
    assert body["conservation"]["in_flight"] == 0
    assert body["invalid_transitions"] == 0
    assert body["state_vs_last_order_events_mismatches"] == 0
    assert body["e2e_latency_s"]["p50"] is not None
    assert body["e2e_latency_s"]["p95"] is not None

    restaurant = _ledger(RSIM_URL)
    courier = _ledger(CSIM_URL)
    extras = duplicate_effects_from_ledgers([restaurant, courier], {order_id})
    assert body["duplicate_effects"] == extras == 0
    assert restaurant[confirm_idempotency_key(order_id)] == 1
    assert courier[dispatch_idempotency_key(order_id)] == 1

    with session_factory() as session:
        result_rows = session.scalar(
            select(func.count())
            .select_from(WorkItem)
            .where(WorkItem.order_id == order_id, WorkItem.result.is_not(None))
        )
    assert result_rows is not None and result_rows >= 1
    assert body["duplicate_effects"] != result_rows

    faults_r = _http("GET", f"{RSIM_URL}/admin/faults")
    faults_c = _http("GET", f"{CSIM_URL}/admin/faults")
    assert faults_r.json()["mix"] == "off"
    assert faults_c.json()["mix"] == "off"

    trace = body["trace"]
    assert isinstance(trace, dict)
    assert trace["order_id"] == str(order_id)
    applied_confirmed = [
        event
        for event in trace["order_events"]
        if event["to_state"] == "confirmed" and event["applied"]
    ]
    assert len(applied_confirmed) == 1
    assert any(
        event["to_state"] == "delivered" and event["applied"] for event in trace["order_events"]
    )

    with session_factory.begin() as session:
        item = session.scalars(
            select(WorkItem).where(WorkItem.order_id == order_id, WorkItem.work_type == "confirm")
        ).one()
        abandoned_at = datetime.now(UTC)
        session.add_all(
            [
                Attempt(
                    work_item_id=item.id,
                    started_at=abandoned_at,
                    ended_at=None,
                    lease_owner="abandoned-lease",
                    outcome=None,
                ),
                Attempt(
                    work_item_id=item.id,
                    started_at=abandoned_at + timedelta(microseconds=1),
                    ended_at=abandoned_at + timedelta(microseconds=2),
                    lease_owner="reclaimer",
                    outcome="ok",
                ),
            ]
        )

    traced = _snapshot(cohort_id=cohort_id, order_id=order_id)
    nulls = [row for row in traced["trace"]["attempts"] if row["outcome"] is None]
    assert nulls, traced["trace"]["attempts"]
    assert any(row["ended_at"] is None for row in nulls)
    assert traced["duplicate_attempts"] >= 1
    still_one = [
        event
        for event in traced["trace"]["order_events"]
        if event["to_state"] == "confirmed" and event["applied"]
    ]
    assert len(still_one) == 1


def test_snapshot_trace_retried_confirm_no_second_event(
    session_factory: sessionmaker[Session],
) -> None:
    mix_off()
    armed = _http("POST", f"{RSIM_URL}/admin/faults", json={"mode": "5xx_after", "mix": "off"})
    assert armed.status_code == 200, armed.text
    try:
        cohort_id = uuid.uuid4()
        order_id = _place_chips(cohort_id=cohort_id, prefix="snap-retry")
        confirm_key = confirm_idempotency_key(order_id)
        _wait_state(
            order_id,
            frozenset({"confirmed", "being_prepared", "ready", "out_for_delivery", "delivered"}),
            timeout_s=CONFIRM_TIMEOUT_S,
        )
        body = _snapshot(cohort_id=cohort_id, order_id=order_id)
        trace = body["trace"]
        assert isinstance(trace, dict)
        applied_confirmed = [
            event
            for event in trace["order_events"]
            if event["to_state"] == "confirmed" and event["applied"]
        ]
        assert len(applied_confirmed) == 1
        confirm_attempts = [row for row in trace["attempts"] if row["work_type"] == "confirm"]
        assert len(confirm_attempts) >= 2
        assert any(row["outcome"] == "http_5xx" for row in confirm_attempts)
        with session_factory() as session:
            events = list(
                session.scalars(
                    select(OrderEvent).where(
                        OrderEvent.order_id == order_id,
                        OrderEvent.to_state == "confirmed",
                        OrderEvent.applied.is_(True),
                    )
                )
            )
        assert len(events) == 1
        assert _ledger(RSIM_URL)[confirm_key] == 1
        extras = duplicate_effects_from_ledgers([_ledger(RSIM_URL)], {order_id})
        assert body["duplicate_effects"] == extras
    finally:
        mix_on()


def test_snapshot_default_cohort_query_param_omitted() -> None:
    body = _snapshot()
    assert body["cohort_id"] == str(DEFAULT_COHORT_ID)
    for name in REQUIRED_FIELDS:
        assert name in body, name
    assert set(body["stages"]) == set(STAGE_NAMES)
    assert body["trace"] is None


def test_compose_snapshot_wiring() -> None:
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "API_RESTAURANT_ADMIN_URL: http://restaurant:8081" in compose
    assert "API_COURIER_ADMIN_URL: http://courier:8082" in compose
    assert "API_WORKER_REPLICAS: ${ORDER_PIPELINE_WORKER_REPLICAS:-2}" in compose
    assert "replicas: ${ORDER_PIPELINE_WORKER_REPLICAS:-2}" in compose
    assert compose.count("${ORDER_PIPELINE_DEP_CAP_RSIM:-8}") == 2
    assert compose.count("${ORDER_PIPELINE_DEP_CAP_CSIM:-8}") == 2
    assert compose.count("${ORDER_PIPELINE_TASK_CAPACITY:-24}") == 2
    assert compose.count("${ORDER_PIPELINE_CONFIRM_DEADLINE_S:-120}") == 2


def test_snapshot_isolation_zero_false_mismatches_under_load(
    session_factory: sessionmaker[Session],
) -> None:
    """After REPEATABLE READ, event mismatches stay 0 across >=500 loaded polls.

    Pre-fix baseline: ~1% false state_vs_last_order_events_mismatches (7/673)
    at ~3 orders/s and 50ms poll. Conservation residual is a partition of one
    orders SELECT (in_flight = not terminal) and is not lost-order evidence.
    """
    mix_on()
    cohort_id = uuid.uuid4()
    stop = threading.Event()
    errors: list[str] = []

    def produce() -> None:
        while not stop.is_set():
            try:
                _place_chips(cohort_id=cohort_id, prefix="iso-load")
            except Exception as exc:
                errors.append(str(exc))
            time.sleep(1.0 / 3.0)

    worker = threading.Thread(target=produce, daemon=True)
    worker.start()
    samples: list[int] = []
    stage_vectors: list[tuple[tuple[str, int], ...]] = []
    try:
        deadline = time.monotonic() + 95.0
        while len(samples) < 500 and time.monotonic() < deadline:
            started = time.monotonic()
            body = _snapshot(cohort_id=cohort_id)
            samples.append(int(body["state_vs_last_order_events_mismatches"]))
            stage_vectors.append(tuple(sorted(body["stages"].items())))
            pause = 0.05 - (time.monotonic() - started)
            if pause > 0:
                time.sleep(pause)
    finally:
        stop.set()
        worker.join(timeout=5.0)

    assert not errors, errors[:3]
    assert len(samples) >= 500, len(samples)
    # A quiet cohort cannot flash a false mismatch, so zero mismatches across a
    # static pipeline proves nothing. Require the sampled window to actually
    # contain committed transitions, or this guard silently disarms itself.
    churn = sum(1 for a, b in zip(stage_vectors, stage_vectors[1:]) if a != b)
    assert churn >= 50, (
        f"pipeline too quiet to prove anything: {churn} stage changes across {len(samples)} polls"
    )
    mismatched = sum(1 for value in samples if value)
    assert mismatched == 0, (
        f"{mismatched}/{len(samples)} polls flashed state_vs_last_order_events_mismatches "
        f"(max={max(samples)})"
    )
    last = _snapshot(cohort_id=cohort_id)
    assert last["startup_scan"] == 0
    # Ledger unavailable must stay unknown; compose ledgers are reachable here.
    assert last["duplicate_effects"] == 0
    with session_factory.begin() as session:
        leftover = list(session.scalars(select(Order.id).where(Order.cohort_id == cohort_id)))
        hold_unclaimable(session, *leftover)
