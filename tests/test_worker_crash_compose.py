"""Scenario 3, in runbook order: kill/resume, then park/clear/Redrive."""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from order_pipeline.intake import confirm_idempotency_key
from order_pipeline.worker.dispatch import dispatch_idempotency_key
from tests.sim_admin import mix_off, mix_on, post_sim_faults

API_URL = "http://localhost:8000"
DASHBOARD_URL = "http://127.0.0.1:5173"
RSIM_URL = "http://localhost:8081"
CSIM_URL = "http://localhost:8082"
LOADGEN_URL = "http://localhost:8090"
REPO_ROOT = Path(__file__).resolve().parents[1]
POLL_S = 0.1
LEASE_RESUME_TIMEOUT_S = 30.0
ORDER_TIMEOUT_S = 180.0
LEASE_LIFECYCLE_CAUSES = frozenset(
    {
        "lease",
        "lease_acquired",
        "lease_dropped",
        "lease_expired",
        "lease_taken",
        "lease_released",
        "reclaim",
    }
)


def _http(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> httpx.Response:
    try:
        return httpx.request(
            method,
            url,
            json=json,
            headers=headers,
            params=params,
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"request failed {method} {url}: {exc}")


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("docker", "compose", *args),
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _place(*, cohort_id: uuid.UUID, prefix: str) -> uuid.UUID:
    response = _http(
        "POST",
        f"{API_URL}/orders",
        json={"items": ["chips"], "cohort_id": str(cohort_id)},
        headers={"Idempotency-Key": f"{prefix}-{uuid.uuid4()}"},
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


def _snapshot(*, cohort_id: uuid.UUID, order_id: uuid.UUID) -> dict[str, Any]:
    response = _http(
        "GET",
        f"{API_URL}/snapshot",
        params={"cohort_id": str(cohort_id), "order_id": str(order_id)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _wait_order(order_id: uuid.UUID, wanted: set[str], *, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    last = "unknown"
    while time.monotonic() < deadline:
        response = _http("GET", f"{API_URL}/orders/{order_id}")
        assert response.status_code == 200, response.text
        last = response.json()["state"]
        assert isinstance(last, str)
        if last in wanted:
            return last
        if last in {"failed", "cancelled"}:
            pytest.fail(f"order {order_id} ended unexpectedly at {last}")
        time.sleep(POLL_S)
    pytest.fail(f"order {order_id} did not reach {wanted}; last={last}")


def _ledger_count(base_url: str, key: str) -> int:
    response = _http("GET", f"{base_url}/admin/ledger")
    assert response.status_code == 200, response.text
    counts = response.json()["counts"]
    assert isinstance(counts, dict)
    return int(counts.get(key, 0))


def _container_owns(container_id: str, owner: str | None) -> bool:
    return owner is not None and container_id.startswith(owner.partition(":")[0])


def _wait_parked_dispatch(
    *,
    cohort_id: uuid.UUID,
    order_id: uuid.UUID,
    work_item_id: str | None = None,
    timeout_s: float = 35.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = _snapshot(cohort_id=cohort_id, order_id=order_id)
        parked_list = body["parked_list"]
        assert isinstance(parked_list, list)
        parked_row = next(
            (
                row
                for row in parked_list
                if isinstance(row, dict)
                and row.get("order_id") == str(order_id)
                and row.get("work_type") == "dispatch"
                and (work_item_id is None or row.get("id") == work_item_id)
            ),
            None,
        )
        if parked_row is not None:
            return cast(dict[str, Any], parked_row)
        time.sleep(POLL_S)
    pytest.fail(f"dispatch for {order_id} did not park within {timeout_s}s")


def test_scenario_3_kill_resume_then_park_clear_redrive() -> None:
    crash_cohort = uuid.uuid4()
    park_cohort = uuid.uuid4()
    killed = False
    try:
        stopped = _http("POST", f"{LOADGEN_URL}/stop")
        assert stopped.status_code == 200, stopped.text
        mix_off()

        # Use the same public instrument and Docker command as the live runbook:
        # Watch identifies the worker that owns this order's lease, then that exact
        # container is killed. No direct DB lookup chooses the victim.
        worker_ids = [value for value in _docker("ps", "-q", "worker").stdout.splitlines() if value]
        assert len(worker_ids) == 2
        post_sim_faults(RSIM_URL, {"mode": "blackout", "seconds": 30, "mix": "off"})
        crash_order_id = _place(cohort_id=crash_cohort, prefix="scenario3-crash")
        crash_item_id: uuid.UUID | None = None
        crash_owner: str | None = None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            body = _snapshot(cohort_id=crash_cohort, order_id=crash_order_id)
            leased_rows = body["currently_leased_items"]
            assert body["currently_leased"] == len(leased_rows)
            selected = next(
                (
                    row
                    for row in leased_rows
                    if row["work_type"] == "confirm" and row["order_id"] == str(crash_order_id)
                ),
                None,
            )
            if selected is not None:
                crash_item_id = uuid.UUID(selected["id"])
                crash_owner = selected["owner"]
                break
            time.sleep(POLL_S)
        assert crash_item_id is not None
        assert crash_owner is not None
        killed_container = next(
            (
                container_id
                for container_id in worker_ids
                if _container_owns(container_id, crash_owner)
            ),
            None,
        )
        assert killed_container is not None
        stored_confirm_key = confirm_idempotency_key(crash_order_id)

        subprocess.run(
            ("docker", "kill", killed_container),
            check=True,
            capture_output=True,
            text=True,
        )
        killed = True
        post_sim_faults(RSIM_URL, {"mode": "clear", "mix": "off"})

        # Do not restart yet: the survivor must reclaim after the <=15s lease gap.
        # Prove the gap + same-key resume from the public snapshot, not Postgres.
        resumed = False
        deadline = time.monotonic() + LEASE_RESUME_TIMEOUT_S
        while time.monotonic() < deadline:
            body = _snapshot(cohort_id=crash_cohort, order_id=crash_order_id)
            trace = body.get("trace")
            attempts = trace["attempts"] if isinstance(trace, dict) else []
            confirm_attempts = [row for row in attempts if row["work_type"] == "confirm"]
            abandoned = [
                row
                for row in confirm_attempts
                if row["lease_owner"] == crash_owner
                and row["outcome"] is None
                and row["ended_at"] is None
                and row["work_item_id"] == str(crash_item_id)
                and row["idempotency_key"] == stored_confirm_key
            ]
            survivor = [
                row
                for row in confirm_attempts
                if row["lease_owner"] != crash_owner
                and row["outcome"] is not None
                and row["ended_at"] is not None
                and row["work_item_id"] == str(crash_item_id)
                and row["idempotency_key"] == stored_confirm_key
            ]
            if abandoned and survivor:
                resumed = True
                break
            time.sleep(POLL_S)
        assert resumed, "survivor did not reclaim the abandoned lease"

        _docker("up", "-d", "worker")
        killed = False
        restarted_worker_ids = [
            value for value in _docker("ps", "-q", "worker").stdout.splitlines() if value
        ]
        assert len(restarted_worker_ids) == 2

        _wait_order(
            crash_order_id,
            {"confirmed", "being_prepared", "ready", "out_for_delivery", "delivered"},
            timeout_s=ORDER_TIMEOUT_S,
        )
        crash_snapshot = _snapshot(cohort_id=crash_cohort, order_id=crash_order_id)
        trace = crash_snapshot["trace"]
        assert trace is not None
        confirm_attempts = [row for row in trace["attempts"] if row["work_type"] == "confirm"]
        abandoned = [
            row
            for row in confirm_attempts
            if row["lease_owner"] == crash_owner
            and row["outcome"] is None
            and row["work_item_id"] == str(crash_item_id)
            and row["idempotency_key"] == stored_confirm_key
        ]
        survivor = [
            row
            for row in confirm_attempts
            if row["lease_owner"] != crash_owner
            and row["outcome"] is not None
            and row["work_item_id"] == str(crash_item_id)
            and row["idempotency_key"] == stored_confirm_key
        ]
        assert abandoned, confirm_attempts
        assert survivor, confirm_attempts
        confirmed = [
            event
            for event in trace["order_events"]
            if event["to_state"] == "confirmed" and event["applied"]
        ]
        assert len(confirmed) == 1
        assert all(event["cause"] not in LEASE_LIFECYCLE_CAUSES for event in trace["order_events"])
        assert crash_snapshot["duplicate_effects"] == 0
        assert _ledger_count(RSIM_URL, stored_confirm_key) == 1

        # Close the crash beat before introducing the separate courier-blackout
        # fixture. Otherwise that deliberate fault can park the crash order's
        # later dispatch and obscure the lease-recovery assertion.
        assert _wait_order(crash_order_id, {"delivered"}, timeout_s=ORDER_TIMEOUT_S) == "delivered"

        # Runbook's second beat: catch dispatch at ready, exhaust under courier
        # blackout, redrive while the fault remains (same job parks again), then
        # clear and Redrive through Watch's POST path.
        park_order_id = _place(cohort_id=park_cohort, prefix="scenario3-park")
        dispatch_key = dispatch_idempotency_key(park_order_id)
        _wait_order(park_order_id, {"ready"}, timeout_s=ORDER_TIMEOUT_S)
        post_sim_faults(CSIM_URL, {"mode": "blackout", "seconds": 30, "mix": "off"})

        parked_row = _wait_parked_dispatch(cohort_id=park_cohort, order_id=park_order_id)
        assert parked_row["id"]
        assert parked_row["owner"]
        assert parked_row["reason"] == "retry_budget_exhausted"
        assert parked_row["next_action"] == "redrive"

        post_sim_faults(CSIM_URL, {"mode": "blackout", "seconds": 30, "mix": "off"})
        mid_blackout = _http(
            "POST",
            f"{DASHBOARD_URL}/work-items/{parked_row['id']}/redrive",
        )
        assert mid_blackout.status_code == 200, mid_blackout.text
        mid_body = mid_blackout.json()
        assert mid_body["id"] == parked_row["id"]
        assert mid_body["status"] == "pending"
        assert mid_body["attempt_count"] == 0
        assert mid_body["idempotency_key"] == dispatch_key
        assert _ledger_count(CSIM_URL, dispatch_key) == 0

        reparker = _wait_parked_dispatch(
            cohort_id=park_cohort,
            order_id=park_order_id,
            work_item_id=parked_row["id"],
        )
        assert reparker["id"] == parked_row["id"]
        assert reparker["reason"] == "retry_budget_exhausted"
        assert _ledger_count(CSIM_URL, dispatch_key) == 0

        post_sim_faults(CSIM_URL, {"mode": "clear", "mix": "off"})
        redriven = _http(
            "POST",
            f"{DASHBOARD_URL}/work-items/{parked_row['id']}/redrive",
        )
        assert redriven.status_code == 200, redriven.text
        redrive_body = redriven.json()
        assert redrive_body["id"] == parked_row["id"]
        assert redrive_body["status"] == "pending"
        assert redrive_body["attempt_count"] == 0
        assert redrive_body["idempotency_key"] == dispatch_key

        assert _wait_order(park_order_id, {"delivered"}, timeout_s=ORDER_TIMEOUT_S) == "delivered"
        assert _ledger_count(CSIM_URL, dispatch_key) == 1
        delivered = _snapshot(cohort_id=park_cohort, order_id=park_order_id)
        assert delivered["duplicate_effects"] == 0
        assert delivered["conservation"]["residual"] == 0
        assert not any(row["order_id"] == str(park_order_id) for row in delivered["parked_list"])
        finished_crash = _snapshot(cohort_id=crash_cohort, order_id=crash_order_id)
        finished_confirms = [
            event
            for event in finished_crash["trace"]["order_events"]
            if event["to_state"] == "confirmed" and event["applied"]
        ]
        assert len(finished_confirms) == 1
    finally:
        mix_on()
        if killed:
            _docker("up", "-d", "worker")
