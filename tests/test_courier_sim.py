"""In-process courier sim: trip hang-up, Stripe replay, poll, admin faults."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from order_pipeline.courier.app import build_app
from order_pipeline.courier.settings import CSIMSettings


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
    settings = CSIMSettings(
        ledger_path=tmp_path / "ledger.sqlite",
        flaky_5xx_pct=0.0,
        flaky_drop_pct=0.0,
    )
    app = build_app(settings, now_fn=clock, blackout_hang_s=0.0)
    with TestClient(app) as test_client:
        yield test_client


def _dispatch(client: TestClient, band: str, key: str) -> httpx.Response:
    response = client.post(
        "/accept",
        json={"band": band},
        headers={"Idempotency-Key": key},
    )
    assert isinstance(response, httpx.Response)
    return response


def test_dispatch_returns_ticket_under_two_seconds(client: TestClient) -> None:
    key = f"unit-hangup-{uuid.uuid4()}"
    started = time.monotonic()
    response = _dispatch(client, "far", key)
    elapsed = time.monotonic() - started
    assert response.status_code == 200, response.text
    body = response.json()
    assert "ticket_id" in body
    assert body["accepted_at"]
    assert body["estimated_ready_at"]
    assert body["status"] == "en_route"
    assert body["service_started_at"]
    assert elapsed < 2.0


def test_replay_same_key_is_one_ledger_row(client: TestClient) -> None:
    key = f"unit-replay-{uuid.uuid4()}"
    first = _dispatch(client, "mid", key)
    assert first.status_code == 200, first.text
    second = _dispatch(client, "mid", key)
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


def test_poll_assigned_en_route_then_delivered(client: TestClient, clock: MutableClock) -> None:
    key = f"unit-poll-{uuid.uuid4()}"
    accepted = _dispatch(client, "near", key)
    assert accepted.status_code == 200, accepted.text
    ticket_id = accepted.json()["ticket_id"]
    eta = datetime.fromisoformat(accepted.json()["estimated_ready_at"].replace("Z", "+00:00"))
    accepted_at = clock.now

    clock.now = accepted_at - timedelta(seconds=1)
    assigned = client.get(f"/tickets/{ticket_id}")
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["status"] == "assigned"

    clock.now = accepted_at
    en_route = client.get(f"/tickets/{ticket_id}")
    assert en_route.json()["status"] == "en_route"

    clock.now = eta + timedelta(seconds=1)
    delivered = client.get(f"/tickets/{ticket_id}")
    assert delivered.json()["status"] == "delivered"


def test_second_dispatch_waits_when_the_only_bike_is_busy(
    tmp_path: Path, clock: MutableClock
) -> None:
    settings = CSIMSettings(
        ledger_path=tmp_path / "one-bike.sqlite",
        flaky_5xx_pct=0.0,
        flaky_drop_pct=0.0,
        fleet_size=1,
    )
    app = build_app(settings, now_fn=clock, blackout_hang_s=0.0)
    with TestClient(app) as client:
        first = _dispatch(client, "near", f"bike-1-{uuid.uuid4()}")
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "en_route"
        second = _dispatch(client, "near", f"bike-2-{uuid.uuid4()}")
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "assigned"
        first_eta = datetime.fromisoformat(
            first.json()["estimated_ready_at"].replace("Z", "+00:00")
        )
        second_start = datetime.fromisoformat(
            second.json()["service_started_at"].replace("Z", "+00:00")
        )
        assert second_start == first_eta


def test_five_xx_before_writes_no_ledger_row(client: TestClient) -> None:
    set_mode = client.post("/admin/faults", json={"mode": "5xx_before"})
    assert set_mode.status_code == 200
    assert set_mode.json()["mode"] == "5xx_before"

    key = f"unit-5xx-before-{uuid.uuid4()}"
    failed = _dispatch(client, "near", key)
    assert failed.status_code == 500
    assert client.get("/admin/ledger").json()["counts"].get(key) is None

    client.post("/admin/faults", json={"mode": "clear"})
    ok = _dispatch(client, "near", key)
    assert ok.status_code == 200
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
    assert body["blackout_remaining_s"] == 0

    fetched = client.get("/admin/faults")
    assert fetched.json()["mix"] == "off"
    assert fetched.json()["mode"] == "off"


def test_fail_void_is_rejected_without_changing_courier_faults(
    client: TestClient,
) -> None:
    before = client.get("/admin/faults").json()
    response = client.post("/admin/faults", json={"mode": "fail_void"})
    assert response.status_code == 422
    assert response.json()["detail"] == "fail_void is restaurant-only"
    assert client.get("/admin/faults").json() == before


def test_blackout_post_get_then_expires(client: TestClient, clock: MutableClock) -> None:
    armed = client.post("/admin/faults", json={"mode": "blackout", "seconds": 2})
    assert armed.status_code == 200, armed.text
    body = armed.json()
    assert body["mode"] == "blackout"
    assert body["blackout_remaining_s"] == pytest.approx(2.0)
    fetched = client.get("/admin/faults")
    assert fetched.json()["mode"] == "blackout"
    assert fetched.json()["blackout_remaining_s"] > 0

    key = f"unit-blackout-{uuid.uuid4()}"
    try:
        dropped = _dispatch(client, "near", key)
    except (httpx.TransportError, RuntimeError, AssertionError):
        pass
    else:
        pytest.fail(f"blackout returned a complete response: {dropped.status_code} {dropped.text}")
    assert client.get("/admin/ledger").json()["counts"].get(key) is None

    clock.now = clock.now + timedelta(seconds=2)
    expired = client.get("/admin/faults")
    assert expired.json()["mode"] == "off"
    assert expired.json()["blackout_remaining_s"] == 0
    ok = _dispatch(client, "near", key)
    assert ok.status_code == 200, ok.text


def test_default_settings_show_mix_on(tmp_path: Path) -> None:
    settings = CSIMSettings(ledger_path=tmp_path / "default-mix.sqlite")
    assert settings.flaky_5xx_pct == 3.0
    assert settings.flaky_drop_pct == 2.0
    app = build_app(settings)
    with TestClient(app) as test_client:
        fetched = test_client.get("/admin/faults")
        assert fetched.status_code == 200
        body = fetched.json()
        assert body["mix"] == "on"
        assert body["flaky_5xx_pct"] == 3.0
        assert body["flaky_drop_pct"] == 2.0
        assert body["mode"] == "off"
        assert body["blackout_remaining_s"] == 0
        off = test_client.post("/admin/faults", json={"mode": "clear", "mix": "off"})
        assert off.json()["mix"] == "off"
        on = test_client.post("/admin/faults", json={"mode": "clear", "mix": "on"})
        assert on.json()["mix"] == "on"
        assert on.json()["flaky_5xx_pct"] == 3.0
