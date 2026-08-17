"""Courier sim FastAPI app — injects trip-band quote + courier statuses into SimCore."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI

from order_pipeline.courier.quote import quote_dispatch, trip_seconds
from order_pipeline.courier.settings import CSIMSettings
from order_pipeline.courier.status import courier_status
from order_pipeline.sim.app import create_sim_app
from order_pipeline.sim.core import Quote, QuoteError, SimCore
from order_pipeline.sim.faults import FaultState
from order_pipeline.sim.ledger import Effect, EffectLedger


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

    def quote(body: dict[str, Any], now: datetime) -> Quote:
        return quote_dispatch(
            body,
            now,
            trip_s=trip_s,
            fleet_size=settings.fleet_size,
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
    return create_sim_app(title="Courier sim", core=core)


def create_app() -> FastAPI:
    return build_app(CSIMSettings())
