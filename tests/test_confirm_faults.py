"""Compose proofs: 5xx_after / drop (timeline D) and reclaim (timeline C)."""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.intake import confirm_idempotency_key
from order_pipeline.models import Attempt, Order, OrderEvent, WorkItem
from order_pipeline.worker.chassis import Worker
from order_pipeline.worker.deps import DepCaps
from order_pipeline.worker.http import RestaurantClient
from order_pipeline.worker.kitchen import KitchenHandlers
from order_pipeline.worker.settings import WorkerSettings

TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:55432/order_pipeline"
REPO_ROOT = Path(__file__).resolve().parents[1]
API_URL = "http://localhost:8000"
RSIM_URL = "http://localhost:8081"
WORKER_URL = "http://localhost:8083"
CONFIRM_TIMEOUT_S = 40.0
POLL_EVERY_S = 0.2
CONFIRMED_OR_BEYOND = frozenset(
    {"confirmed", "being_prepared", "ready", "out_for_delivery", "delivered"}
)
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
FaultModeName = Literal["5xx_after", "drop"]


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


def _set_faults(mode: str) -> dict[str, Any]:
    response = _http("POST", f"{RSIM_URL}/admin/faults", json={"mode": mode, "mix": "off"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    assert body["mix"] == "off"
    assert body["flaky_5xx_pct"] == 0.0
    assert body["flaky_drop_pct"] == 0.0
    return body


def _clear_faults() -> None:
    response = _http("POST", f"{RSIM_URL}/admin/faults", json={"mode": "clear", "mix": "off"})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "off"
    assert response.json()["mix"] == "off"


def _ledger_count(key: str) -> int:
    response = _http("GET", f"{RSIM_URL}/admin/ledger")
    assert response.status_code == 200, response.text
    counts = response.json()["counts"]
    assert isinstance(counts, dict)
    raw = counts.get(key, 0)
    assert isinstance(raw, int)
    return raw


def _place_chips() -> uuid.UUID:
    place_key = f"faults-{uuid.uuid4()}"
    posted = _http(
        "POST",
        f"{API_URL}/orders",
        json={"items": ["chips"]},
        headers={"Idempotency-Key": place_key},
    )
    assert posted.status_code == 201, posted.text
    order_id = uuid.UUID(posted.json()["id"])
    assert posted.json()["state"] == "placed"
    return order_id


def _wait_until_confirmed(order_id: uuid.UUID) -> str:
    deadline = time.monotonic() + CONFIRM_TIMEOUT_S
    last = "placed"
    while time.monotonic() < deadline:
        got = _http("GET", f"{API_URL}/orders/{order_id}")
        assert got.status_code == 200, got.text
        last = got.json()["state"]
        assert isinstance(last, str)
        if last in CONFIRMED_OR_BEYOND:
            return last
        if last in {"failed", "cancelled"}:
            pytest.fail(f"order {order_id} left the confirm path early: {last}")
        time.sleep(POLL_EVERY_S)
    pytest.fail(
        f"order {order_id} did not reach confirmed within {CONFIRM_TIMEOUT_S}s; last={last}"
    )


def _applied_confirmed(session: Session, order_id: uuid.UUID) -> list[OrderEvent]:
    return list(
        session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id,
                OrderEvent.to_state == "confirmed",
                OrderEvent.applied.is_(True),
            )
        ).all()
    )


