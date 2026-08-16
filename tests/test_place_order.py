"""Compose-backed POST /orders: 201, replay, 409, zero rows on bad carts, one-commit."""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

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


def _table_counts() -> dict[str, int]:
    row = _psql(
        "SELECT (SELECT count(*) FROM orders), "
        "(SELECT count(*) FROM order_events), "
        "(SELECT count(*) FROM work_items), "
        "(SELECT count(*) FROM intake_keys), "
        "(SELECT count(*) FROM attempts)"
    )
    orders, events, work, keys, attempts = (int(part) for part in row.split("|"))
    return {
        "orders": orders,
        "order_events": events,
        "work_items": work,
        "intake_keys": keys,
        "attempts": attempts,
    }


def _post(
    items: list[str],
    place_key: str,
    *,
    cohort_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
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
        return httpx.post(f"{API_URL}/orders", json=payload, headers=headers, timeout=5.0)
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
    assert got["state"] == "placed"
    assert got["accepted_at"]
    assert got["items"] == ["burrito"]
    assert got["cohort_id"] == str(DEFAULT_COHORT_ID)

    work = _psql(
        f"SELECT type, status, idempotency_key FROM work_items WHERE order_id = '{order_id}'"
    )
    work_type, status, stored_key = work.split("|")
    assert work_type == "confirm"
    assert status == "pending"
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
    before = _table_counts()
    place_key = f"test-bad-{uuid.uuid4()}"
    response = _post(kwargs["items"], place_key)  # type: ignore[arg-type]
    assert 400 <= response.status_code < 500, response.text
    assert _table_counts() == before


def test_missing_place_key_creates_zero_rows() -> None:
    before = _table_counts()
    try:
        response = httpx.post(
            f"{API_URL}/orders",
            json={"items": ["burrito"]},
            headers={"Content-Type": "application/json"},
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")
    assert 400 <= response.status_code < 500, response.text
    assert _table_counts() == before


def test_malformed_json_creates_zero_rows() -> None:
    before = _table_counts()
    try:
        response = httpx.post(
            f"{API_URL}/orders",
            content=b"{not-json",
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": f"test-malformed-{uuid.uuid4()}",
            },
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")
    assert 400 <= response.status_code < 500, response.text
    assert _table_counts() == before


def test_timeline_b_no_order_without_confirm_work_item() -> None:
    place_key = f"test-timeline-b-{uuid.uuid4()}"
    created = _post(["taco"], place_key)
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]
    work_count = _psql(
        "SELECT count(*) FROM work_items "
        f"WHERE order_id = '{order_id}' AND type = 'confirm' AND status = 'pending'"
    )
    assert work_count == "1"
    orphans = _psql(
        "SELECT count(*) FROM orders o WHERE NOT EXISTS ("
        "SELECT 1 FROM work_items w WHERE w.order_id = o.id AND w.type = 'confirm'"
        ")"
    )
    assert orphans == "0"
    attempts = _psql(
        "SELECT count(*) FROM attempts a "
        "JOIN work_items w ON w.id = a.work_item_id "
        f"WHERE w.order_id = '{order_id}'"
    )
    assert attempts == "0"


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


def test_get_missing_order_is_404() -> None:
    missing = uuid.uuid4()
    try:
        response = httpx.get(f"{API_URL}/orders/{missing}", timeout=5.0)
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")
    assert response.status_code == 404
