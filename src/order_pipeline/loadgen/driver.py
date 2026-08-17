"""Open-loop arrival scheduler. Never slows on 429."""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from typing import Any
from uuid import UUID

from order_pipeline.intake import DEFAULT_COHORT_ID
from order_pipeline.loadgen.carts import pick_cart
from order_pipeline.loadgen.client import PipelineClient
from order_pipeline.loadgen.settings import LoadgenSettings


def backlog_total(snapshot: dict[str, Any]) -> int:
    raw = snapshot.get("backlog")
    if not isinstance(raw, dict):
        return 0
    total = 0
    for value in raw.values():
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def http_429s_from_snapshot(snapshot: dict[str, Any]) -> dict[str, int]:
    raw = snapshot.get("http_429s")
    if not isinstance(raw, dict):
        return {"door": 0, "kitchen": 0, "courier": 0}
    mix = {"door": 0, "kitchen": 0, "courier": 0}
    for key in mix:
        value = raw.get(key, 0)
        mix[key] = int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
    return mix


def oldest_age_s(snapshot: dict[str, Any]) -> float | None:
    raw = snapshot.get("oldest_open")
    if not isinstance(raw, dict):
        return None
    age = raw.get("age_s")
    return float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else None


def parked_count(snapshot: dict[str, Any]) -> int:
    raw = snapshot.get("parked_list")
    return len(raw) if isinstance(raw, list) else 0


def failed_count(snapshot: dict[str, Any]) -> int:
    raw = snapshot.get("conservation")
    if not isinstance(raw, dict):
        return 0
    failed = raw.get("failed")
    return failed if isinstance(failed, int) and not isinstance(failed, bool) else 0


def no_progress_count(snapshot: dict[str, Any]) -> int:
    raw = snapshot.get("no_progress_beyond_threshold")
    if not isinstance(raw, dict):
        return 0
    count = raw.get("count")
    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def step_is_flat(*, start_backlog: int, end_backlog: int) -> bool:
    """H is the highest rate whose backlog does not climb during the step.

    A short step starting near empty adds normal in-flight inventory (arrival ×
    dwell), so a small absolute fill-in is allowed only at low WIP. Once the
    pipeline is populated, flat means no backlog growth.
    """
    slack = 4 if start_backlog <= 4 else 0
    return end_backlog <= start_backlog + slack