def _compose(*args: str, timeout: float = 60.0) -> None:
    result = subprocess.run(
        ["docker", "compose", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "no output"
        pytest.fail(f"docker compose {' '.join(args)} failed: {detail}")


def _wait_worker_healthy(*, attempts: int = 80) -> None:
    last_error = "no attempts"
    for _ in range(attempts):
        try:
            response = httpx.get(f"{WORKER_URL}/health", timeout=2.0)
            if response.status_code == 200 and response.json() == {"status": "ok"}:
                return
            last_error = f"status {response.status_code}: {response.text}"
        except httpx.RequestError as exc:
            last_error = str(exc)
        time.sleep(0.25)
    pytest.fail(f"worker never became healthy on {WORKER_URL}: {last_error}")


def _wait_worker_down(*, attempts: int = 40) -> None:
    for _ in range(attempts):
        try:
            httpx.get(f"{WORKER_URL}/health", timeout=1.0)
        except httpx.RequestError:
            return
        time.sleep(0.25)
    pytest.fail("worker still answering health after compose stop")


@pytest.mark.parametrize("mode", ["5xx_after", "drop"])
def test_sticky_fault_one_ledger_row_and_one_confirmed_event(
    mode: FaultModeName,
    session_factory: sessionmaker[Session],
) -> None:
    armed = _set_faults(mode)
    assert armed["mode"] == mode
    try:
        order_id = _place_chips()
        confirm_key = confirm_idempotency_key(order_id)
        _wait_until_confirmed(order_id)
        assert _ledger_count(confirm_key) == 1
        with session_factory() as session:
            confirmed = _applied_confirmed(session, order_id)
            assert len(confirmed) == 1
            assert confirmed[0].cause == "confirm"
            causes = {
                event.cause
                for event in session.scalars(
                    select(OrderEvent).where(OrderEvent.order_id == order_id)
                )
            }
            assert causes.isdisjoint(LEASE_LIFECYCLE_CAUSES)
    finally:
        _clear_faults()


def test_reclaim_one_effect_null_attempt_no_second_confirm(
    db_engine: Engine,
    session_factory: sessionmaker[Session],
) -> None:
    _compose("stop", "worker")
    _wait_worker_down()
    try:
        _set_faults("drop")
        try:
            order_id = _place_chips()
            confirm_key = confirm_idempotency_key(order_id)
            with session_factory() as session:
                item = session.scalars(
                    select(WorkItem).where(
                        WorkItem.order_id == order_id,
                        WorkItem.work_type == "confirm",
                    )
                ).one()
                item_id = item.id

            settings = WorkerSettings(
                database_url=TEST_DATABASE_URL,
                restaurant_base_url=RSIM_URL,
            )
            asyncio.run(
                _reclaim_mid_call(
                    db_engine,
                    session_factory,
                    settings,
                    item_id=item_id,
                    confirm_key=confirm_key,
                )
            )

            with session_factory() as session:
                attempts = list(
                    session.scalars(
                        select(Attempt)
                        .where(Attempt.work_item_id == item_id)
                        .order_by(Attempt.started_at.asc())
                    )
                )
                assert len(attempts) == 2
                assert attempts[0].outcome is None
                assert attempts[0].ended_at is None
                assert attempts[1].outcome == "ok"
                assert attempts[1].id != attempts[0].id
                confirmed = _applied_confirmed(session, order_id)
                assert len(confirmed) == 1
                events = list(
                    session.scalars(select(OrderEvent).where(OrderEvent.order_id == order_id))
                )
                assert all(event.cause not in LEASE_LIFECYCLE_CAUSES for event in events)
                order = session.get(Order, order_id)
                assert order is not None
                assert order.state == "confirmed"
            assert _ledger_count(confirm_key) == 1
        finally:
            _clear_faults()
    finally:
        _compose("start", "worker")
        _wait_worker_healthy()


async def _reclaim_mid_call(
    db_engine: Engine,
    session_factory: sessionmaker[Session],
    settings: WorkerSettings,
    *,
    item_id: uuid.UUID,
    confirm_key: str,
) -> None:
    caps = DepCaps(settings)
    restaurant = RestaurantClient(settings, caps)
    kitchen = KitchenHandlers(settings, restaurant)
    worker_a = Worker(
        settings,
        db_engine,
        handlers={"confirm": kitchen.confirm},
        worker_id="reclaim-a",
    )
    worker_b = Worker(
        settings,
        db_engine,
        handlers={"confirm": kitchen.confirm},
        worker_id="reclaim-b",
    )
    try:
        claimed_a = worker_a.claim(work_item_id=item_id)
        assert claimed_a is not None
        assert claimed_a.idempotency_key == confirm_key
        try:
            await kitchen.confirm(claimed_a)
        except httpx.RequestError:
            pass
        assert _ledger_count(confirm_key) == 1

        with session_factory.begin() as session:
            item = session.get(WorkItem, item_id)
            assert item is not None
            item.lease_until = datetime.now(UTC) - timedelta(seconds=1)

        claimed_b = worker_b.claim(work_item_id=item_id)
        assert claimed_b is not None
        assert claimed_b.idempotency_key == confirm_key
        assert claimed_b.attempt_id != claimed_a.attempt_id
        await worker_b.process(claimed_b)
        assert _ledger_count(confirm_key) == 1
    finally:
        await restaurant.aclose()
