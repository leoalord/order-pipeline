"""Restaurant menu-item stock: set/restore, atomic reject, replay-safe decrement."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import httpx
import pytest
from fastapi.testclient import TestClient

from order_pipeline.courier.app import build_app as build_courier
from order_pipeline.courier.settings import CSIMSettings
from order_pipeline.menu import MENU_ITEM_IDS
from order_pipeline.restaurant.app import build_app
from order_pipeline.restaurant.settings import RSIMSettings
from order_pipeline.restaurant.stock import BONUS_RESTORE_STOCK, DEFAULT_STOCK, MenuStock
from order_pipeline.sim.ledger import Effect, EffectLedger


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


def _stock(client: TestClient) -> dict[str, int]:
    response = client.get("/admin/stock")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return {str(key): int(value) for key, value in body.items()}


def test_menu_stock_defaults_and_unavailable(tmp_path: Path) -> None:
    stock = MenuStock(EffectLedger(tmp_path / "stock.sqlite"))
    assert stock.snapshot() == {item: DEFAULT_STOCK for item in sorted(MENU_ITEM_IDS)}
    stock.set("burrito", 0)
    assert stock.unavailable(["burrito"])
    assert stock.unavailable(["chips", "burrito"])
    assert not stock.unavailable(["chips"])
    assert stock.snapshot()["chips"] == DEFAULT_STOCK
    assert stock.snapshot()["burrito"] == 0


def test_admin_stock_set_and_restore(client: TestClient) -> None:
    assert _stock(client) == {item: DEFAULT_STOCK for item in sorted(MENU_ITEM_IDS)}
    zeroed = client.post("/admin/stock", json={"item": "burrito", "count": 0})
    assert zeroed.status_code == 200, zeroed.text
    assert zeroed.json()["burrito"] == 0
    assert zeroed.json()["chips"] == DEFAULT_STOCK
    restored = client.post("/admin/stock", json={"item": "burrito", "count": BONUS_RESTORE_STOCK})
    assert restored.status_code == 200, restored.text
    assert restored.json()["burrito"] == BONUS_RESTORE_STOCK
    assert restored.json()["chips"] == DEFAULT_STOCK
    assert restored.json()["taco"] == DEFAULT_STOCK
    unknown = client.post("/admin/stock", json={"item": "chicken_burrito", "count": 0})
    assert unknown.status_code == 422, unknown.text


@pytest.mark.parametrize("count", [True, False, 1.0, "5", 1.5, -1, None, [], {}])
def test_admin_stock_rejects_non_strict_or_invalid_counts(
    client: TestClient, count: object
) -> None:
    before = _stock(client)
    response = client.post("/admin/stock", json={"item": "burrito", "count": count})
    assert response.status_code == 422, response.text
    assert _stock(client) == before


def test_admin_stock_rejects_extra_fields(client: TestClient) -> None:
    response = client.post(
        "/admin/stock",
        json={"item": "burrito", "count": 0, "restore": True},
    )
    assert response.status_code == 422, response.text


def test_fault_clear_does_not_restore_stock(client: TestClient) -> None:
    changed = client.post("/admin/stock", json={"item": "burrito", "count": 7})
    assert changed.status_code == 200, changed.text
    cleared = client.post("/admin/faults", json={"mode": "clear"})
    assert cleared.status_code == 200, cleared.text
    assert _stock(client)["burrito"] == 7


def test_zero_stock_is_business_4xx_and_consumes_nothing(client: TestClient) -> None:
    client.post("/admin/stock", json={"item": "burrito", "count": 0})
    key = f"stock-zero-{uuid.uuid4()}"
    rejected = _accept(client, ["burrito"], key)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"] == "out of stock"
    assert client.get("/admin/ledger").json()["counts"].get(key) is None
    assert _stock(client)["burrito"] == 0


def test_two_item_cart_fails_atomically_when_one_item_is_zero(client: TestClient) -> None:
    client.post("/admin/stock", json={"item": "burrito", "count": 0})
    before = _stock(client)
    key = f"stock-partial-{uuid.uuid4()}"
    rejected = _accept(client, ["chips", "burrito"], key)
    assert rejected.status_code == 409, rejected.text
    assert client.get("/admin/ledger").json()["counts"].get(key) is None
    assert _stock(client) == before
    assert before["chips"] == DEFAULT_STOCK
    assert before["burrito"] == 0


def test_successful_accept_decrements_each_item_once(client: TestClient) -> None:
    key = f"stock-ok-{uuid.uuid4()}"
    accepted = _accept(client, ["burrito", "chips"], key)
    assert accepted.status_code == 200, accepted.text
    counts = _stock(client)
    assert counts["burrito"] == DEFAULT_STOCK - 1
    assert counts["chips"] == DEFAULT_STOCK - 1
    assert counts["taco"] == DEFAULT_STOCK
    assert client.get("/admin/ledger").json()["counts"][key] == 1


def test_repeated_item_multiplicity_is_atomic(client: TestClient) -> None:
    client.post("/admin/stock", json={"item": "burrito", "count": 1})
    rejected_key = f"stock-repeat-no-{uuid.uuid4()}"
    rejected = _accept(client, ["burrito", "burrito"], rejected_key)
    assert rejected.status_code == 409, rejected.text
    assert _stock(client)["burrito"] == 1
    assert client.get("/admin/ledger").json()["counts"].get(rejected_key) is None

    client.post("/admin/stock", json={"item": "burrito", "count": 2})
    accepted_key = f"stock-repeat-ok-{uuid.uuid4()}"
    accepted = _accept(client, ["burrito", "burrito"], accepted_key)
    assert accepted.status_code == 200, accepted.text
    assert _stock(client)["burrito"] == 0
    assert client.get("/admin/ledger").json()["counts"][accepted_key] == 1


def test_replay_same_key_does_not_decrement_again(client: TestClient) -> None:
    key = f"stock-replay-{uuid.uuid4()}"
    first = _accept(client, ["taco"], key)
    assert first.status_code == 200, first.text
    after_first = _stock(client)["taco"]
    second = _accept(client, ["taco"], key)
    assert second.status_code == 200, second.text
    assert second.json()["ticket_id"] == first.json()["ticket_id"]
    assert _stock(client)["taco"] == after_first
    assert client.get("/admin/ledger").json()["counts"][key] == 1


def test_same_key_different_body_does_not_consume_more_stock(client: TestClient) -> None:
    key = f"stock-conflict-{uuid.uuid4()}"
    first = _accept(client, ["chips"], key)
    assert first.status_code == 200, first.text
    after_first = _stock(client)
    conflict = _accept(client, ["taco"], key)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"] == "Idempotency-Key reused with a different body"
    assert _stock(client) == after_first
    assert client.get("/admin/ledger").json()["counts"][key] == 1


def test_concurrent_accepts_cannot_oversell_final_unit(tmp_path: Path, clock: MutableClock) -> None:
    settings = RSIMSettings(
        ledger_path=tmp_path / "final-unit.sqlite",
        flaky_5xx_pct=0.0,
        flaky_drop_pct=0.0,
    )
    barrier = Barrier(2)

    def accept(app: TestClient, key: str) -> tuple[str, int]:
        barrier.wait()
        return key, _accept(app, ["burrito"], key).status_code

    with (
        TestClient(build_app(settings, now_fn=clock, blackout_hang_s=0.0)) as first,
        TestClient(build_app(settings, now_fn=clock, blackout_hang_s=0.0)) as second,
    ):
        first.post("/admin/stock", json={"item": "burrito", "count": 1})
        keys = [f"stock-final-{index}-{uuid.uuid4()}" for index in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(accept, (first, second), keys))

        assert [status for _, status in results].count(200) == 1
        assert [status for _, status in results].count(409) == 1
        assert _stock(first)["burrito"] == 0
        ledger_counts = first.get("/admin/ledger").json()["counts"]
        assert sum(ledger_counts.get(key, 0) for key, _ in results) == 1


def test_concurrent_same_key_replays_without_second_decrement(
    tmp_path: Path, clock: MutableClock
) -> None:
    settings = RSIMSettings(
        ledger_path=tmp_path / "same-key-race.sqlite",
        flaky_5xx_pct=0.0,
        flaky_drop_pct=0.0,
    )
    barrier = Barrier(2)
    key = f"stock-same-race-{uuid.uuid4()}"

    def accept(app: TestClient) -> httpx.Response:
        barrier.wait()
        return _accept(app, ["burrito"], key)

    with (
        TestClient(build_app(settings, now_fn=clock, blackout_hang_s=0.0)) as first,
        TestClient(build_app(settings, now_fn=clock, blackout_hang_s=0.0)) as second,
    ):
        first.post("/admin/stock", json={"item": "burrito", "count": 1})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(accept, (first, second)))

        assert [response.status_code for response in responses] == [200, 200]
        assert responses[0].json()["ticket_id"] == responses[1].json()["ticket_id"]
        assert _stock(first)["burrito"] == 0
        assert first.get("/admin/ledger").json()["counts"][key] == 1


def test_five_xx_before_consumes_no_stock(client: TestClient) -> None:
    client.post("/admin/faults", json={"mode": "5xx_before"})
    key = f"stock-5xx-before-{uuid.uuid4()}"
    failed = _accept(client, ["burrito"], key)
    assert failed.status_code == 500, failed.text
    assert client.get("/admin/ledger").json()["counts"].get(key) is None
    assert _stock(client)["burrito"] == DEFAULT_STOCK
    client.post("/admin/faults", json={"mode": "clear"})
    ok = _accept(client, ["burrito"], key)
    assert ok.status_code == 200, ok.text
    assert _stock(client)["burrito"] == DEFAULT_STOCK - 1


def test_five_xx_after_consumes_once_and_replay_does_not(client: TestClient) -> None:
    client.post("/admin/faults", json={"mode": "5xx_after"})
    key = f"stock-5xx-after-{uuid.uuid4()}"
    failed = _accept(client, ["burrito"], key)
    assert failed.status_code == 500, failed.text
    assert client.get("/admin/ledger").json()["counts"][key] == 1
    assert _stock(client)["burrito"] == DEFAULT_STOCK - 1
    replay = _accept(client, ["burrito"], key)
    assert replay.status_code == 200, replay.text
    assert replay.json()["ticket_id"]
    assert _stock(client)["burrito"] == DEFAULT_STOCK - 1
    assert client.get("/admin/ledger").json()["counts"][key] == 1


def test_drop_consumes_once_and_replay_does_not(client: TestClient) -> None:
    client.post("/admin/faults", json={"mode": "drop"})
    key = f"stock-drop-{uuid.uuid4()}"
    try:
        dropped = _accept(client, ["chips"], key)
    except (httpx.TransportError, RuntimeError, AssertionError):
        pass
    else:
        pytest.fail(f"drop returned a complete response: {dropped.status_code} {dropped.text}")
    assert client.get("/admin/ledger").json()["counts"][key] == 1
    assert _stock(client)["chips"] == DEFAULT_STOCK - 1
    client.post("/admin/faults", json={"mode": "clear"})
    replay = _accept(client, ["chips"], key)
    assert replay.status_code == 200, replay.text
    assert _stock(client)["chips"] == DEFAULT_STOCK - 1


def test_stock_and_effect_survive_restaurant_restart(tmp_path: Path, clock: MutableClock) -> None:
    ledger_path = tmp_path / "restart.sqlite"
    settings = RSIMSettings(
        ledger_path=ledger_path,
        flaky_5xx_pct=0.0,
        flaky_drop_pct=0.0,
    )
    key = f"stock-restart-{uuid.uuid4()}"
    with TestClient(build_app(settings, now_fn=clock, blackout_hang_s=0.0)) as first_app:
        first_app.post("/admin/stock", json={"item": "burrito", "count": 3})
        accepted = _accept(first_app, ["burrito"], key)
        assert accepted.status_code == 200, accepted.text
        ticket_id = accepted.json()["ticket_id"]
        assert _stock(first_app)["burrito"] == 2

    with TestClient(build_app(settings, now_fn=clock, blackout_hang_s=0.0)) as restarted:
        assert _stock(restarted)["burrito"] == 2
        replay = _accept(restarted, ["burrito"], key)
        assert replay.status_code == 200, replay.text
        assert replay.json()["ticket_id"] == ticket_id
        assert _stock(restarted)["burrito"] == 2
        assert restarted.get("/admin/ledger").json()["counts"][key] == 1


def test_ledger_insert_failure_rolls_back_stock(
    tmp_path: Path,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_path = tmp_path / "insert-failure.sqlite"
    duplicate_ticket_id = uuid.uuid4()
    seeded = EffectLedger(ledger_path)
    assert seeded.insert(
        Effect(
            idempotency_key=f"seed-{uuid.uuid4()}",
            ticket_id=str(duplicate_ticket_id),
            accepted_at=clock.now,
            estimated_ready_at=clock.now + timedelta(seconds=12),
            payload={"items": ["chips"], "quiet_cook_s": 12.0},
        )
    )
    settings = RSIMSettings(
        ledger_path=ledger_path,
        flaky_5xx_pct=0.0,
        flaky_drop_pct=0.0,
    )
    monkeypatch.setattr("order_pipeline.sim.core.uuid4", lambda: duplicate_ticket_id)
    with TestClient(build_app(settings, now_fn=clock, blackout_hang_s=0.0)) as app:
        before = _stock(app)
        key = f"stock-insert-failure-{uuid.uuid4()}"
        failed = _accept(app, ["burrito"], key)
        assert failed.status_code == 500, failed.text
        assert failed.json()["detail"] == "ledger insert failed"
        assert _stock(app) == before
        assert app.get("/admin/ledger").json()["counts"].get(key) is None


def test_courier_has_no_stock_routes(tmp_path: Path, clock: MutableClock) -> None:
    settings = CSIMSettings(
        ledger_path=tmp_path / "courier.sqlite",
        flaky_5xx_pct=0.0,
        flaky_drop_pct=0.0,
    )
    app = build_courier(settings, now_fn=clock, blackout_hang_s=0.0)
    with TestClient(app) as courier:
        missing_get = courier.get("/admin/stock")
        assert missing_get.status_code == 404
        missing_post = courier.post("/admin/stock", json={"item": "burrito", "count": 0})
        assert missing_post.status_code == 404
        accepted = courier.post(
            "/accept",
            json={"band": "near"},
            headers={"Idempotency-Key": f"csim-no-stock-{uuid.uuid4()}"},
        )
        assert accepted.status_code == 200, accepted.text
