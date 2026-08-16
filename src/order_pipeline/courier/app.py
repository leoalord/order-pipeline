"""Courier sim FastAPI app — injects trip-band quote + courier statuses into SimCore."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import FastAPI

from order_pipeline.courier.quote import quote_dispatch
from order_pipeline.courier.settings import CSIMSettings
from order_pipeline.courier.status import courier_status
from order_pipeline.sim.app import create_sim_app
from order_pipeline.sim.core import Quote, SimCore
from order_pipeline.sim.faults import FaultState
from order_pipeline.sim.ledger import EffectLedger


def build_app(
    settings: CSIMSettings,
    *,
    now_fn: Callable[[], datetime] | None = None,
    blackout_hang_s: float | None = None,
) -> FastAPI:
    trip_s = settings.trip_s.as_map()

    def quote(body: dict[str, Any], now: datetime) -> Quote:
        return quote_dispatch(body, now, trip_s=trip_s)

    core = SimCore(
        ledger=EffectLedger(settings.ledger_path),
        faults=FaultState(now_fn=now_fn),
        quote=quote,
        status_at=courier_status,
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
