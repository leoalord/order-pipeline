"""Bonus B: burrito=0 is a permanent whole-order fail, one confirm, zero retries."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.models import Attempt, Order, WorkItem
from order_pipeline.restaurant.stock import BONUS_RESTORE_STOCK
from tests.sim_admin import (
    CSIM_URL,
    RSIM_URL,
    mix_off,
    restore_restaurant_stock,
    set_restaurant_stock,
)

API_URL = "http://localhost:8000"
LOADGEN_URL = "http://localhost:8090"
FAIL_TIMEOUT_S = 15.0
POLL_EVERY_S = 0.2


def _http(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> httpx.Response:
    try:
        return httpx.request(
            method, url, json=json, headers=headers, params=params, timeout=timeout
        )
    except httpx.RequestError as exc:
        pytest.fail(f"request failed {method} {url}: {exc}")


def _wait_failed(order_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + FAIL_TIMEOUT_S
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = _http("GET", f"{API_URL}/orders/{order_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        assert isinstance(last, dict)
        if last.get("state") == "failed":
            return last
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"order {order_id} never failed within {FAIL_TIMEOUT_S}s: {last}")


def _confirm_attempts(session: Session, order_id: uuid.UUID) -> list[Attempt]:
    item = session.scalars(
        select(WorkItem).where(
            WorkItem.order_id == order_id,
            WorkItem.work_type == "confirm",
        )
    ).one()
    return list(session.scalars(select(Attempt).where(Attempt.work_item_id == item.id)).all())


@pytest.fixture
def restored_stock() -> Any:
    restore_restaurant_stock()
    try:
        yield
    finally:
        restore_restaurant_stock()


def test_beat_place_returns_an_order_id(restored_stock: None) -> None:
    del restored_stock
    mix_off()
    response = _http("POST", f"{LOADGEN_URL}/beat/place", json={"item": "burrito"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "order_id" in body
    order_id = uuid.UUID(body["order_id"])
    fetched = _http("GET", f"{API_URL}/orders/{order_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["items"] == ["burrito"]
    assert fetched.json()["id"] == str(order_id)


def test_zero_burrito_fails_order_with_one_confirm_no_retry(
    session_factory: sessionmaker[Session],
    restored_stock: None,
) -> None:
    del restored_stock
    mix_off()
    set_restaurant_stock("burrito", 0)
    try:
        placed = _http("POST", f"{LOADGEN_URL}/beat/place", json={"item": "burrito"})
        assert placed.status_code == 200, placed.text
        order_id = uuid.UUID(placed.json()["order_id"])
        existing = _http("GET", f"{API_URL}/orders/{order_id}")
        assert existing.status_code == 200, existing.text
        assert existing.json()["state"] in {"placed", "failed"}

        failed = _wait_failed(str(order_id))
        assert failed["state"] == "failed"
        assert failed["items"] == ["burrito"]

        with session_factory() as session:
            order = session.get(Order, order_id)
            assert order is not None
            assert order.state == "failed"
            attempts = _confirm_attempts(session, order_id)
            assert len(attempts) == 1
            assert attempts[0].outcome == "http_4xx"
            confirm = session.scalars(
                select(WorkItem).where(
                    WorkItem.order_id == order_id,
                    WorkItem.work_type == "confirm",
                )
            ).one()
            assert confirm.status == "failed"
            assert confirm.attempt_count == 1

        snap = _http(
            "GET",
            f"{API_URL}/snapshot",
            params={"cohort_id": existing.json()["cohort_id"]},
        )
        assert snap.status_code == 200, snap.text
        conservation = snap.json()["conservation"]
        assert conservation["residual"] == 0
        assert conservation["failed"] >= 1
        ledger = _http("GET", f"{RSIM_URL}/admin/ledger")
        assert ledger.status_code == 200, ledger.text
        confirm_key = f"({order_id}, confirm)"
        assert ledger.json()["counts"].get(confirm_key) is None
        stock = _http("GET", f"{RSIM_URL}/admin/stock")
        assert stock.json()["burrito"] == 0
    finally:
        restore_restaurant_stock()


def test_two_item_cart_fails_whole_order_and_consumes_nothing(
    session_factory: sessionmaker[Session],
    restored_stock: None,
) -> None:
    del restored_stock
    mix_off()
    set_restaurant_stock("burrito", 0)
    try:
        before = _http("GET", f"{RSIM_URL}/admin/stock").json()
        assert before["chips"] == BONUS_RESTORE_STOCK
        assert before["burrito"] == 0
        cohort_id = uuid.uuid4()
        placed = _http(
            "POST",
            f"{API_URL}/orders",
            json={"items": ["chips", "burrito"], "cohort_id": str(cohort_id)},
            headers={"Idempotency-Key": f"oos-two-{uuid.uuid4()}"},
        )
        assert placed.status_code == 201, placed.text
        order_id = uuid.UUID(placed.json()["id"])
        _wait_failed(str(order_id))

        with session_factory() as session:
            attempts = _confirm_attempts(session, order_id)
            assert len(attempts) == 1
            assert attempts[0].outcome == "http_4xx"

        after = _http("GET", f"{RSIM_URL}/admin/stock").json()
        assert after == before
        snap = _http("GET", f"{API_URL}/snapshot", params={"cohort_id": str(cohort_id)})
        assert snap.status_code == 200, snap.text
        assert snap.json()["conservation"]["residual"] == 0
    finally:
        restore_restaurant_stock()


def test_courier_compose_has_no_stock_admin(restored_stock: None) -> None:
    del restored_stock
    missing = _http("GET", f"{CSIM_URL}/admin/stock")
    assert missing.status_code == 404
    posted = _http("POST", f"{CSIM_URL}/admin/stock", json={"item": "burrito", "count": 0})
    assert posted.status_code == 404
