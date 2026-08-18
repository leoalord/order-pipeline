"""Restaurant sim FastAPI app — first impl of the shared sim core."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response

from order_pipeline.restaurant.quote import quiet_cook_s, quote_accept
from order_pipeline.restaurant.settings import RSIMSettings
from order_pipeline.restaurant.status import kitchen_status
from order_pipeline.sim.app import IdempotencyKeyHeader, create_sim_app
from order_pipeline.sim.core import Quote, QuoteError, SimCore
from order_pipeline.sim.drop import DroppedResponse
from order_pipeline.sim.faults import FaultState
from order_pipeline.sim.ledger import Effect, EffectLedger


def _occupancy(
    ledger: EffectLedger,
    now: datetime,
    *,
    cook_s: dict[str, float],
    extra_item_s: float,
) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    effects = ledger.list_effects()
    voided_accept_keys = {
        accept_key
        for effect in effects
        if effect.payload.get("kind") == "void" and effect.payload.get("voided") is True
        if isinstance((accept_key := effect.payload.get("accept_key")), str)
    }
    for effect in effects:
        if effect.payload.get("kind") == "void" or effect.idempotency_key in voided_accept_keys:
            continue
        if effect.estimated_ready_at <= now:
            continue
        cook = _effect_cook_s(effect, cook_s=cook_s, extra_item_s=extra_item_s)
        start = effect.estimated_ready_at - timedelta(seconds=cook)
        windows.append((start, effect.estimated_ready_at))
    return windows


def _effect_cook_s(
    effect: Effect,
    *,
    cook_s: dict[str, float],
    extra_item_s: float,
) -> float:
    raw = effect.payload.get("quiet_cook_s")
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    items = effect.payload.get("items")
    if isinstance(items, list) and items and all(isinstance(item, str) for item in items):
        try:
            return quiet_cook_s(items, cook_s, extra_item_s)
        except QuoteError:
            return 0.0
    return 0.0


def build_app(
    settings: RSIMSettings,
    *,
    now_fn: Callable[[], datetime] | None = None,
    blackout_hang_s: float | None = None,
) -> FastAPI:
    cook_s = settings.cook_s.as_map()
    extra_item_s = settings.extra_item_s
    ledger = EffectLedger(settings.ledger_path)

    def quote(body: dict[str, Any], now: datetime) -> Quote:
        return quote_accept(
            body,
            now,
            cook_s=cook_s,
            extra_item_s=extra_item_s,
            pans=settings.kitchen_pans,
            busy_multiple=settings.busy_multiple,
            rail_fuse=settings.rail_fuse,
            occupancy=_occupancy(ledger, now, cook_s=cook_s, extra_item_s=extra_item_s),
        )

    def status_at(
        *,
        accepted_at: datetime,
        estimated_ready_at: datetime,
        now: datetime,
        payload: dict[str, Any],
    ) -> str:
        cook = 0.0
        raw = payload.get("quiet_cook_s")
        if isinstance(raw, int | float) and not isinstance(raw, bool):
            cook = float(raw)
        else:
            items = payload.get("items")
            if isinstance(items, list) and items and all(isinstance(item, str) for item in items):
                try:
                    cook = quiet_cook_s(items, cook_s, extra_item_s)
                except QuoteError:
                    cook = 0.0
        return kitchen_status(
            accepted_at=accepted_at,
            estimated_ready_at=estimated_ready_at,
            now=now,
            quiet_cook_s=cook,
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
    app = create_sim_app(title="Restaurant sim", core=core, allow_fail_void=True)

    @app.post("/void")
    def post_void(
        body: dict[str, Any],
        idempotency_key: IdempotencyKeyHeader,
    ) -> Response:
        outcome = core.void(idempotency_key, body)
        if outcome.action == "blackout":
            if core.blackout_hang_s > 0:
                time.sleep(core.blackout_hang_s)
            return DroppedResponse()
        if outcome.body is None:
            raise HTTPException(
                status_code=outcome.status_code,
                detail=outcome.detail or "sim error",
            )
        return JSONResponse(outcome.body, status_code=outcome.status_code)

    return app


def create_app() -> FastAPI:
    return build_app(RSIMSettings())
