"""Shared /admin/faults + /admin/ledger router."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from order_pipeline.sim.core import SimCore


class FaultsPost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["clear", "5xx_before", "5xx_after", "drop"]


def admin_router(core: SimCore) -> APIRouter:
    router = APIRouter(prefix="/admin")

    @router.get("/faults")
    def get_faults() -> dict[str, Any]:
        return core.faults_view()

    @router.post("/faults")
    def post_faults(body: FaultsPost) -> dict[str, Any]:
        return core.set_fault_command(body.mode)

    @router.get("/ledger")
    def get_ledger() -> dict[str, dict[str, int]]:
        return {"counts": core.ledger_counts()}

    return router
