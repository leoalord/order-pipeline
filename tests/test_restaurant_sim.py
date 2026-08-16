"""In-process restaurant sim: quote hang-up, Stripe replay, poll, admin faults."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from order_pipeline.restaurant.app import build_app
from order_pipeline.restaurant.settings import RSIMSettings


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(datetime(2026, 8, 16, 12, 0, tzinfo=UTC))


@pytest.fixture
def client(tmp_path: Path, clock: MutableClock) -> Iterator[TestClient]:
    settings = RSIMSettings(ledger_path=tmp_path / "ledger.sqlite")
    app = build_app(settings, now_fn=clock)
    with TestClient(app) as test_client:
        yield test_client


def _accept(client: TestClient, items: list[str], key: str) -> httpx.Response:
    response = client.post(
        "/accept",
        json={"items": items},
        headers={"Idempotency-Key": key},
    )
    assert isinstance(response, httpx.Response)
    return response


def test_accept_returns_ticket_under_two_seconds(client: TestClient) -> None:
    key = f"unit-hangup-{uuid.uuid4()}"
    started = time.monotonic()
    response = _accept(client, ["burrito"], key)
    elapsed = time.monotonic() - started
    assert response.status_code == 200, response.text
    body = response.json()
    assert "ticket_id" in body
    assert body["estimated_ready_at"]
    assert body["status"] == "cooking"
    assert elapsed < 2.0


def test_replay_same_key_is_one_ledger_row(client: TestClient) -> None:
    key = f"unit-replay-{uuid.uuid4()}"
    first = _accept(client, ["taco", "chips"], key)
    assert first.status_code == 200, first.text
    second = _accept(client, ["taco", "chips"], key)
    assert second.status_code == 200, second.text
    assert first.json()["ticket_id"] == second.json()["ticket_id"]
    assert first.json()["estimated_ready_at"] == second.json()["estimated_ready_at"]

    by_key = client.get(f"/keys/{key}")
    assert by_key.status_code == 200, by_key.text
    assert by_key.json()["ticket_id"] == first.json()["ticket_id"]

    ledger = client.get("/admin/ledger")
    assert ledger.status_code == 200, ledger.text
    counts = ledger.json()["counts"]
    assert counts[key] == 1
    assert list(counts.values()).count(1) >= 1


def test_poll_cooking_then_ready(client: TestClient, clock: MutableClock) -> None:
    key = f"unit-poll-{uuid.uuid4()}"
    accepted = _accept(client, ["chips"], key)
    assert accepted.status_code == 200, accepted.text
    ticket_id = accepted.json()["ticket_id"]
    eta = datetime.fromisoformat(accepted.json()["estimated_ready_at"].replace("Z", "+00:00"))

    cooking = client.get(f"/tickets/{ticket_id}")
    assert cooking.status_code == 200, cooking.text
    assert cooking.json()["status"] == "cooking"

    clock.now = eta + timedelta(seconds=1)
    ready = client.get(f"/tickets/{ticket_id}")
    assert ready.json()["status"] == "ready"


def test_five_xx_before_writes_no_ledger_row(client: TestClient) -> None:
    set_mode = client.post("/admin/faults", json={"mode": "5xx_before"})
    assert set_mode.status_code == 200
    assert set_mode.json()["mode"] == "5xx_before"

    key = f"unit-5xx-before-{uuid.uuid4()}"
    failed = _accept(client, ["burrito"], key)
    assert failed.status_code == 500
    assert client.get("/admin/ledger").json()["counts"].get(key) is None

    client.post("/admin/faults", json={"mode": "clear"})
    ok = _accept(client, ["burrito"], key)
    assert ok.status_code == 200
    assert client.get("/admin/ledger").json()["counts"][key] == 1


def test_five_xx_after_then_replay_is_one_ledger_row(client: TestClient) -> None:
    client.post("/admin/faults", json={"mode": "5xx_after"})
    key = f"unit-5xx-after-{uuid.uuid4()}"
    failed = _accept(client, ["taco"], key)
    assert failed.status_code == 500
    assert client.get("/admin/ledger").json()["counts"][key] == 1

    replay = _accept(client, ["taco"], key)
    assert replay.status_code == 200, replay.text
    assert client.get("/admin/ledger").json()["counts"][key] == 1
    assert client.get(f"/keys/{key}").status_code == 200


def test_drop_applies_effect_without_body(client: TestClient) -> None:
    client.post("/admin/faults", json={"mode": "drop"})
    key = f"unit-drop-{uuid.uuid4()}"
    try:
        dropped = _accept(client, ["chips"], key)
    except (httpx.TransportError, RuntimeError, AssertionError):
        pass
    else:
        pytest.fail(f"drop returned a complete response: {dropped.status_code} {dropped.text}")
    assert client.get("/admin/ledger").json()["counts"][key] == 1

    client.post("/admin/faults", json={"mode": "clear"})
    replay = _accept(client, ["chips"], key)
    assert replay.status_code == 200
    assert client.get("/admin/ledger").json()["counts"][key] == 1


def test_clear_shows_mix_off(client: TestClient) -> None:
    client.post("/admin/faults", json={"mode": "5xx_before"})
    cleared = client.post("/admin/faults", json={"mode": "clear"})
    assert cleared.status_code == 200
    body = cleared.json()
    assert body["mode"] == "off"
    assert body["mix"] == "off"
    assert body["flaky_5xx_pct"] == 0.0
    assert body["flaky_drop_pct"] == 0.0

    fetched = client.get("/admin/faults")
    assert fetched.json()["mix"] == "off"
    assert fetched.json()["mode"] == "off"
