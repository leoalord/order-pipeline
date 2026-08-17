"""Compose-backed POST /orders: 201, replay, 409, zero rows on bad carts, one-commit."""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from order_pipeline.api.settings import APISettings
from order_pipeline.intake import DEFAULT_COHORT_ID, confirm_idempotency_key

REPO_ROOT = Path(__file__).resolve().parents[1]
API_URL = "http://localhost:8000"


def _psql(sql: str) -> str:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-d",
            "order_pipeline",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atc",
            sql,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "no output"
        pytest.fail(f"DB inspection failed (compose/DB down?): {detail}")
    return result.stdout.strip()


def _accept_counts() -> tuple[int, int]:
    """Worker may write events/attempts/poll items; it never inserts orders or intake keys."""
    row = _psql("SELECT (SELECT count(*) FROM orders), (SELECT count(*) FROM intake_keys)")
    orders, keys = (int(part) for part in row.split("|"))
    return orders, keys


def _accept_counts_for_place_key(place_key: str) -> tuple[int, int]:
    """Count only rows attributable to one rejected/accepted request."""
    row = _psql(
        "SELECT "
        "(SELECT count(*) FROM orders o JOIN intake_keys i ON i.order_id = o.id "
        f"WHERE i.place_key = '{place_key}'), "
        f"(SELECT count(*) FROM intake_keys WHERE place_key = '{place_key}')"
    )
    orders, keys = (int(part) for part in row.split("|"))
    return orders, keys


