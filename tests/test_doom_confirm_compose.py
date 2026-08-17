"""Scenario-2 fixture: targeted confirms fail at their clocks; other keys recover."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.intake import confirm_idempotency_key
from order_pipeline.models import Order, OrderEvent
from tests.sim_admin import CSIM_URL, RSIM_URL, mix_off, post_sim_faults

API_URL = "http://localhost:8000"
LOADGEN_URL = "http://localhost:8090"
POLL_S = 0.25


def _http(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> httpx.Response:
    try:
        return httpx.request(method, url, json=json, headers=headers, timeout=timeout)
    except httpx.RequestError as exc:
        pytest.fail(f"request failed {method} {url}: {exc}")


def _order(order_id: uuid.UUID) -> dict[str, Any]:
    response = _http("GET", f"{API_URL}/orders/{order_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


@pytest.mark.slow
def test_doomed_ids_fail_at_deadlines_while_untagged_confirm_recovers(
    session_factory: sessionmaker[Session],
) -> None:
    """The deterministic explicit-fail set is independent of global blackout."""
    _http("POST", f"{LOADGEN_URL}/stop", timeout=15)
    mix_off(RSIM_URL, CSIM_URL)
    minted = _http("POST", f"{LOADGEN_URL}/cohort/new")
    assert minted.status_code == 200, minted.text
    cohort_id = minted.json()["cohort_id"]

    try:
        doomed = _http("POST", f"{LOADGEN_URL}/beat/doom-confirm", timeout=15)
        assert doomed.status_code == 200, doomed.text
        raw_ids = doomed.json()["order_ids"]
        assert isinstance(raw_ids, list)
        assert 2 <= len(raw_ids) <= 3
        order_ids = [uuid.UUID(raw) for raw in raw_ids]
        assert len(set(order_ids)) == len(order_ids)
        assert doomed.json()["cohort_id"] == cohort_id

        accepted_at: dict[uuid.UUID, datetime] = {}
        for order_id in order_ids:
            body = _order(order_id)
            assert body["state"] == "placed", body
            stamp = datetime.fromisoformat(body["accepted_at"].replace("Z", "+00:00"))
            accepted_at[order_id] = stamp.astimezone(UTC)

        faults = _http("GET", f"{RSIM_URL}/admin/faults")
        assert faults.status_code == 200, faults.text
        fault_body = faults.json()
        assert fault_body["mode"] == "off"
        assert fault_body["blackout_remaining_s"] == 0
        targets = fault_body["confirm_unavailable"]
        assert {target["idempotency_key"] for target in targets} == {
            confirm_idempotency_key(order_id) for order_id in order_ids
        }
        until_by_key = {
            target["idempotency_key"]: datetime.fromisoformat(
                target["until"].replace("Z", "+00:00")
            )
            for target in targets
        }
        for order_id, accepted in accepted_at.items():
            assert until_by_key[confirm_idempotency_key(order_id)] == accepted + timedelta(
                seconds=120
            )

        ordinary = _http(
            "POST",
            f"{API_URL}/orders",
            json={"items": ["chips"], "cohort_id": cohort_id},
            headers={"Idempotency-Key": f"doom-ordinary-{uuid.uuid4()}"},
        )
        assert ordinary.status_code == 201, ordinary.text
        ordinary_id = uuid.UUID(ordinary.json()["id"])
        ordinary_deadline = time.monotonic() + 30
        ordinary_state = "placed"
        while time.monotonic() < ordinary_deadline:
            ordinary_state = _order(ordinary_id)["state"]
            if ordinary_state != "placed":
                break
            time.sleep(POLL_S)
        assert ordinary_state in {
            "confirmed",
            "being_prepared",
            "ready",
            "out_for_delivery",
            "delivered",
        }, ordinary_state

        wait_until = max(accepted_at.values()) + timedelta(seconds=138)
        last_states = {order_id: "placed" for order_id in order_ids}
        while datetime.now(UTC) < wait_until:
            for order_id in order_ids:
                state = _order(order_id)["state"]
                assert state in {"placed", "failed"}, (order_id, state)
                last_states[order_id] = state
            if all(state == "failed" for state in last_states.values()):
                break
            time.sleep(POLL_S)
        assert set(last_states.values()) == {"failed"}, last_states

        with session_factory() as session:
            for order_id, accepted in accepted_at.items():
                order = session.get(Order, order_id)
                assert order is not None
                assert order.state == "failed"
                failed = session.scalars(
                    select(OrderEvent).where(
                        OrderEvent.order_id == order_id,
                        OrderEvent.applied.is_(True),
                        OrderEvent.cause == "confirm_deadline",
                    )
                ).one()
                deadline = accepted + timedelta(seconds=120)
                assert failed.timestamp >= deadline
                assert failed.timestamp <= deadline + timedelta(seconds=15)

        expired = _http("GET", f"{RSIM_URL}/admin/faults")
        assert expired.status_code == 200, expired.text
        assert expired.json()["confirm_unavailable"] == []
        assert expired.json()["mode"] == "off"
    finally:
        post_sim_faults(RSIM_URL, {"mode": "clear", "mix": "off"})
        post_sim_faults(CSIM_URL, {"mode": "clear", "mix": "off"})
