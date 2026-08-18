"""FastAPI factory for a sim: health, accept, poll, GET-by-key, admin."""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, Response

from order_pipeline.sim.admin import admin_router
from order_pipeline.sim.core import SimCore
from order_pipeline.sim.drop import DroppedResponse

IdempotencyKeyHeader = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        description="Stripe-style idempotency key; retries reuse the same key",
    ),
]


def mount_sim_routes(app: FastAPI, core: SimCore, *, allow_fail_void: bool) -> None:
    def blackout_drop() -> DroppedResponse | None:
        if not core.blackout_active():
            return None
        if core.blackout_hang_s > 0:
            time.sleep(core.blackout_hang_s)
        return DroppedResponse()

    @app.get("/health")
    def health() -> dict[str, str]:
        try:
            core.ping()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="ledger unavailable") from exc
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        return health()

    @app.post("/accept")
    def post_accept(
        body: dict[str, Any],
        idempotency_key: IdempotencyKeyHeader,
    ) -> Response:
        outcome = core.accept(idempotency_key, body)
        if outcome.action == "blackout":
            if core.blackout_hang_s > 0:
                time.sleep(core.blackout_hang_s)
            return DroppedResponse()
        if outcome.action == "drop":
            return DroppedResponse()
        if outcome.body is None:
            raise HTTPException(
                status_code=outcome.status_code,
                detail=outcome.detail or "sim error",
            )
        return JSONResponse(outcome.body, status_code=outcome.status_code)

    @app.get("/tickets/{ticket_id}")
    def poll_ticket(ticket_id: str) -> Response:
        unavailable = blackout_drop()
        if unavailable is not None:
            return unavailable
        ticket = core.poll(ticket_id)
        if ticket is None:
            raise HTTPException(status_code=404, detail="ticket not found")
        return JSONResponse(ticket)

    @app.get("/keys/{idempotency_key}")
    def get_by_key(idempotency_key: str) -> Response:
        unavailable = blackout_drop()
        if unavailable is not None:
            return unavailable
        ticket = core.get_by_key(idempotency_key)
        if ticket is None:
            raise HTTPException(status_code=404, detail="key not found")
        return JSONResponse(ticket)

    app.include_router(admin_router(core, allow_fail_void=allow_fail_void))


def create_sim_app(*, title: str, core: SimCore, allow_fail_void: bool = False) -> FastAPI:
    app = FastAPI(title=title)
    mount_sim_routes(app, core, allow_fail_void=allow_fail_void)
    return app