def _post(
    items: list[str],
    place_key: str,
    *,
    cohort_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> httpx.Response:
    payload: dict[str, Any] = {"items": items}
    if cohort_id is not None:
        payload["cohort_id"] = cohort_id
    headers = {"Content-Type": "application/json"}
    if extra_headers is None:
        headers["Idempotency-Key"] = place_key
    else:
        headers.update(extra_headers)
    try:
        return httpx.post(f"{API_URL}/orders", json=payload, headers=headers, timeout=timeout)
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")


def test_post_201_get_placed_replay_same_id_different_cart_409() -> None:
    place_key = f"test-happy-{uuid.uuid4()}"
    created = _post(["burrito"], place_key)
    assert created.status_code == 201, created.text
    body = created.json()
    order_id = body["id"]
    assert body["state"] == "placed"
    assert body["accepted_at"]
    assert body["items"] == ["burrito"]
    assert body["cohort_id"] == str(DEFAULT_COHORT_ID)

    try:
        fetched = httpx.get(f"{API_URL}/orders/{order_id}", timeout=5.0)
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")
    assert fetched.status_code == 200, fetched.text
    got = fetched.json()
    assert got["id"] == order_id
    assert got["state"] in {"placed", "confirmed", "being_prepared", "ready"}
    assert got["accepted_at"]
    assert got["items"] == ["burrito"]
    assert got["cohort_id"] == str(DEFAULT_COHORT_ID)

    work = _psql(
        "SELECT type, idempotency_key FROM work_items "
        f"WHERE order_id = '{order_id}' AND type = 'confirm'"
    )
    work_type, stored_key = work.split("|")
    assert work_type == "confirm"
    assert stored_key == confirm_idempotency_key(uuid.UUID(order_id))
    assert "confirm" in stored_key

    ttl_seconds = _psql(
        "SELECT EXTRACT(EPOCH FROM (expires_at - created_at))::bigint "
        f"FROM intake_keys WHERE place_key = '{place_key}'"
    )
    assert int(ttl_seconds) == 48 * 3600

    replay = _post(["burrito"], place_key)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == order_id

    conflict = _post(["taco"], place_key)
    assert conflict.status_code == 409, conflict.text
    assert _psql(f"SELECT count(*) FROM orders WHERE id = '{order_id}'") == "1"
    assert _psql(f"SELECT count(*) FROM intake_keys WHERE place_key = '{place_key}'") == "1"


def test_optional_cohort_id_is_stored() -> None:
    place_key = f"test-cohort-{uuid.uuid4()}"
    cohort_id = str(uuid.uuid4())
    created = _post(["chips"], place_key, cohort_id=cohort_id)
    assert created.status_code == 201, created.text
    assert created.json()["cohort_id"] == cohort_id
    try:
        fetched = httpx.get(f"{API_URL}/orders/{created.json()['id']}", timeout=5.0)
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")
    assert fetched.json()["cohort_id"] == cohort_id


@pytest.mark.parametrize(
    "kwargs",
    [
        {"items": [], "place_key": "will-set"},
        {"items": ["burrito", "taco", "chips", "taco"], "place_key": "will-set"},
        {"items": ["chicken_burrito"], "place_key": "will-set"},
        {"items": ["not-a-menu-item"], "place_key": "will-set"},
    ],
)
def test_malformed_and_over_cap_carts_create_zero_rows(kwargs: dict[str, object]) -> None:
    place_key = f"test-bad-{uuid.uuid4()}"
    response = _post(kwargs["items"], place_key)  # type: ignore[arg-type]
    assert 400 <= response.status_code < 500, response.text
    assert _accept_counts_for_place_key(place_key) == (0, 0)


def test_missing_place_key_creates_zero_rows() -> None:
    cohort_id = str(uuid.uuid4())
    try:
        response = httpx.post(
            f"{API_URL}/orders",
            json={"items": ["burrito"], "cohort_id": cohort_id},
            headers={"Content-Type": "application/json"},
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")
    assert 400 <= response.status_code < 500, response.text
    assert _psql(f"SELECT count(*) FROM orders WHERE cohort_id = '{cohort_id}'") == "0"


def test_malformed_json_creates_zero_rows() -> None:
    place_key = f"test-malformed-{uuid.uuid4()}"
    try:
        response = httpx.post(
            f"{API_URL}/orders",
            content=b"{not-json",
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": place_key,
            },
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")
    assert 400 <= response.status_code < 500, response.text
    assert _accept_counts_for_place_key(place_key) == (0, 0)


def test_timeline_b_no_order_without_confirm_work_item() -> None:
    place_key = f"test-timeline-b-{uuid.uuid4()}"
    created = _post(["taco"], place_key)
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]
    work_count = _psql(
        f"SELECT count(*) FROM work_items WHERE order_id = '{order_id}' AND type = 'confirm'"
    )
    assert work_count == "1"
    orphans = _psql(
        "SELECT count(*) FROM orders o WHERE NOT EXISTS ("
        "SELECT 1 FROM work_items w WHERE w.order_id = o.id AND w.type = 'confirm'"
        ")"
    )
    assert orphans == "0"


def test_timeline_a_retried_place_key_is_one_order() -> None:
    place_key = f"test-timeline-a-{uuid.uuid4()}"
    first = _post(["chips", "taco"], place_key)
    second = _post(["chips", "taco"], place_key)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    order_id = first.json()["id"]
    assert _psql(f"SELECT count(*) FROM orders WHERE id = '{order_id}'") == "1"
    assert _psql(f"SELECT count(*) FROM intake_keys WHERE place_key = '{place_key}'") == "1"
    assert (
        _psql(f"SELECT count(*) FROM work_items WHERE order_id = '{order_id}' AND type = 'confirm'")
        == "1"
    )
    linked = _psql(
        "SELECT count(*) FROM orders o "
        "JOIN intake_keys i ON i.order_id = o.id "
        f"WHERE i.place_key = '{place_key}'"
    )
    assert linked == "1"


def test_concurrent_same_key_is_one_order() -> None:
    """N racers, one key: UniqueViolation recovery, one id, no leftover rows."""
    place_key = f"test-concurrent-{uuid.uuid4()}"
    workers = 12
    before = _accept_counts()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_post, ["burrito"], place_key, timeout=15.0) for _ in range(workers)]
        responses = [future.result() for future in futures]
    failures = [response.text for response in responses if response.status_code != 201]
    assert failures == []
    ids = {response.json()["id"] for response in responses}
    assert len(ids) == 1
    order_id = next(iter(ids))
    after = _accept_counts()
    assert after[0] - before[0] == 1
    assert after[1] - before[1] == 1
    assert _psql(f"SELECT count(*) FROM orders WHERE id = '{order_id}'") == "1"
    assert _psql(f"SELECT count(*) FROM intake_keys WHERE place_key = '{place_key}'") == "1"
    assert (
        _psql(f"SELECT count(*) FROM work_items WHERE order_id = '{order_id}' AND type = 'confirm'")
        == "1"
    )
    assert (
        _psql(
            "SELECT count(*) FROM order_events "
            f"WHERE order_id = '{order_id}' AND cause = 'place' AND applied"
        )
        == "1"
    )


