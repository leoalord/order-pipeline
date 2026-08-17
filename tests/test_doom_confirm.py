"""Deterministic loadgen doom-confirm coordinator and HTTP endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from order_pipeline.intake import confirm_idempotency_key
from order_pipeline.loadgen.app import create_app
from order_pipeline.loadgen.doom_confirm import (
    DOOM_CONFIRM_COUNT,
    ConfirmUnavailableTarget,
    DoomConfirmFixture,
    PlacedOrder,
)
from order_pipeline.loadgen.settings import LoadgenSettings


class IdlePipeline:
    async def place(self, *, items: list[str], cohort_id: UUID, place_key: str) -> int:
        del items, cohort_id, place_key
        return 201

    async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
        del cohort_id
        return {}

    async def aclose(self) -> None:
        return None


class FakeDoomClient:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
        self.orders: dict[UUID, PlacedOrder] = {}
        self.replacements: list[list[ConfirmUnavailableTarget]] = []
        self.closed = False

    async def place(
        self,
        *,
        items: list[str],
        cohort_id: UUID,
        place_key: str,
    ) -> PlacedOrder:
        del cohort_id, place_key
        assert items == ["chips"]
        order = PlacedOrder(id=uuid4(), state="placed", accepted_at=self.now)
        self.now += timedelta(milliseconds=1)
        self.orders[order.id] = order
        return order

    async def get_order(self, order_id: UUID) -> PlacedOrder:
        return self.orders[order_id]

    async def replace_confirm_unavailable(
        self,
        targets: list[ConfirmUnavailableTarget],
    ) -> None:
        self.replacements.append(list(targets))

    async def aclose(self) -> None:
        self.closed = True


def test_fixture_arms_exact_stored_keys_until_each_accepted_at_plus_deadline() -> None:
    fake = FakeDoomClient()
    fixture = DoomConfirmFixture(fake, confirm_deadline_s=120)
    cohort_id = uuid4()

    async def run() -> None:
        result = await fixture.create(cohort_id)
        assert len(result.order_ids) == DOOM_CONFIRM_COUNT
        assert result.cohort_id == cohort_id

    import asyncio

    asyncio.run(run())

    assert fake.replacements[0] == []
    targets = fake.replacements[1]
    assert len(targets) == DOOM_CONFIRM_COUNT
    for target in targets:
        order_id = next(
            order_id
            for order_id in fake.orders
            if confirm_idempotency_key(order_id) == target.idempotency_key
        )
        assert target.until == fake.orders[order_id].accepted_at + timedelta(seconds=120)


def test_http_beat_returns_three_ids_and_new_cohort_cleans_targets() -> None:
    pipeline = IdlePipeline()
    doom = FakeDoomClient()
    app = create_app(LoadgenSettings(), client=pipeline, doom_client=doom)

    with TestClient(app) as client:
        response = client.post("/beat/doom-confirm")
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["order_ids"]) == DOOM_CONFIRM_COUNT
        assert all(UUID(value) in doom.orders for value in body["order_ids"])
        assert body["cohort_id"] == str(app.state.driver.cohort_id)
        assert doom.replacements[-1]

        minted = client.post("/cohort/new")
        assert minted.status_code == 200, minted.text
        assert UUID(minted.json()["cohort_id"]) == app.state.driver.cohort_id
        assert doom.replacements[-1] == []

    assert doom.closed is True


def test_fixture_aborts_and_cleans_if_a_tagged_order_already_confirmed() -> None:
    class RacingDoomClient(FakeDoomClient):
        async def get_order(self, order_id: UUID) -> PlacedOrder:
            order = self.orders[order_id]
            return PlacedOrder(id=order.id, state="confirmed", accepted_at=order.accepted_at)

    doom = RacingDoomClient()
    app = create_app(LoadgenSettings(), client=IdlePipeline(), doom_client=doom)

    with TestClient(app) as client:
        response = client.post("/beat/doom-confirm")

    assert response.status_code == 409, response.text
    assert "confirmed before doom-confirm rule landed" in response.json()["detail"]
    assert doom.replacements[-1] == []