class OpenLoopDriver:
    """Target-rate arrivals. HTTP completion never paces the next fire."""

    def __init__(
        self,
        settings: LoadgenSettings,
        client: PipelineClient,
        *,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.rng = rng or random.Random()
        self.cohort_id: UUID = DEFAULT_COHORT_ID
        self.h: float | None = None
        self.rate_rps = 0.0
        self.placed = 0
        self.rejected_429 = 0
        self.other_status = 0
        self._running = False
        self._loop_task: asyncio.Task[None] | None = None
        self._rush_task: asyncio.Task[None] | None = None
        self._in_flight: set[asyncio.Task[None]] = set()
        self._rate_generation = 0
        self._rate_changed = asyncio.Event()

    def snapshot_status(self) -> dict[str, Any]:
        return {
            "cohort_id": str(self.cohort_id),
            "h": self.h,
            "rate_rps": self.rate_rps,
            "placed": self.placed,
            "rejected_429": self.rejected_429,
            "running": self.rate_rps > 0,
        }

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        self._running = True
        self._loop_task = asyncio.create_task(self._run_loop())

    async def aclose(self) -> None:
        self._running = False
        self.rate_rps = 0.0
        if self._rush_task is not None:
            self._rush_task.cancel()
            try:
                await self._rush_task
            except asyncio.CancelledError:
                pass
            self._rush_task = None
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        pending = list(self._in_flight)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self.client.aclose()

    def new_cohort(self) -> UUID:
        self.cohort_id = uuid.uuid4()
        return self.cohort_id

    def set_rate(self, rps: float) -> None:
        rate = max(0.0, rps)
        if rate == self.rate_rps:
            return
        self.rate_rps = rate
        self._rate_generation += 1
        self._rate_changed.set()

    def stop(self) -> None:
        self.set_rate(0.0)
        if self._rush_task is not None:
            self._rush_task.cancel()
            self._rush_task = None

    async def stop_and_drain(self) -> None:
        """Stop arrivals and wait for in-flight POSTs so later tests see a quiet API."""
        self.stop()
        pending = list(self._in_flight)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def steady_rps(self) -> float:
        if self.h is None or self.h <= 0:
            raise RuntimeError("calibrate did not find a sustainable H")
        return self.settings.steady_fraction * self.h

    def rush_rps(self, mult: float = 1.0) -> float:
        if self.h is None or self.h <= 0:
            raise RuntimeError("calibrate did not find a sustainable H")
        return self.settings.rush_multiplier * self.h * mult

    async def _run_loop(self) -> None:
        next_due: float | None = None
        schedule_generation = -1
        while self._running:
            generation = self._rate_generation
            rate = self.rate_rps
            if rate <= 0:
                next_due = None
                self._rate_changed.clear()
                if generation != self._rate_generation:
                    continue
                await self._rate_changed.wait()
                continue

            # A new rate starts a new open-loop clock. Reusing the old origin
            # would turn an increase into a catch-up burst and a decrease into a pause.
            if next_due is None or schedule_generation != generation:
                schedule_generation = generation
                next_due = time.monotonic()

            delay = next_due - time.monotonic()
            if delay > 0:
                self._rate_changed.clear()
                if generation != self._rate_generation:
                    continue
                try:
                    await asyncio.wait_for(self._rate_changed.wait(), timeout=delay)
                    continue
                except TimeoutError:
                    pass
            if generation != self._rate_generation or self.rate_rps <= 0:
                continue
            task = asyncio.create_task(self._place_one())
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)
            next_due += 1.0 / rate

    async def _place_one(self) -> None:
        items = pick_cart(
            self.rng,
            one_item_pct=self.settings.one_item_pct,
            two_item_pct=self.settings.two_item_pct,
        )
        try:
            status = await self.client.place(
                items=items,
                cohort_id=self.cohort_id,
                place_key=str(uuid.uuid4()),
            )
        except Exception:
            self.other_status += 1
            return
        if status == 201:
            self.placed += 1
        elif status == 429:
            self.rejected_429 += 1
        else:
            self.other_status += 1

    async def calibrate(
        self,
        *,
        step_s: float | None = None,
        start_rps: float | None = None,
        factor: float | None = None,
        max_rps: float | None = None,
    ) -> dict[str, Any]:
        """Stepped ramp. H = highest flat-backlog step. Reports H + 429 mix."""
        step = self.settings.calibrate_step_s if step_s is None else step_s
        rps = self.settings.calibrate_start_rps if start_rps is None else start_rps
        grow = self.settings.calibrate_factor if factor is None else factor
        cap = self.settings.calibrate_max_rps if max_rps is None else max_rps
        if step <= 0 or rps <= 0 or grow <= 1.0 or cap < rps:
            raise ValueError("invalid calibrate knobs")
        await self.stop_and_drain()
        steps: list[dict[str, Any]] = []
        h = 0.0
        overload_seen = False
        downstream_429_observed = False
        last_mix = {"door": 0, "kitchen": 0, "courier": 0}
        last_oldest: float | None = None
        while rps <= cap + 1e-9:
            self.set_rate(rps)
            warmup = min(max(3.0, step * 0.4), step * 0.5)
            await asyncio.sleep(warmup)
            first = await self.client.snapshot(self.cohort_id)
            await asyncio.sleep(max(0.0, step - warmup))
            last = await self.client.snapshot(self.cohort_id)
            start_backlog = backlog_total(first)
            end_backlog = backlog_total(last)
            first_mix = http_429s_from_snapshot(first)
            mix = http_429s_from_snapshot(last)
            mix_delta = {name: max(0, mix[name] - first_mix[name]) for name in mix}
            start_age = oldest_age_s(first)
            age = oldest_age_s(last)
            backlog_flat = step_is_flat(
                start_backlog=start_backlog,
                end_backlog=end_backlog,
            )
            oldest_within_bound = age is None or age <= self.settings.confirm_deadline_s
            no_new_parks = parked_count(last) <= parked_count(first)
            no_new_failures = failed_count(last) <= failed_count(first)
            no_new_stalls = no_progress_count(last) <= no_progress_count(first)
            flat = (
                backlog_flat
                and oldest_within_bound
                and no_new_parks
                and no_new_failures
                and no_new_stalls
            )
            steps.append(
                {
                    "rps": rps,
                    "backlog_start": start_backlog,
                    "backlog_end": end_backlog,
                    "oldest_age_start_s": start_age,
                    "oldest_age_s": age,
                    "http_429s": mix,
                    "http_429s_delta": mix_delta,
                    "backlog_flat": backlog_flat,
                    "oldest_within_bound": oldest_within_bound,
                    "no_new_parks": no_new_parks,
                    "no_new_failures": no_new_failures,
                    "no_new_stalls": no_new_stalls,
                    "flat": flat,
                }
            )
            last_mix = mix
            last_oldest = age
            downstream_429_this_step = mix_delta["kitchen"] + mix_delta["courier"] > 0
            door_observed = mix_delta["door"] > 0
            if flat and not overload_seen and not downstream_429_this_step and not door_observed:
                h = rps
            else:
                overload_seen = True
            downstream_429_observed = downstream_429_observed or (downstream_429_this_step)
            if overload_seen and (downstream_429_observed or door_observed):
                break
            rps = round(rps * grow, 4)
        await self.stop_and_drain()
        self.h = h
        door_first = last_mix["door"] > 0 and last_mix["door"] > (
            last_mix["kitchen"] + last_mix["courier"]
        )
        return {
            "h": h,
            "http_429s": last_mix,
            "door_first": door_first,
            "downstream_429_observed": downstream_429_observed,
            "oldest_age_s": last_oldest,
            "steps": steps,
            "hint": ("raise API_ACCEPT_CONCURRENCY and recalibrate" if door_first else None),
        }

    async def start_rush(self, *, mult: float = 1.0) -> dict[str, Any]:
        """60s @ 1.5×H×mult then drain to 0.4×H. Does not replay a baseline minute."""
        peak = self.rush_rps(mult)
        drain = self.steady_rps()
        self.set_rate(peak)
        if self._rush_task is not None:
            self._rush_task.cancel()
        self._rush_task = asyncio.create_task(self._rush_then_drain(drain))
        return {
            "peak_rps": peak,
            "drain_rps": drain,
            "duration_s": self.settings.rush_duration_s,
            "mult": mult,
        }

    async def _rush_then_drain(self, drain_rps: float) -> None:
        try:
            await asyncio.sleep(self.settings.rush_duration_s)
            self.set_rate(drain_rps)
        except asyncio.CancelledError:
            raise
