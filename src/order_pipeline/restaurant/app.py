"""Restaurant sim FastAPI app — first impl of the shared sim core."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import FastAPI

from order_pipeline.restaurant.quote import quote_accept
from order_pipeline.restaurant.settings import RSIMSettings
from order_pipeline.restaurant.status import kitchen_status
from order_pipeline.sim.app import create_sim_app
from order_pipeline.sim.core import Quote, SimCore
from order_pipeline.sim.faults import FaultState
from order_pipeline.sim.ledger import EffectLedger


def build_app(
    settings: RSIMSettings,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> FastAPI:
    cook_s = settings.cook_s.as_map()
    extra_item_s = settings.extra_item_s

    def quote(body: dict[str, Any], now: datetime) -> Quote:
        return quote_accept(body, now, cook_s=cook_s, extra_item_s=extra_item_s)

    core = SimCore(
        ledger=EffectLedger(settings.ledger_path),
        faults=FaultState(),
        quote=quote,
        status_at=kitchen_status,
        flaky_5xx_pct=settings.flaky_5xx_pct,
        flaky_drop_pct=settings.flaky_drop_pct,
        now_fn=now_fn,
    )
    return create_sim_app(title="Restaurant sim", core=core)


def create_app() -> FastAPI:
    return build_app(RSIMSettings())
