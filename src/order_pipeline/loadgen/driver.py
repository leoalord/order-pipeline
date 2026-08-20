"""Open-loop arrival scheduler. Never slows on 429."""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from typing import Any
from uuid import UUID

import httpx

from order_pipeline.intake import DEFAULT_COHORT_ID
from order_pipeline.loadgen.carts import pick_cart
from order_pipeline.loadgen.client import PipelineClient
from order_pipeline.loadgen.settings import LoadgenSettings

# Confirm / dispatch are waiting to start kitchen or courier work. poll_cook
# and poll_ride are in-service (cooking or en route) and must not cap H.
WAITING_WORK_TYPES = ("confirm", "dispatch")
IN_SERVICE_WORK_TYPES = ("poll_cook", "poll_ride")

_TRANSPORT_UNKNOWN = (
    httpx.TimeoutException,
    httpx.TransportError,
    TimeoutError,
    ConnectionError,
    OSError,
)


def backlog_total(snapshot: dict[str, Any]) -> int:
    raw = snapshot.get("backlog")
    if not isinstance(raw, dict):
        return 0
    total = 0
    for value in raw.values():
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def _typed_backlog(snapshot: dict[str, Any], names: tuple[str, ...]) -> int:
    raw = snapshot.get("backlog")
    if not isinstance(raw, dict):
        return 0
    total = 0
    for name in names:
        value = raw.get(name, 0)
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def waiting_backlog(snapshot: dict[str, Any]) -> int:
    """Work still waiting for a kitchen or courier slot, not in-service polls."""
    return _typed_backlog(snapshot, WAITING_WORK_TYPES)


def in_service_backlog(snapshot: dict[str, Any]) -> int:
    """Cook / ride polls that keep running after the place POST returns."""
    return _typed_backlog(snapshot, IN_SERVICE_WORK_TYPES)


def currently_leased(snapshot: dict[str, Any]) -> int:
    value = snapshot.get("currently_leased")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


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
    """H is the highest rate whose waiting queue does not climb during the step.

    Callers pass confirm+dispatch only. Cooking and on-bike polls are healthy
    in-flight work; a short step may add a few waiting tickets while workers
    catch up. Once that waiting queue is already populated, flat means no
    further growth. Kitchen/courier 429s are the capacity brake.
    """
    slack = 4 if start_backlog <= 4 else 0
    return end_backlog <= start_backlog + slack


