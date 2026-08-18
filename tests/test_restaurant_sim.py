"""In-process restaurant sim: quote hang-up, Stripe replay, poll, admin faults."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from order_pipeline.restaurant.app import build_app
from order_pipeline.restaurant.quote import quote_accept
from order_pipeline.restaurant.settings import RSIMSettings
from order_pipeline.sim.core import Quote, SimCore
from order_pipeline.sim.faults import FaultState
from order_pipeline.sim.ledger import EffectLedger


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
    settings = RSIMSettings(
        ledger_path=tmp_path / "ledger.sqlite",
        flaky_5xx_pct=0.0,
        flaky_drop_pct=0.0,
    )
    app = build_app(settings, now_fn=clock, blackout_hang_s=0.0)
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
    assert body["accepted_at"]
    assert body["estimated_ready_at"]
    assert body["status"] == "cooking"
    assert body["service_started_at"]
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


def test_second_ticket_queues_when_the_only_pan_is_busy(
    tmp_path: Path, clock: MutableClock
) -> None:
    settings = RSIMSettings(
        ledger_path=tmp_path / "one-pan.sqlite",
        flaky_5xx_pct=0.0,
        flaky_drop_pct=0.0,
        kitchen_pans=1,
        rail_fuse=80,
    )
    app = build_app(settings, now_fn=clock, blackout_hang_s=0.0)
    with TestClient(app) as client:
        first = _accept(client, ["burrito"], f"pan-1-{uuid.uuid4()}")
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "cooking"
        second = _accept(client, ["burrito"], f"pan-2-{uuid.uuid4()}")
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "queued"
        first_eta = datetime.fromisoformat(
            first.json()["estimated_ready_at"].replace("Z", "+00:00")
        )
        second_start = datetime.fromisoformat(
            second.json()["service_started_at"].replace("Z", "+00:00")
        )
        assert second_start == first_eta


def test_concurrent_accepts_reserve_no_more_than_twenty_pans(
    tmp_path: Path, clock: MutableClock
) -> None:
    ledger = EffectLedger(tmp_path / "concurrent-rail.sqlite")
    cook_s = RSIMSettings().cook_s.as_map()

    def quote(body: dict[str, Any], now: datetime) -> Quote:
        occupancy = [
            (effect.estimated_ready_at - timedelta(seconds=12), effect.estimated_ready_at)
            for effect in ledger.list_effects()
            if effect.estimated_ready_at > now
        ]
        # Make an unlocked read/quote/insert sequence race deterministically.
        time.sleep(0.005)
        return quote_accept(
            body,
            now,
            cook_s=cook_s,
            extra_item_s=5.0,
            pans=20,
            occupancy=occupancy,
        )

    core = SimCore(
        ledger=ledger,
        faults=FaultState(now_fn=clock),
        quote=quote,
        status_at=lambda **_kwargs: "cooking",
        flaky_5xx_pct=0.0,
        flaky_drop_pct=0.0,
        now_fn=clock,
    )

    with ThreadPoolExecutor(max_workers=40) as pool:
        outcomes = list(
            pool.map(
                lambda index: core.accept(f"concurrent-{index}", {"items": ["chips"]}),
                range(40),
            )
        )
    assert all(outcome.status_code == 200 for outcome in outcomes)

    one_second_in = clock.now + timedelta(seconds=1)
    active = 0
    for effect in ledger.list_effects():
        started_at = effect.estimated_ready_at - timedelta(seconds=12)
        if started_at <= one_second_in < effect.estimated_ready_at:
            active += 1
    assert active == 20


def test_busy_429_then_replay_of_earlier_key_still_succeeds(
    tmp_path: Path, clock: MutableClock
) -> None:
    settings = RSIMSettings(
        ledger_path=tmp_path / "busy.sqlite",
        flaky_5xx_pct=0.0,
        flaky_drop_pct=0.0,
        kitchen_pans=1,
        busy_multiple=3,
        rail_fuse=80,
    )
    app = build_app(settings, now_fn=clock, blackout_hang_s=0.0)
    with TestClient(app) as client:
        first_key = f"busy-first-{uuid.uuid4()}"
        first = _accept(client, ["burrito"], first_key)
        assert first.status_code == 200, first.text
        # Fill the 3× window: wait grows by 25s per extra burrito on one pan.
        for i in range(2):
            filled = _accept(client, ["burrito"], f"busy-fill-{i}-{uuid.uuid4()}")
            assert filled.status_code == 200, filled.text
        busy = _accept(client, ["burrito"], f"busy-no-{uuid.uuid4()}")
        assert busy.status_code == 429, busy.text
        replay = _accept(client, ["burrito"], first_key)
        assert replay.status_code == 200, replay.text
        assert replay.json()["ticket_id"] == first.json()["ticket_id"]
        assert client.get("/admin/ledger").json()["counts"][first_key] == 1


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
    assert fetched.json()["blackout_remaining_s"] == 0

    missing = client.post("/admin/faults", json={"mode": "blackout"})
    assert missing.status_code == 422


def test_blackout_post_get_then_expires(client: TestClient, clock: MutableClock) -> None:
    existing_key = f"unit-blackout-existing-{uuid.uuid4()}"
    existing = _accept(client, ["chips"], existing_key)
    assert existing.status_code == 200
    existing_ticket_id = existing.json()["ticket_id"]

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
        dropped = _accept(client, ["chips"], key)
    except (httpx.TransportError, RuntimeError, AssertionError):
        pass
    else:
        pytest.fail(f"blackout returned a complete response: {dropped.status_code} {dropped.text}")
    assert client.get("/admin/ledger").json()["counts"].get(key) is None

    for path in (f"/keys/{existing_key}", f"/tickets/{existing_ticket_id}"):
        try:
            response = client.get(path)
        except (httpx.TransportError, RuntimeError, AssertionError):
            pass
        else:
            pytest.fail(
                f"blackout let dependency polling complete: {response.status_code} {response.text}"
            )

    clock.now = clock.now + timedelta(seconds=2)
    expired = client.get("/admin/faults")
    assert expired.json()["mode"] == "off"
    assert expired.json()["blackout_remaining_s"] == 0
    ok = _accept(client, ["chips"], key)
    assert ok.status_code == 200, ok.text


def test_default_settings_show_mix_on(tmp_path: Path) -> None:
    settings = RSIMSettings(ledger_path=tmp_path / "default-mix.sqlite")
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
        assert off.json()["flaky_5xx_pct"] == 0.0
        on = test_client.post("/admin/faults", json={"mode": "clear", "mix": "on"})
        assert on.json()["mix"] == "on"
        assert on.json()["flaky_5xx_pct"] == 3.0


def test_void_replays_and_fail_void_500s_until_clear(client: TestClient) -> None:
    accept_key = f"unit-void-accept-{uuid.uuid4()}"
    void_key = f"unit-void-{uuid.uuid4()}"
    accepted = _accept(client, ["chips"], accept_key)
    assert accepted.status_code == 200, accepted.text
    ticket_id = accepted.json()["ticket_id"]

    first = client.post(
        "/void",
        json={"accept_key": accept_key, "ticket_id": ticket_id},
        headers={"Idempotency-Key": void_key},
    )
    assert first.status_code == 200, first.text
    assert first.json()["voided"] is True
    second = client.post(
        "/void",
        json={"accept_key": accept_key, "ticket_id": ticket_id},
        headers={"Idempotency-Key": void_key},
    )
    assert second.status_code == 200, second.text
    ledger = client.get("/admin/ledger")
    assert ledger.json()["counts"][accept_key] == 1
    assert ledger.json()["counts"][void_key] == 1

    original_replay = _accept(client, ["chips"], accept_key)
    assert original_replay.status_code == 200, original_replay.text
    assert original_replay.json()["ticket_id"] == ticket_id

    armed = client.post("/admin/faults", json={"mode": "fail_void"})
    assert armed.status_code == 200, armed.text
    assert armed.json()["mode"] == "fail_void"
    still_accepts = _accept(client, ["taco"], f"unit-void-accept-2-{uuid.uuid4()}")
    assert still_accepts.status_code == 200, still_accepts.text

    cached = client.post(
        "/void",
        json={"accept_key": accept_key, "ticket_id": ticket_id},
        headers={"Idempotency-Key": void_key},
    )
    assert cached.status_code == 200, cached.text
    assert cached.json() == first.json()

    failed = client.post(
        "/void",
        json={"accept_key": accept_key},
        headers={"Idempotency-Key": f"unit-void-fail-{uuid.uuid4()}"},
    )
    assert failed.status_code == 500, failed.text

    absent = client.post(
        "/void",
        json={"accept_key": f"unit-never-applied-{uuid.uuid4()}"},
        headers={"Idempotency-Key": f"unit-void-absent-{uuid.uuid4()}"},
    )
    assert absent.status_code == 200, absent.text
    assert absent.json()["voided"] is False
    assert absent.json()["absent"] is True

    conflict = client.post(
        "/void",
        json={"accept_key": "different-confirm"},
        headers={"Idempotency-Key": void_key},
    )
    assert conflict.status_code == 409, conflict.text
    cleared = client.post("/admin/faults", json={"mode": "clear"})
    assert cleared.json()["mode"] == "off"
