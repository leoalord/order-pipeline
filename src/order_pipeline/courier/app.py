"""Courier sim FastAPI app — injects trip-band quote + courier statuses into SimCore."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from order_pipeline.courier.quote import quote_dispatch, trip_seconds
from order_pipeline.courier.settings import CSIMSettings
from order_pipeline.courier.status import courier_status
from order_pipeline.sim.app import create_sim_app
from order_pipeline.sim.core import Quote, QuoteError, SimCore
from order_pipeline.sim.faults import FaultState
from order_pipeline.sim.ledger import Effect, EffectLedger


class CourierCapacityCommand(BaseModel):
    """Live fleet-size change used by the presenter controls."""

    fleet_size: int = Field(ge=1, le=64)


class CourierCapacity:
    """Thread-safe, process-local courier fleet capacity."""

    def __init__(self, fleet_size: int) -> None:
        self.boot_fleet_size = fleet_size
        self._fleet_size = fleet_size
        self._lock = threading.Lock()

    def get(self) -> int:
        with self._lock:
            return self._fleet_size

    def set(self, fleet_size: int) -> int:
        with self._lock:
            self._fleet_size = fleet_size
            return self._fleet_size

    def view(self) -> dict[str, int]:
        return {
            "fleet_size": self.get(),
            "boot_fleet_size": self.boot_fleet_size,
            "min_fleet_size": 1,
            "max_fleet_size": 64,
        }


def _occupancy(
    ledger: EffectLedger,
    now: datetime,
    *,
    trip_s: dict[str, float],
) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    for effect in ledger.list_effects():
        if effect.estimated_ready_at <= now:
            continue
        trip = _effect_trip_s(effect, trip_s=trip_s)
        start = effect.estimated_ready_at - timedelta(seconds=trip)
        windows.append((start, effect.estimated_ready_at))
    return windows


def _effect_trip_s(effect: Effect, *, trip_s: dict[str, float]) -> float:
    raw = effect.payload.get("trip_s")
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    band = effect.payload.get("band")
    if isinstance(band, str):
        try:
            return trip_seconds(band, trip_s)
        except QuoteError:
            return 0.0
    return 0.0


def build_app(
    settings: CSIMSettings,
    *,
    now_fn: Callable[[], datetime] | None = None,
    blackout_hang_s: float | None = None,
) -> FastAPI:
    trip_s = settings.trip_s.as_map()
    ledger = EffectLedger(settings.ledger_path)
    capacity = CourierCapacity(settings.fleet_size)

    def quote(body: dict[str, Any], now: datetime) -> Quote:
        return quote_dispatch(
            body,
            now,
            trip_s=trip_s,
            fleet_size=capacity.get(),
            busy_multiple=settings.busy_multiple,
            occupancy=_occupancy(ledger, now, trip_s=trip_s),
        )

    def status_at(
        *,
        accepted_at: datetime,
        estimated_ready_at: datetime,
        now: datetime,
        payload: dict[str, Any],
    ) -> str:
        trip = 0.0
        raw = payload.get("trip_s")
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            trip = float(raw)
        else:
            band = payload.get("band")
            if isinstance(band, str):
                try:
                    trip = trip_seconds(band, trip_s)
                except QuoteError:
                    trip = 0.0
        return courier_status(
            accepted_at=accepted_at,
            estimated_ready_at=estimated_ready_at,
            now=now,
            trip_s=trip,
        )

    core = SimCore(
        ledger=ledger,
        faults=FaultState(now_fn=now_fn),
        quote=quote,
        status_at=status_at,
        flaky_5xx_pct=settings.flaky_5xx_pct,
        flaky_drop_pct=settings.flaky_drop_pct,
        now_fn=now_fn,
        blackout_hang_s=(
            settings.sim_timeout_s + 0.5 if blackout_hang_s is None else blackout_hang_s
        ),
    )
    app = create_sim_app(title="Courier sim", core=core)

    @app.get("/admin/capacity")
    def get_capacity() -> dict[str, int]:
        return capacity.view()

    @app.post("/admin/capacity")
    def set_capacity(command: CourierCapacityCommand) -> dict[str, int]:
        capacity.set(command.fleet_size)
        return capacity.view()

    return app


def create_app() -> FastAPI:
    return build_app(CSIMSettings())
