"""Restaurant menu-item stock: set/restore, atomic reject, replay-safe decrement."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from order_pipeline.courier.app import build_app as build_courier
from order_pipeline.courier.settings import CSIMSettings
from order_pipeline.menu import MENU_ITEM_IDS
from order_pipeline.restaurant.app import build_app
from order_pipeline.restaurant.settings import RSIMSettings
from order_pipeline.restaurant.stock import DEFAULT_STOCK, MenuStock


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


def test_menu_stock_defaults_and_atomic_unavailable() -> None:
    stock = MenuStock()
    assert stock.snapshot() == {item: DEFAULT_STOCK for item in sorted(MENU_ITEM_IDS)}
    stock.set("burrito", 0)
    assert stock.unavailable(["burrito"])
    assert stock.unavailable(["chips", "burrito"])
    assert not stock.unavailable(["chips"])
    with pytest.raises(RuntimeError, match="out-of-stock"):
        stock.decrement(["chips", "burrito"])
    assert stock.snapshot()["chips"] == DEFAULT_STOCK
    assert stock.snapshot()["burrito"] == 0


def test_admin_stock_set_and_restore(client: TestClient) -> None:
    assert _stock(client) == {item: DEFAULT_STOCK for item in sorted(MENU_ITEM_IDS)}
    zeroed = client.post("/admin/stock", json={"item": "burrito", "count": 0})
    assert zeroed.status_code == 200, zeroed.text
    assert zeroed.json()["burrito"] == 0
    assert zeroed.json()["chips"] == DEFAULT_STOCK
    restored = client.post("/admin/stock", json={"item": "burrito", "count": 200})
    assert restored.status_code == 200, restored.text
    assert restored.json() == {item: DEFAULT_STOCK for item in sorted(MENU_ITEM_IDS)}
    unknown = client.post("/admin/stock", json={"item": "chicken_burrito", "count": 0})
    assert unknown.status_code == 422, unknown.text


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
