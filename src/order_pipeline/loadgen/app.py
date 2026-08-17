"""Loadgen admin HTTP: load scenarios plus deterministic outage fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from order_pipeline.loadgen.client import HttpPipelineClient, PipelineClient
from order_pipeline.loadgen.doom_confirm import (
    DoomConfirmClient,
    DoomConfirmError,
    DoomConfirmFixture,
    DoomConfirmRace,
    HttpDoomConfirmClient,
)
from order_pipeline.loadgen.driver import OpenLoopDriver
from order_pipeline.loadgen.settings import LoadgenSettings


async def _read_json_object(request: Request) -> dict[str, Any]:
    length = request.headers.get("content-length", "0")
    if length == "0" or not length:
        return {}
    try:
        raw = await request.json()
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def create_app(
    settings: LoadgenSettings | None = None,
    *,
    client: PipelineClient | None = None,
    doom_client: DoomConfirmClient | None = None,
) -> FastAPI:
    cfg = settings or LoadgenSettings()
    pipeline = client or HttpPipelineClient(
        cfg.api_base_url,
        place_timeout_s=cfg.place_timeout_s,
        snapshot_timeout_s=cfg.snapshot_timeout_s,
    )
    driver = OpenLoopDriver(cfg, pipeline)
    fixture_client = doom_client
    if fixture_client is None and client is None:
        fixture_client = HttpDoomConfirmClient(
            cfg.api_base_url,
            cfg.restaurant_admin_url,
            timeout_s=max(cfg.place_timeout_s, cfg.snapshot_timeout_s),
        )
    doom_fixture = (
        DoomConfirmFixture(
            fixture_client,
            confirm_deadline_s=cfg.confirm_deadline_s,
        )
        if fixture_client is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await driver.start()
        yield
        await driver.aclose()
        if doom_fixture is not None:
            await doom_fixture.aclose()

    app = FastAPI(title="Order Pipeline Loadgen", lifespan=lifespan)
    app.state.driver = driver
    app.state.settings = cfg

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status")
    def status() -> dict[str, Any]:
        return driver.snapshot_status()

    @app.post("/calibrate")
    async def calibrate(request: Request) -> dict[str, Any]:
        body = await _read_json_object(request)
        try:
            return await driver.calibrate(
                step_s=_optional_float(body.get("step_s")),
                start_rps=_optional_float(body.get("start_rps")),
                factor=_optional_float(body.get("factor")),
                max_rps=_optional_float(body.get("max_rps")),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/scenario/steady")
    def steady() -> dict[str, Any]:
        try:
            rate = driver.steady_rps()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        driver.set_rate(rate)
        return {"rate_rps": rate, "h": driver.h, "cohort_id": str(driver.cohort_id)}

    @app.post("/scenario/rush")
    async def rush(
        request: Request,
        mult: float | None = Query(default=None, gt=0),
    ) -> dict[str, Any]:
        body = await _read_json_object(request)
        factor = 1.0
        if mult is not None:
            factor = mult
        body_mult = _optional_float(body.get("mult"))
        if body_mult is not None:
            factor = body_mult
        if factor <= 0:
            raise HTTPException(status_code=400, detail="mult must be > 0")
        try:
            plan = await driver.start_rush(mult=factor)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        plan["h"] = driver.h
        plan["cohort_id"] = str(driver.cohort_id)
        return plan

    @app.post("/stop")
    async def stop() -> dict[str, Any]:
        await driver.stop_and_drain()
        return driver.snapshot_status()

    @app.post("/cohort/new")
    async def new_cohort() -> dict[str, str]:
        if doom_fixture is not None:
            try:
                await doom_fixture.clear()
            except DoomConfirmError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        cohort_id = driver.new_cohort()
        return {"cohort_id": str(cohort_id)}

    @app.post("/beat/doom-confirm")
    async def doom_confirm() -> dict[str, Any]:
        if doom_fixture is None:
            raise HTTPException(status_code=503, detail="doom-confirm fixture client unavailable")
        try:
            result = await doom_fixture.create(driver.cohort_id)
        except DoomConfirmRace as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DoomConfirmError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "order_ids": [str(order_id) for order_id in result.order_ids],
            "cohort_id": str(result.cohort_id),
        }

    return app


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(status_code=400, detail="expected a number")
    return float(value)
