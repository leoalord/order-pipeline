"""Open-loop loadgen: never slows on 429; calibrate reports H + 429 mix."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from order_pipeline.loadgen.app import create_app
from order_pipeline.loadgen.carts import pick_cart
from order_pipeline.loadgen.driver import (
    OpenLoopDriver,
    backlog_total,
    http_429s_from_snapshot,
    step_is_flat,
)
from order_pipeline.loadgen.settings import LoadgenSettings
from order_pipeline.menu import MENU_ITEM_IDS


class FakePipeline:
    def __init__(self, *, place_status: int = 201, delay_s: float = 0.0) -> None:
        self.place_status = place_status
        self.delay_s = delay_s
        self.places: list[tuple[list[str], UUID]] = []
        self.snapshots = 0
        self.backlog_end = 0

    async def place(self, *, items: list[str], cohort_id: UUID, place_key: str) -> int:
        del place_key
        self.places.append((list(items), cohort_id))
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return self.place_status

    async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
        del cohort_id
        self.snapshots += 1
        backlog = 0 if self.snapshots == 1 else self.backlog_end
        return {
            "backlog": {
                "confirm": backlog,
                "poll_cook": 0,
                "dispatch": 0,
                "poll_ride": 0,
            },
            "oldest_open": {"age_s": 1.0, "stage": "placed"},
            "http_429s": {"door": 0, "kitchen": 2, "courier": 1},
        }

    async def aclose(self) -> None:
        return None


def test_cart_mix_is_one_to_three_menu_items() -> None:
    rng = random.Random(0)
    seen_n = set()
    for _ in range(200):
        cart = pick_cart(rng)
        assert 1 <= len(cart) <= 3
        assert all(item in MENU_ITEM_IDS for item in cart)
        seen_n.add(len(cart))
    assert seen_n == {1, 2, 3}
    ones = sum(1 for seed in range(400) if len(pick_cart(random.Random(seed))) == 1)
    # Mostly 1-item (~70%); 400 trials should land well above half.
    assert ones > 200


def test_step_flatness_and_snapshot_helpers() -> None:
    assert step_is_flat(start_backlog=4, end_backlog=5)
    assert step_is_flat(start_backlog=0, end_backlog=3)
    assert not step_is_flat(start_backlog=5, end_backlog=6)
    assert not step_is_flat(start_backlog=40, end_backlog=54)
    assert not step_is_flat(start_backlog=4, end_backlog=20)
    snap = {
        "backlog": {"confirm": 2, "poll_cook": 3, "dispatch": 1, "poll_ride": 0},
        "http_429s": {"door": 1, "kitchen": 4, "courier": 2},
    }
    assert backlog_total(snap) == 6
    assert http_429s_from_snapshot(snap) == {"door": 1, "kitchen": 4, "courier": 2}


def test_open_loop_does_not_slow_on_429() -> None:
    fake = FakePipeline(place_status=429, delay_s=0.15)
    settings = LoadgenSettings(calibrate_step_s=0.2)
    driver = OpenLoopDriver(settings, fake, rng=random.Random(1))

    async def run() -> None:
        await driver.start()
        driver.set_rate(40.0)
        await asyncio.sleep(0.35)
        driver.stop()
        await asyncio.sleep(0.05)
        await driver.aclose()

    asyncio.run(run())
    # Closed-loop would manage ~2 fires (0.35 / 0.15). Open-loop keeps the 40/s clock.
    assert len(fake.places) >= 8
    assert driver.rejected_429 >= 8
    assert driver.placed == 0


def test_rate_change_starts_a_new_clock_and_stop_fires_nothing_late() -> None:
    fake = FakePipeline()
    driver = OpenLoopDriver(LoadgenSettings(), fake, rng=random.Random(2))

    async def run() -> None:
        await driver.start()
        driver.set_rate(10.0)
        await asyncio.sleep(0.45)
        before_change = len(fake.places)

        driver.set_rate(40.0)
        changed_at = time.monotonic()
        await asyncio.sleep(0.03)
        just_after_change = len(fake.places)
        # A fresh 40/s clock emits at most the immediate arrival plus one tick.
        # Reusing the old origin produces a double-digit catch-up burst here.
        assert 1 <= just_after_change - before_change <= 3
        assert time.monotonic() - changed_at < 0.1

        driver.stop()
        await asyncio.sleep(0.02)
        stopped_at = len(fake.places)
        await asyncio.sleep(0.15)
        assert len(fake.places) == stopped_at
        await driver.aclose()

    asyncio.run(run())


def test_calibrate_reports_h_and_429_mix() -> None:
    fake = FakePipeline()
    settings = LoadgenSettings(
        calibrate_step_s=0.05,
        calibrate_start_rps=1.0,
        calibrate_factor=2.0,
        calibrate_max_rps=2.0,
    )
    driver = OpenLoopDriver(settings, fake)

    async def run() -> dict[str, Any]:
        await driver.start()
        result = await driver.calibrate(step_s=0.05, start_rps=1.0, factor=2.0, max_rps=2.0)
        await driver.aclose()
        return result

    body = asyncio.run(run())
    assert body["h"] == 2.0
    assert body["http_429s"] == {"door": 0, "kitchen": 2, "courier": 1}
    assert body["door_first"] is False
    assert driver.h == 2.0


def test_calibrate_h_is_last_flat_step_when_backlog_climbs() -> None:
    fake = FakePipeline()
    fake.backlog_end = 40
    settings = LoadgenSettings(calibrate_step_s=0.05)
    driver = OpenLoopDriver(settings, fake)

    async def run() -> dict[str, Any]:
        await driver.start()
        result = await driver.calibrate(step_s=0.05, start_rps=0.5, factor=2.0, max_rps=2.0)
        await driver.aclose()
        return result

    body = asyncio.run(run())
    assert body["h"] == 0.0
    assert body["http_429s"]["kitchen"] == 2
    assert body["steps"][0]["flat"] is False


def test_zero_h_refuses_steady_and_rush() -> None:
    fake = FakePipeline()
    fake.backlog_end = 40
    app = create_app(LoadgenSettings(calibrate_step_s=0.05), client=fake)

    with TestClient(app) as client:
        calibrated = client.post(
            "/calibrate",
            json={"step_s": 0.05, "start_rps": 1.0, "factor": 2.0, "max_rps": 1.0},
        )
        assert calibrated.status_code == 200
        assert calibrated.json()["h"] == 0.0
        steady = client.post("/scenario/steady")
        rush = client.post("/scenario/rush")

    assert steady.status_code == 409
    assert rush.status_code == 409
    assert "did not find a sustainable H" in steady.json()["detail"]
    assert "did not find a sustainable H" in rush.json()["detail"]


def test_calibrate_rejects_flat_backlog_with_over_age_or_new_park() -> None:
    class UnsafePipeline(FakePipeline):
        async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
            body = await super().snapshot(cohort_id)
            body["oldest_open"] = {"age_s": 130.0, "stage": "confirmed"}
            body["parked_list"] = [] if self.snapshots == 1 else [{"order_id": "parked"}]
            return body

    fake = UnsafePipeline()
    driver = OpenLoopDriver(LoadgenSettings(), fake)

    async def run() -> dict[str, Any]:
        await driver.start()
        result = await driver.calibrate(
            step_s=0.05,
            start_rps=1.0,
            factor=2.0,
            max_rps=1.0,
        )
        await driver.aclose()
        return result

    body = asyncio.run(run())
    assert body["h"] == 0.0
    assert body["steps"][0]["backlog_flat"] is True
    assert body["steps"][0]["oldest_within_bound"] is False
    assert body["steps"][0]["no_new_parks"] is False
    assert body["steps"][0]["flat"] is False


def test_calibrate_keeps_probing_after_backlog_growth_until_downstream_429() -> None:
    class OverloadProbePipeline(FakePipeline):
        async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
            del cohort_id
            self.snapshots += 1
            samples = (
                (0, 0),
                (3, 0),  # 0.5 rps: low-WIP fill-in is sustainable.
                (3, 0),
                (10, 0),  # 1.0 rps: backlog grows, but 3x has not fired yet.
                (10, 0),
                (10, 1),  # 2.0 rps: continue probing until kitchen busy is seen.
            )
            backlog, kitchen_429s = samples[min(self.snapshots - 1, len(samples) - 1)]
            return {
                "backlog": {
                    "confirm": backlog,
                    "poll_cook": 0,
                    "dispatch": 0,
                    "poll_ride": 0,
                },
                "oldest_open": {"age_s": 10.0, "stage": "confirmed"},
                "http_429s": {"door": 0, "kitchen": kitchen_429s, "courier": 0},
            }

    fake = OverloadProbePipeline()
    driver = OpenLoopDriver(LoadgenSettings(), fake)

    async def run() -> dict[str, Any]:
        await driver.start()
        result = await driver.calibrate(
            step_s=0.05,
            start_rps=0.5,
            factor=2.0,
            max_rps=2.0,
        )
        await driver.aclose()
        return result

    body = asyncio.run(run())
    assert body["h"] == 0.5
    assert len(body["steps"]) == 3
    assert body["steps"][1]["flat"] is False
    assert body["steps"][2]["http_429s_delta"]["kitchen"] == 1
    assert body["downstream_429_observed"] is True


def test_loadgen_http_health_cohort_steady_rush_stop() -> None:
    fake = FakePipeline()
    settings = LoadgenSettings(calibrate_step_s=0.05)
    app = create_app(settings, client=fake)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert client.get("/ready").json() == {"status": "ok"}
        calibrated = client.post(
            "/calibrate",
            json={"step_s": 0.05, "start_rps": 1.0, "factor": 2.0, "max_rps": 1.0},
        )
        assert calibrated.status_code == 200, calibrated.text
        body = calibrated.json()
        assert "h" in body
        assert set(body["http_429s"]) == {"door", "kitchen", "courier"}
        steady = client.post("/scenario/steady")
        assert steady.status_code == 200, steady.text
        assert steady.json()["rate_rps"] == body["h"] * 0.4
        rush = client.post("/scenario/rush")
        assert rush.status_code == 200, rush.text
        assert rush.json()["peak_rps"] == body["h"] * 1.5
        rushed = client.post("/scenario/rush?mult=2.0")
        assert rushed.status_code == 200, rushed.text
        assert rushed.json()["mult"] == 2.0
        stopped = client.post("/stop")
        assert stopped.status_code == 200
        assert stopped.json()["rate_rps"] == 0.0
        cohort = client.post("/cohort/new")
        assert cohort.status_code == 200
        assert "cohort_id" in cohort.json()
        again = client.post("/calibrate", json={"step_s": 0.05, "start_rps": 1.0, "max_rps": 1.0})
        assert again.status_code == 200
