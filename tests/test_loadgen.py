"""Open-loop loadgen: never slows on 429; calibrate reports H + 429 mix."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

from order_pipeline.loadgen.app import create_app
from order_pipeline.loadgen.carts import pick_cart
from order_pipeline.loadgen.driver import (
    OpenLoopDriver,
    backlog_total,
    http_429s_from_snapshot,
    in_service_backlog,
    step_is_flat,
    waiting_backlog,
)
from order_pipeline.loadgen.settings import LoadgenSettings
from order_pipeline.menu import MENU_ITEM_IDS


def loadgen_settings(**kwargs: Any) -> LoadgenSettings:
    kwargs.setdefault("drain_timeout_s", 0.2)
    kwargs.setdefault("drain_poll_s", 0.01)
    kwargs.setdefault("recovery_streak", 3)
    return LoadgenSettings(**kwargs)


def _empty_backlog() -> dict[str, int]:
    return {"confirm": 0, "poll_cook": 0, "dispatch": 0, "poll_ride": 0}


class FakePipeline:
    def __init__(self, *, place_status: int = 201, delay_s: float = 0.0) -> None:
        self.place_status = place_status
        self.delay_s = delay_s
        self.places: list[tuple[list[str], UUID]] = []
        self.place_keys: list[str] = []
        self.snapshots = 0
        self.backlog_end = 0

    async def place(self, *, items: list[str], cohort_id: UUID, place_key: str) -> int:
        self.places.append((list(items), cohort_id))
        self.place_keys.append(place_key)
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
            "currently_leased": 0,
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
    assert waiting_backlog(snap) == 3
    assert in_service_backlog(snap) == 3
    assert http_429s_from_snapshot(snap) == {"door": 1, "kitchen": 4, "courier": 2}


def test_open_loop_does_not_slow_on_429() -> None:
    fake = FakePipeline(place_status=429, delay_s=0.15)
    settings = loadgen_settings(calibrate_step_s=0.2)
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


def test_conservative_default_h_allows_scenarios_before_calibration() -> None:
    driver = OpenLoopDriver(loadgen_settings(default_h=0.25), FakePipeline())

    assert driver.h == 0.25
    assert driver.steady_rps() == 0.1
    assert driver.rush_rps() == 0.375


def test_status_reports_the_fallback_h_as_unmeasured() -> None:
    """Rush sizes its peak off H, so callers must be able to tell a guess from a measurement."""
    driver = OpenLoopDriver(loadgen_settings(default_h=0.25), FakePipeline())

    status = driver.snapshot_status()
    assert status["h"] == 0.25
    assert status["h_source"] == "fallback"
    assert status["calibrated"] is False

    driver.h = 0.4
    driver.h_source = "calibrated"
    assert driver.snapshot_status()["calibrated"] is True

    # A calibration that never found a sustainable step is not a baseline.
    driver.h = 0.0
    assert driver.snapshot_status()["calibrated"] is False


def test_rate_change_starts_a_new_clock_and_stop_fires_nothing_late() -> None:
    fake = FakePipeline()
    driver = OpenLoopDriver(loadgen_settings(), fake, rng=random.Random(2))

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
    settings = loadgen_settings(
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
    assert set(body).issuperset(
        {"offered", "accepted", "door_429", "other_http", "transport_unknown"}
    )


def test_calibrate_h_is_last_flat_step_when_backlog_climbs() -> None:
    fake = FakePipeline()
    fake.backlog_end = 40
    settings = loadgen_settings(calibrate_step_s=0.05)
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
    app = create_app(loadgen_settings(calibrate_step_s=0.05), client=fake)

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
    driver = OpenLoopDriver(loadgen_settings(), fake)

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
    driver = OpenLoopDriver(loadgen_settings(), fake)

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


def test_calibrate_ignores_in_service_cook_and_ride_polls() -> None:
    """Growing poll_cook / poll_ride is healthy dwell, not a waiting queue."""

    class CookingPipeline(FakePipeline):
        async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
            del cohort_id
            self.snapshots += 1
            cook = 0 if self.snapshots == 1 else 40
            return {
                "backlog": {
                    "confirm": 0,
                    "poll_cook": cook,
                    "dispatch": 0,
                    "poll_ride": cook,
                },
                "oldest_open": {"age_s": 20.0, "stage": "being prepared"},
                "http_429s": {"door": 0, "kitchen": 0, "courier": 0},
            }

    fake = CookingPipeline()
    driver = OpenLoopDriver(loadgen_settings(), fake)

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
    assert body["h"] == 2.0
    assert all(step["backlog_flat"] for step in body["steps"])
    assert body["downstream_429_observed"] is False


def test_calibrate_door_first_ignores_earlier_cumulative_429s() -> None:
    class PriorDoorPipeline(FakePipeline):
        async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
            del cohort_id
            self.snapshots += 1
            return {
                "backlog": {"confirm": 0, "poll_cook": 0, "dispatch": 0, "poll_ride": 0},
                "oldest_open": {"age_s": 1.0, "stage": "placed"},
                # Ten door rejections predate this calibration; the kitchen
                # rejection is the only brake that appears during this run.
                "http_429s": {
                    "door": 10,
                    "kitchen": 0 if self.snapshots == 1 else 1,
                    "courier": 0,
                },
            }

    fake = PriorDoorPipeline()
    driver = OpenLoopDriver(loadgen_settings(), fake)

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
    assert body["downstream_429_observed"] is True
    assert body["door_first"] is False
    assert body["hint"] is None


def test_loadgen_http_health_cohort_steady_rush_stop() -> None:
    fake = FakePipeline()
    settings = loadgen_settings(calibrate_step_s=0.05)
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


class FakeCancelRace:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def run(self, cohort_id: UUID) -> dict[str, Any]:
        self.calls.append(cohort_id)
        return {
            "order_id": "11111111-1111-4111-8111-111111111111",
            "cohort_id": str(cohort_id),
            "cancel_status": 200,
            "state": "cancelled",
        }

    async def aclose(self) -> None:
        return None


def test_cancel_race_beat_uses_injected_runner() -> None:
    fake = FakePipeline()
    runner = FakeCancelRace()
    app = create_app(loadgen_settings(), client=fake, cancel_race=runner)
    with TestClient(app) as client:
        missing = create_app(loadgen_settings(), client=fake)
        with TestClient(missing) as bare:
            assert bare.post("/beat/cancel-race").status_code == 503
        response = client.post("/beat/cancel-race")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "cancelled"
        assert body["cancel_status"] == 200
        assert runner.calls == [UUID(body["cohort_id"])]


class FakeBeatPlace:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str]] = []

    async def run(self, cohort_id: UUID, item: str) -> dict[str, Any]:
        self.calls.append((cohort_id, item))
        return {
            "order_id": "22222222-2222-4222-8222-222222222222",
            "cohort_id": str(cohort_id),
            "item": item,
        }

    async def aclose(self) -> None:
        return None


def test_beat_place_uses_injected_runner() -> None:
    fake = FakePipeline()
    runner = FakeBeatPlace()
    app = create_app(loadgen_settings(), client=fake, beat_place=runner)
    with TestClient(app) as client:
        missing = create_app(loadgen_settings(), client=fake)
        with TestClient(missing) as bare:
            assert bare.post("/beat/place", json={"item": "burrito"}).status_code == 503
        unknown = client.post("/beat/place", json={"item": "chicken_burrito"})
        assert unknown.status_code == 422
        response = client.post("/beat/place", json={"item": "burrito"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["item"] == "burrito"
        assert body["order_id"] == "22222222-2222-4222-8222-222222222222"
        assert runner.calls == [(UUID(body["cohort_id"]), "burrito")]


class TimeoutAfterCommit:
    """First POST is lost after the API accepted; replay must reuse the key."""

    def __init__(self) -> None:
        self.calls = 0
        self.committed = 0
        self.place_keys: list[str] = []
        self.bodies: list[list[str]] = []

    async def place(self, *, items: list[str], cohort_id: UUID, place_key: str) -> int:
        del cohort_id
        self.calls += 1
        self.place_keys.append(place_key)
        self.bodies.append(list(items))
        if self.calls == 1:
            self.committed += 1
            raise httpx.ReadTimeout("lost after commit")
        return 201

    async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
        del cohort_id
        return {
            "backlog": _empty_backlog(),
            "oldest_open": {"age_s": 1.0, "stage": "placed"},
            "http_429s": {"door": 0, "kitchen": 0, "courier": 0},
            "currently_leased": 0,
        }

    async def aclose(self) -> None:
        return None


def test_place_timeout_after_commit_replays_same_key_once() -> None:
    fake = TimeoutAfterCommit()
    driver = OpenLoopDriver(loadgen_settings(), fake, rng=random.Random(3))

    asyncio.run(driver._place_one())
    assert fake.committed == 1
    assert fake.calls == 2
    assert fake.place_keys[0] == fake.place_keys[1]
    assert fake.bodies[0] == fake.bodies[1]
    assert len(set(fake.place_keys)) == 1
    assert driver.placed == 1
    assert driver.offered == 1
    assert driver.transport_unknown == 0
    status = driver.snapshot_status()
    assert status["offered"] == 1
    assert status["placed"] == 1
    assert status["other_http"] == 0
    assert status["transport_unknown"] == 0


class SplitOutcomePipeline:
    """Door 429, other HTTP, and unresolved transport each get their own bucket."""

    def __init__(self) -> None:
        self.calls = 0
        self.snapshots = 0

    async def place(self, *, items: list[str], cohort_id: UUID, place_key: str) -> int:
        del items, cohort_id, place_key
        self.calls += 1
        if self.calls == 1:
            return 201
        if self.calls == 2:
            return 429
        if self.calls == 3:
            return 503
        raise httpx.ConnectError("upstream gone")

    async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
        del cohort_id
        self.snapshots += 1
        return {
            "backlog": _empty_backlog(),
            "oldest_open": {"age_s": 1.0, "stage": "placed"},
            "http_429s": {"door": 0, "kitchen": 0, "courier": 0},
            "currently_leased": 0,
        }

    async def aclose(self) -> None:
        return None


def test_status_splits_offered_accepted_door_other_http_and_transport() -> None:
    fake = SplitOutcomePipeline()
    driver = OpenLoopDriver(loadgen_settings(place_timeout_retries=0), fake, rng=random.Random(4))

    async def run() -> None:
        await driver._place_one()
        await driver._place_one()
        await driver._place_one()
        await driver._place_one()

    asyncio.run(run())
    assert driver.offered == 4
    assert driver.placed == 1
    assert driver.rejected_429 == 1
    assert driver.other_http == 1
    assert driver.transport_unknown == 1
    counters = driver.load_counters()
    assert counters == {
        "offered": 4,
        "accepted": 1,
        "door_429": 1,
        "other_http": 1,
        "transport_unknown": 1,
    }


class TransportDuringCalibrate(FakePipeline):
    async def place(self, *, items: list[str], cohort_id: UUID, place_key: str) -> int:
        self.places.append((list(items), cohort_id))
        self.place_keys.append(place_key)
        raise httpx.ReadTimeout("lost")

    async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
        del cohort_id
        self.snapshots += 1
        return {
            "backlog": _empty_backlog(),
            "oldest_open": {"age_s": 1.0, "stage": "placed"},
            "http_429s": {"door": 0, "kitchen": 0, "courier": 0},
            "currently_leased": 0,
        }


def test_unresolved_transport_invalidates_a_calibration_step() -> None:
    fake = TransportDuringCalibrate()
    driver = OpenLoopDriver(
        loadgen_settings(place_timeout_retries=0, calibrate_stale_abort_steps=2),
        fake,
    )

    async def run() -> dict[str, Any]:
        await driver.start()
        result = await driver.calibrate(step_s=0.05, start_rps=1.0, factor=2.0, max_rps=1.0)
        await driver.aclose()
        return result

    body = asyncio.run(run())
    assert body["h"] == 0.0
    assert body["transport_unknown"] >= 1
    assert body["steps"][0]["transport_clean"] is False
    assert body["steps"][0]["flat"] is False
    assert body["accepted"] == 0


class DirtyOldestPipeline(FakePipeline):
    async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
        del cohort_id
        self.snapshots += 1
        return {
            "backlog": _empty_backlog(),
            "oldest_open": {"age_s": 130.0, "stage": "confirmed"},
            "http_429s": {"door": 0, "kitchen": 0, "courier": 0},
            "parked_list": [{"order_id": "leftover"}],
            "no_progress_beyond_threshold": {"count": 1, "threshold_s": 90},
            "currently_leased": 0,
        }


def test_calibrate_aborts_when_h_stays_zero() -> None:
    fake = DirtyOldestPipeline()
    driver = OpenLoopDriver(
        loadgen_settings(calibrate_stale_abort_steps=2),
        fake,
    )

    async def run() -> dict[str, Any]:
        await driver.start()
        result = await driver.calibrate(
            step_s=0.05,
            start_rps=0.5,
            factor=2.0,
            max_rps=8.0,
        )
        await driver.aclose()
        return result

    body = asyncio.run(run())
    assert body["h"] == 0.0
    assert body["aborted"] == "h_still_zero"
    assert len(body["steps"]) == 2
    assert body["diagnostic"]["parked_count"] == 1
    assert "docker compose down -v" in body["diagnostic"]["hint"]


class LingeringCookPipeline:
    def __init__(self) -> None:
        self.places = 0
        self.snapshots = 0
        self.cook_left = 4

    async def place(self, *, items: list[str], cohort_id: UUID, place_key: str) -> int:
        del items, cohort_id, place_key
        self.places += 1
        return 201

    async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
        del cohort_id
        self.snapshots += 1
        cook = self.cook_left
        if self.cook_left > 0:
            self.cook_left -= 1
        return {
            "backlog": {
                "confirm": 0,
                "poll_cook": cook,
                "dispatch": 0,
                "poll_ride": cook,
            },
            "oldest_open": {"age_s": 5.0, "stage": "being prepared"},
            "http_429s": {"door": 0, "kitchen": 0, "courier": 0},
            "currently_leased": 0,
        }

    async def aclose(self) -> None:
        return None


def test_stop_and_drain_waits_for_cook_and_ride_after_posts() -> None:
    fake = LingeringCookPipeline()
    driver = OpenLoopDriver(
        loadgen_settings(drain_timeout_s=1.0, drain_poll_s=0.02),
        fake,
    )

    async def run() -> dict[str, Any]:
        await driver.start()
        await driver._place_one()
        started = time.monotonic()
        result = await driver.stop_and_drain()
        result["_elapsed"] = time.monotonic() - started
        await driver.aclose()
        return result

    body = asyncio.run(run())
    assert fake.places == 1
    assert body["quiesced"] is True
    assert body["timed_out"] is False
    assert body["in_service"] == 0
    assert fake.snapshots >= 4
    assert body["_elapsed"] >= 0.04


class RecoveringRushPipeline:
    def __init__(self) -> None:
        self.snapshots = 0

    async def place(self, *, items: list[str], cohort_id: UUID, place_key: str) -> int:
        del items, cohort_id, place_key
        return 201

    async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
        del cohort_id
        self.snapshots += 1
        waiting = max(0, 8 - self.snapshots)
        age = max(0.0, 20.0 - 3.0 * self.snapshots)
        parked = [{"order_id": "shed"}] if self.snapshots >= 2 else []
        return {
            "backlog": {
                "confirm": waiting,
                "poll_cook": 0,
                "dispatch": 0,
                "poll_ride": 0,
            },
            "oldest_open": {"age_s": age, "stage": "placed"},
            "http_429s": {"door": 0, "kitchen": 0, "courier": 0},
            "parked_list": parked,
            "currently_leased": 0,
        }

    async def aclose(self) -> None:
        return None


def test_observe_recovery_requires_sustained_decline_and_reports_parking() -> None:
    fake = RecoveringRushPipeline()
    driver = OpenLoopDriver(
        loadgen_settings(recovery_streak=3, drain_poll_s=0.01, drain_timeout_s=1.0),
        fake,
    )

    async def run() -> dict[str, Any]:
        return await driver.observe_recovery(
            baseline_backlog=0,
            baseline_age_s=2.0,
            peak_backlog=8,
            peak_age_s=20.0,
            timeout_s=1.0,
        )

    body = asyncio.run(run())
    assert body["recovered"] is True
    assert body["backlog_recovered"] is True
    assert body["parked_seen"] is True
    assert body["parked_count"] >= 1
    assert body["waiting_backlog"] < body["peak_backlog"]


def test_observe_drain_endpoint_returns_production_predicate() -> None:
    fake = RecoveringRushPipeline()
    app = create_app(
        loadgen_settings(recovery_streak=3, drain_poll_s=0.01, drain_timeout_s=1.0),
        client=fake,
    )
    with TestClient(app) as client:
        response = client.post(
            "/observe-drain",
            json={
                "baseline_backlog": 0,
                "baseline_age_s": 2.0,
                "peak_backlog": 8,
                "peak_age_s": 20.0,
                "timeout_s": 1.0,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recovered"] is True
    assert body["parked_seen"] is True
    assert "parked_count" in body


class StuckRushPipeline:
    async def place(self, *, items: list[str], cohort_id: UUID, place_key: str) -> int:
        del items, cohort_id, place_key
        return 201

    async def snapshot(self, cohort_id: UUID) -> dict[str, Any]:
        del cohort_id
        return {
            "backlog": {
                "confirm": 12,
                "poll_cook": 0,
                "dispatch": 0,
                "poll_ride": 0,
            },
            "oldest_open": {"age_s": 40.0, "stage": "placed"},
            "http_429s": {"door": 0, "kitchen": 0, "courier": 0},
            "parked_list": [{"order_id": "parked"}],
            "currently_leased": 0,
        }

    async def aclose(self) -> None:
        return None


def test_observe_drain_does_not_treat_parking_as_recovery() -> None:
    fake = StuckRushPipeline()
    app = create_app(
        loadgen_settings(drain_poll_s=0.01, drain_timeout_s=0.08),
        client=fake,
    )
    with TestClient(app) as client:
        response = client.post(
            "/observe-drain",
            json={
                "baseline_backlog": 0,
                "baseline_age_s": 1.0,
                "peak_backlog": 12,
                "peak_age_s": 40.0,
                "timeout_s": 0.08,
            },
        )
    assert response.status_code == 504, response.text
    body = response.json()["detail"]
    assert body["recovered"] is False
    assert body["parked_seen"] is True
    assert body["parked_count"] == 1
