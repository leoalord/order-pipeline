"""Compose walk: POST /orders then GET until delivered. Mix stays off."""

from __future__ import annotations

import time
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.models import OrderEvent
from order_pipeline.worker.dispatch import dispatch_idempotency_key
from tests.sim_admin import mix_off

API_URL = "http://localhost:8000"
CSIM_URL = "http://localhost:8082"
WALK_TIMEOUT_S = 180.0
POLL_EVERY_S = 0.05
EXPECTED = (
    "placed",
    "confirmed",
    "being_prepared",
    "ready",
    "out_for_delivery",
    "delivered",
)


def test_chips_walks_placed_through_delivered(session_factory: sessionmaker[Session]) -> None:
    mix_off()
    place_key = f"walk-delivered-{uuid.uuid4()}"
    try:
        posted = httpx.post(
            f"{API_URL}/orders",
            json={"items": ["chips"]},
            headers={"Idempotency-Key": place_key},
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"API is down at {API_URL}: {exc}")

    assert posted.status_code == 201, posted.text
    body = posted.json()
    order_id = body["id"]
    assert body["state"] == "placed"
    assert body["items"] == ["chips"]

    seen: list[str] = ["placed"]
    ready_at: float | None = None
    started = time.monotonic()
    deadline = started + WALK_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            got = httpx.get(f"{API_URL}/orders/{order_id}", timeout=5.0)
        except httpx.RequestError as exc:
            pytest.fail(f"API is down at {API_URL}: {exc}")
        assert got.status_code == 200, got.text
        state = got.json()["state"]
        if state != seen[-1]:
            seen.append(state)
        if state == "ready" and ready_at is None:
            ready_at = time.monotonic()
        if state == "delivered":
            break
        if state in {"failed", "cancelled"}:
            pytest.fail(f"order {order_id} left the walk early: {seen}")
        time.sleep(POLL_EVERY_S)
    else:
        pytest.fail(
            f"order {order_id} did not reach delivered within {WALK_TIMEOUT_S}s; seen={seen}"
        )

    elapsed_s = time.monotonic() - started
    if "ready" not in seen and "out_for_delivery" in seen:
        with session_factory() as session:
            ready_event = session.scalars(
                select(OrderEvent).where(
                    OrderEvent.order_id == uuid.UUID(str(order_id)),
                    OrderEvent.to_state == "ready",
                    OrderEvent.applied.is_(True),
                )
            ).one_or_none()
        assert ready_event is not None, seen
        seen.insert(seen.index("out_for_delivery"), "ready")

    after_ready_s = (time.monotonic() - ready_at) if ready_at is not None else None
    assert tuple(seen) == EXPECTED, (
        f"seen={seen} elapsed={elapsed_s:.1f}s after_ready={after_ready_s}"
    )
    if after_ready_s is not None:
        assert after_ready_s < 60.0, f"quiet near trip took {after_ready_s:.1f}s after ready"
    assert elapsed_s < 120.0, f"full walk took {elapsed_s:.1f}s"
    print(f"delivered_walk elapsed_s={elapsed_s:.1f} after_ready_s={after_ready_s}")

    dispatch_key = dispatch_idempotency_key(uuid.UUID(str(order_id)))
    try:
        ledger = httpx.get(f"{CSIM_URL}/admin/ledger", timeout=5.0)
        faults = httpx.get(f"{CSIM_URL}/admin/faults", timeout=5.0)
    except httpx.RequestError as exc:
        pytest.fail(f"courier sim is down at {CSIM_URL}: {exc}")
    assert ledger.status_code == 200, ledger.text
    assert ledger.json()["counts"][dispatch_key] == 1
    assert faults.status_code == 200, faults.text
    assert faults.json()["mix"] == "off"
    assert faults.json()["mode"] == "off"
