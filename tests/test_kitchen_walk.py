"""Compose walk: POST /orders then GET until ready, all three kitchen arrows."""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

API_URL = "http://localhost:8000"
WALK_TIMEOUT_S = 120.0
POLL_EVERY_S = 0.2
EXPECTED = ("placed", "confirmed", "being_prepared", "ready")


def test_burrito_walks_placed_confirmed_being_prepared_ready() -> None:
    place_key = f"walk-burrito-{uuid.uuid4()}"
    try:
        posted = httpx.post(
            f"{API_URL}/orders",
            json={"items": ["burrito"]},
            headers={"Idempotency-Key": place_key},
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"API is down at {API_URL}: {exc}")

    assert posted.status_code == 201, posted.text
    body = posted.json()
    order_id = body["id"]
    assert body["state"] == "placed"
    assert body["items"] == ["burrito"]

    seen: list[str] = ["placed"]
    deadline = time.monotonic() + WALK_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            got = httpx.get(f"{API_URL}/orders/{order_id}", timeout=5.0)
        except httpx.RequestError as exc:
            pytest.fail(f"API is down at {API_URL}: {exc}")
        assert got.status_code == 200, got.text
        state = got.json()["state"]
        if state != seen[-1]:
            seen.append(state)
        if state == "ready":
            break
        if state in {"failed", "cancelled"}:
            pytest.fail(f"order {order_id} left the kitchen walk early: {seen}")
        time.sleep(POLL_EVERY_S)
    else:
        pytest.fail(f"order {order_id} did not reach ready within {WALK_TIMEOUT_S}s; seen={seen}")

    assert tuple(seen) == EXPECTED, seen