def is_transport_unknown(exc: BaseException) -> bool:
    return isinstance(exc, _TRANSPORT_UNKNOWN)


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
        self.h: float | None = settings.default_h
        # The boot H is a conservative host-independent guess, not a measurement.
        # Callers that size pressure off H need to know which one they have.
        self.h_source: str = "fallback"
        self.rate_rps = 0.0
        self.offered = 0
        self.placed = 0
        self.rejected_429 = 0
        self.other_http = 0
        self.transport_unknown = 0
        self._running = False
        self._loop_task: asyncio.Task[None] | None = None
        self._rush_task: asyncio.Task[None] | None = None
        self._in_flight: set[asyncio.Task[None]] = set()
        self._rate_generation = 0
        self._rate_changed = asyncio.Event()
        self._rush_baseline_backlog: int | None = None
        self._rush_baseline_age_s: float | None = None

    def load_counters(self) -> dict[str, int]:
        return {
            "offered": self.offered,
            "accepted": self.placed,
            "door_429": self.rejected_429,
            "other_http": self.other_http,
            "transport_unknown": self.transport_unknown,
        }

    def snapshot_status(self) -> dict[str, Any]:
        return {
            "cohort_id": str(self.cohort_id),
            "h": self.h,
            "h_source": self.h_source,
            "calibrated": self.h_source == "calibrated" and (self.h or 0.0) > 0,
            "rate_rps": self.rate_rps,
            "offered": self.offered,
            "placed": self.placed,
            "rejected_429": self.rejected_429,
            "other_http": self.other_http,
            "transport_unknown": self.transport_unknown,
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

    async def _drain_http(self) -> None:
        """Await dispatched POSTs. stop() is synchronous, so this list has no leak."""
        pending = list(self._in_flight)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _pipeline_busy(self, snapshot: dict[str, Any]) -> bool:
        return (
            waiting_backlog(snapshot) > 0
            or in_service_backlog(snapshot) > 0
            or currently_leased(snapshot) > 0
        )

    def _activity_from_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "waiting_backlog": waiting_backlog(snapshot),
            "in_service": in_service_backlog(snapshot),
            "currently_leased": currently_leased(snapshot),
            "parked": parked_count(snapshot),
            "oldest_age_s": oldest_age_s(snapshot),
        }

    async def _quiesce_pipeline(self, *, timeout_s: float) -> dict[str, Any]:
        """Wait for waiting + cook/ride + leased work on the active cohort."""
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {
            "waiting_backlog": 0,
            "in_service": 0,
            "currently_leased": 0,
            "parked": 0,
            "oldest_age_s": None,
        }
        while time.monotonic() < deadline:
            snapshot = await self.client.snapshot(self.cohort_id)
            last = self._activity_from_snapshot(snapshot)
            if not self._pipeline_busy(snapshot):
                return {
                    "quiesced": True,
                    "timed_out": False,
                    "reason": None,
                    **last,
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self.settings.drain_poll_s, remaining))
        return {
            "quiesced": False,
            "timed_out": True,
            "reason": (
                "pipeline still busy after stop: waiting "
                f"{last['waiting_backlog']}, in_service {last['in_service']}, "
                f"leased {last['currently_leased']}"
            ),
            **last,
        }

    async def stop_and_drain(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Stop arrivals, finish in-flight POSTs, then wait for pipeline work.

        HTTP drain is not enough: cook and ride keep running after the place
        POST returns. Bound the wait and return a visible failure if the
        cohort does not quiesce.
        """
        limit = self.settings.drain_timeout_s if timeout_s is None else timeout_s
        self.stop()
        await self._drain_http()
        pipeline = await self._quiesce_pipeline(timeout_s=limit)
        status = self.snapshot_status()
        status.update(pipeline)
        return status

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

    async def _place_once(self, *, items: list[str], place_key: str) -> int:
        return await self.client.place(
            items=items,
            cohort_id=self.cohort_id,
            place_key=place_key,
        )

    async def _place_one(self) -> None:
        items = pick_cart(
            self.rng,
            one_item_pct=self.settings.one_item_pct,
            two_item_pct=self.settings.two_item_pct,
        )
        place_key = str(uuid.uuid4())
        self.offered += 1
        attempts = 1 + self.settings.place_timeout_retries
        status: int | None = None
        for attempt in range(attempts):
            try:
                status = await self._place_once(items=items, place_key=place_key)
                break
            except Exception as exc:
                if is_transport_unknown(exc) and attempt + 1 < attempts:
                    continue
                if is_transport_unknown(exc):
                    self.transport_unknown += 1
                    return
                self.transport_unknown += 1
                return
        if status == 201:
            self.placed += 1
        elif status == 429:
            self.rejected_429 += 1
        else:
            self.other_http += 1

    def _step_counters(self, start: dict[str, int], end: dict[str, int]) -> dict[str, int]:
        return {name: max(0, end[name] - start[name]) for name in start}

    def _dirty_diagnostic(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "parked_count": parked_count(snapshot),
            "failed_count": failed_count(snapshot),
            "no_progress_count": no_progress_count(snapshot),
            "oldest_age_s": oldest_age_s(snapshot),
            "waiting_backlog": waiting_backlog(snapshot),
            "hint": (
                "leftover parked/stalled work or a fat sim ledger will pin "
                "oldest-age and walk the ramp to h=0; run "
                "`docker compose down -v && docker compose up --wait` "
                "then calibrate again"
            ),
        }

    async def calibrate(
        self,
        *,
        step_s: float | None = None,
        start_rps: float | None = None,
        factor: float | None = None,
        max_rps: float | None = None,
    ) -> dict[str, Any]:
        """Stepped ramp. H = highest rate that keeps up (waiting queue flat, no new 429s)."""
        step = self.settings.calibrate_step_s if step_s is None else step_s
        rps = self.settings.calibrate_start_rps if start_rps is None else start_rps
        grow = self.settings.calibrate_factor if factor is None else factor
        cap = self.settings.calibrate_max_rps if max_rps is None else max_rps
        if step <= 0 or rps <= 0 or grow <= 1.0 or cap < rps:
            raise ValueError("invalid calibrate knobs")
        await self._drain_http()
        self.stop()
        # Fresh cohort so a prior run's parked/stalled rows cannot pin oldest-age.
        self.new_cohort()
        steps: list[dict[str, Any]] = []
        h = 0.0
        overload_seen = False
        downstream_429_observed = False
        first_brake: str | None = None
        last_mix = {"door": 0, "kitchen": 0, "courier": 0}
        last_oldest: float | None = None
        last_snapshot: dict[str, Any] = {}
        aborted: str | None = None
        diagnostic: dict[str, Any] | None = None
        zero_h_steps = 0
        while rps <= cap + 1e-9:
            before = self.load_counters()
            self.set_rate(rps)
            warmup = min(max(3.0, step * 0.4), step * 0.5)
            await asyncio.sleep(warmup)
            first = await self.client.snapshot(self.cohort_id)
            await asyncio.sleep(max(0.0, step - warmup))
            last = await self.client.snapshot(self.cohort_id)
            last_snapshot = last
            after = self.load_counters()
            counters = self._step_counters(before, after)
            start_backlog = waiting_backlog(first)
            end_backlog = waiting_backlog(last)
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
            transport_clean = counters["transport_unknown"] == 0
            flat = (
                backlog_flat
                and oldest_within_bound
                and no_new_parks
                and no_new_failures
                and no_new_stalls
                and transport_clean
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
                    "transport_clean": transport_clean,
                    "flat": flat,
                    **counters,
                }
            )
            last_mix = mix
            last_oldest = age
            downstream_429_this_step = mix_delta["kitchen"] + mix_delta["courier"] > 0
            door_observed = mix_delta["door"] > 0
            if first_brake is None:
                if door_observed and not downstream_429_this_step:
                    first_brake = "door"
                elif downstream_429_this_step and not door_observed:
                    first_brake = "downstream"
                elif door_observed and downstream_429_this_step:
                    first_brake = (
                        "door"
                        if mix_delta["door"] > mix_delta["kitchen"] + mix_delta["courier"]
                        else "downstream"
                    )
            if flat and not overload_seen and not downstream_429_this_step and not door_observed:
                h = rps
                zero_h_steps = 0
            else:
                overload_seen = True
                if h <= 0:
                    zero_h_steps += 1
            downstream_429_observed = downstream_429_observed or (downstream_429_this_step)
            if overload_seen and (downstream_429_observed or door_observed):
                break
            if h <= 0 and zero_h_steps >= self.settings.calibrate_stale_abort_steps:
                aborted = "h_still_zero"
                diagnostic = self._dirty_diagnostic(last)
                break
            rps = round(rps * grow, 4)
        self.stop()
        await self._drain_http()
        self.h = h
        self.h_source = "calibrated"
        # Snapshot counters are cohort-cumulative. Restrict this decision to
        # brake events observed during this calibration so an earlier run
        # cannot poison the result after the operator raises the door cap.
        door_first = first_brake == "door"
        result: dict[str, Any] = {
            "h": h,
            "cohort_id": str(self.cohort_id),
            "http_429s": last_mix,
            "door_first": door_first,
            "downstream_429_observed": downstream_429_observed,
            "oldest_age_s": last_oldest,
            "steps": steps,
            "hint": ("raise API_ACCEPT_CONCURRENCY and recalibrate" if door_first else None),
            "aborted": aborted,
            "diagnostic": diagnostic,
            **self.load_counters(),
        }
        if aborted is not None and last_snapshot:
            result["hint"] = diagnostic["hint"] if diagnostic is not None else result["hint"]
        return result

    async def start_rush(self, *, mult: float = 1.0) -> dict[str, Any]:
        """60s @ 1.5×H×mult then drain to 0.4×H. Does not replay a baseline minute."""
        peak = self.rush_rps(mult)
        drain = self.steady_rps()
        try:
            snap = await self.client.snapshot(self.cohort_id)
        except Exception:
            snap = {}
        self._rush_baseline_backlog = waiting_backlog(snap)
        self._rush_baseline_age_s = oldest_age_s(snap) or 0.0
        self.set_rate(peak)
        if self._rush_task is not None:
            self._rush_task.cancel()
        self._rush_task = asyncio.create_task(self._rush_then_drain(drain))
        return {
            "peak_rps": peak,
            "drain_rps": drain,
            "duration_s": self.settings.rush_duration_s,
            "mult": mult,
            "baseline_backlog": self._rush_baseline_backlog,
            "baseline_age_s": self._rush_baseline_age_s,
        }

    async def observe_recovery(
        self,
        *,
        baseline_backlog: int | None = None,
        baseline_age_s: float | None = None,
        peak_backlog: int | None = None,
        peak_age_s: float | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Wait for a sustained waiting-backlog / oldest-age drain.

        Parking is shedding, not recovery: parked rows are reported and never
        satisfy the predicate. Recovery is a return to within slack of the
        pre-rush baseline, or K consecutive declining samples.
        """
        base_backlog = self._rush_baseline_backlog if baseline_backlog is None else baseline_backlog
        base_age = self._rush_baseline_age_s if baseline_age_s is None else baseline_age_s
        if base_backlog is None or base_age is None:
            snap = await self.client.snapshot(self.cohort_id)
            if base_backlog is None:
                base_backlog = waiting_backlog(snap)
            if base_age is None:
                base_age = oldest_age_s(snap) or 0.0
        limit = self.settings.drain_timeout_s if timeout_s is None else timeout_s
        streak_k = self.settings.recovery_streak
        backlog_slack = self.settings.recovery_backlog_slack
        age_slack = self.settings.recovery_age_slack_s
        deadline = time.monotonic() + limit
        seen_peak_backlog = base_backlog if peak_backlog is None else peak_backlog
        seen_peak_age = base_age if peak_age_s is None else peak_age_s
        prev_waiting: int | None = None
        prev_age: float | None = None
        backlog_streak = 0
        age_streak = 0
        parked_seen = False
        last_waiting = base_backlog
        last_age = base_age
        last_parked = 0
        samples = 0
        while time.monotonic() < deadline:
            snapshot = await self.client.snapshot(self.cohort_id)
            samples += 1
            waiting = waiting_backlog(snapshot)
            age = oldest_age_s(snapshot)
            parked = parked_count(snapshot)
            last_waiting = waiting
            last_age = age if age is not None else 0.0
            last_parked = parked
            if parked > 0:
                parked_seen = True
            seen_peak_backlog = max(seen_peak_backlog, waiting)
            if age is not None:
                seen_peak_age = max(seen_peak_age, age)
            if prev_waiting is not None and waiting < prev_waiting:
                backlog_streak += 1
            else:
                backlog_streak = 0
            if age is None:
                age_streak = streak_k
            elif prev_age is not None and age < prev_age:
                age_streak += 1
            else:
                age_streak = 0
            prev_waiting = waiting
            prev_age = age
            backlog_at_baseline = waiting <= base_backlog + backlog_slack
            age_at_baseline = age is None or age <= base_age + age_slack
            backlog_recovered = backlog_at_baseline or backlog_streak >= streak_k
            age_recovered = age_at_baseline or age_streak >= streak_k
            # Waiting-backlog recovery is the allowed substitute when parks pin age.
            recovered = backlog_recovered and (age_recovered or backlog_recovered)
            if recovered:
                return {
                    "recovered": True,
                    "timed_out": False,
                    "backlog_recovered": backlog_recovered,
                    "age_recovered": age_recovered,
                    "waiting_recovered": backlog_recovered,
                    "parked_seen": parked_seen,
                    "parked_count": parked,
                    "baseline_backlog": base_backlog,
                    "baseline_age_s": base_age,
                    "peak_backlog": seen_peak_backlog,
                    "peak_age_s": seen_peak_age,
                    "waiting_backlog": waiting,
                    "oldest_age_s": age,
                    "samples": samples,
                    "reason": None,
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self.settings.drain_poll_s, remaining))
        return {
            "recovered": False,
            "timed_out": True,
            "backlog_recovered": last_waiting <= base_backlog + backlog_slack,
            "age_recovered": last_age <= base_age + age_slack,
            "waiting_recovered": last_waiting <= base_backlog + backlog_slack,
            "parked_seen": parked_seen,
            "parked_count": last_parked,
            "baseline_backlog": base_backlog,
            "baseline_age_s": base_age,
            "peak_backlog": seen_peak_backlog,
            "peak_age_s": seen_peak_age,
            "waiting_backlog": last_waiting,
            "oldest_age_s": last_age,
            "samples": samples,
            "reason": (
                "drain did not sustain a waiting-backlog or oldest-age improvement; "
                f"waiting={last_waiting} peak_waiting={seen_peak_backlog} "
                f"age={last_age} peak_age={seen_peak_age} parked={last_parked}"
            ),
        }

    async def _rush_then_drain(self, drain_rps: float) -> None:
        try:
            await asyncio.sleep(self.settings.rush_duration_s)
            self.set_rate(drain_rps)
        except asyncio.CancelledError:
            raise