def test_get_missing_order_is_404() -> None:
    missing = uuid.uuid4()
    try:
        response = httpx.get(f"{API_URL}/orders/{missing}", timeout=5.0)
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")
    assert response.status_code == 404


def test_saturate_accept_concurrency_429_creates_no_order(db_engine: Engine) -> None:
    """Hold Place Order in-flight at the door cap; extras 429 and never insert."""
    door_cap = APISettings(
        database_url="postgresql+psycopg://postgres:postgres@localhost/unused"
    ).accept_concurrency
    extra = 8
    n = door_cap + extra
    keys = [f"test-door-{uuid.uuid4()}" for _ in range(n)]
    before = _accept_counts()

    lock_held = threading.Event()
    release_lock = threading.Event()
    lock_errors: list[BaseException] = []

    def hold_orders_share_lock() -> None:
        # SHARE blocks INSERT/UPDATE (ROW EXCLUSIVE) but not SELECT, so we can
        # count rows while the admitted POSTs sit in-flight.
        try:
            with db_engine.connect() as conn:
                trans = conn.begin()
                conn.execute(text("LOCK TABLE orders IN SHARE MODE"))
                lock_held.set()
                if not release_lock.wait(timeout=30):
                    trans.rollback()
                    return
                trans.rollback()
        except BaseException as exc:
            lock_errors.append(exc)
            lock_held.set()

    locker = threading.Thread(target=hold_orders_share_lock, daemon=True)
    locker.start()
    if not lock_held.wait(timeout=5):
        pytest.fail("could not lock orders to hold Place Order in-flight")
    if lock_errors:
        pytest.fail(f"orders SHARE lock failed: {lock_errors[0]!r}")

    pool = ThreadPoolExecutor(max_workers=n)
    futures: list[Future[httpx.Response]] = []
    try:
        futures = [pool.submit(_post, ["chips"], key, timeout=30.0) for key in keys]
        deadline = time.monotonic() + 15
        rejected: list[tuple[str, httpx.Response]] = []
        while time.monotonic() < deadline:
            rejected = []
            for key, future in zip(keys, futures, strict=True):
                if not future.done():
                    continue
                response = future.result()
                if response.status_code == 429:
                    rejected.append((key, response))
            if len(rejected) >= extra:
                break
            time.sleep(0.05)
        assert rejected, "door cap never returned 429 while Place Order was held in-flight"
        for key, response in rejected:
            assert response.json()["detail"] == "door busy"
            assert _psql(f"SELECT count(*) FROM intake_keys WHERE place_key = '{key}'") == "0"
        assert _accept_counts() == before
    finally:
        release_lock.set()
        locker.join(timeout=5)
        pool.shutdown(wait=True)

    responses = [future.result(timeout=30) for future in futures]
    statuses = [response.status_code for response in responses]
    created = sum(1 for status in statuses if status == 201)
    busy = sum(1 for status in statuses if status == 429)
    assert set(statuses) <= {201, 429}, statuses
    assert created == door_cap
    assert busy == extra
    after = _accept_counts()
    assert after[0] - before[0] == created
    assert after[1] - before[1] == created
    for key, response in zip(keys, responses, strict=True):
        if response.status_code != 429:
            continue
        assert response.json()["detail"] == "door busy"
        assert _psql(f"SELECT count(*) FROM intake_keys WHERE place_key = '{key}'") == "0"
