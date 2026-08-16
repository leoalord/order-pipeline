"""Shared /admin/faults + /admin/ledger router."""

from __future__ import annotations

from typing import Any, Literal, Self

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, model_validator

from order_pipeline.sim.core import SimCore


class FaultsPost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["clear", "5xx_before", "5xx_after", "drop", "blackout"]
    seconds: float | None = None
    mix: Literal["off", "on"] | None = None

    @model_validator(mode="after")
    def blackout_needs_seconds(self) -> Self:
        if self.mode == "blackout" and (self.seconds is None or self.seconds <= 0):
            raise ValueError("blackout requires seconds > 0")
        return self


def admin_router(core: SimCore) -> APIRouter:
    router = APIRouter(prefix="/admin")

    @router.get("/faults")
    def get_faults() -> dict[str, Any]:
        return core.faults_view()

    @router.post("/faults")
    def post_faults(body: FaultsPost) -> dict[str, Any]:
        try:
            return core.set_fault_command(
                body.mode,
                seconds=body.seconds,
                mix=body.mix,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/ledger")
    def get_ledger() -> dict[str, dict[str, int]]:
        return {"counts": core.ledger_counts()}

    return router
