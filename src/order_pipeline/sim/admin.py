"""Shared /admin/faults + /admin/ledger router."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from order_pipeline.sim.core import ExistingEffectConflict, SimCore


class FaultsPost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["clear", "5xx_before", "5xx_after", "drop", "blackout", "fail_void"]
    seconds: float | None = None
    mix: Literal["off", "on"] | None = None

    @model_validator(mode="after")
    def blackout_needs_seconds(self) -> Self:
        if self.mode == "blackout" and (self.seconds is None or self.seconds <= 0):
            raise ValueError("blackout requires seconds > 0")
        return self


class ConfirmUnavailableTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: Annotated[str, Field(min_length=1)]
    until: datetime

    @field_validator("until")
    @classmethod
    def deadline_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("until must include a timezone")
        return value


class ConfirmUnavailablePost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Empty is the targeted-only cleanup used by POST /cohort/new. It does not
    # mutate global blackout or the always-on random mix.
    targets: list[ConfirmUnavailableTarget]

    @field_validator("targets")
    @classmethod
    def keys_are_unique(
        cls, targets: list[ConfirmUnavailableTarget]
    ) -> list[ConfirmUnavailableTarget]:
        keys = [target.idempotency_key for target in targets]
        if len(keys) != len(set(keys)):
            raise ValueError("confirm-unavailable target keys must be unique")
        return targets


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

    @router.post("/faults/confirm-unavailable")
    def post_confirm_unavailable(body: ConfirmUnavailablePost) -> dict[str, Any]:
        targets = {target.idempotency_key: target.until for target in body.targets}
        try:
            return core.replace_confirm_unavailable(targets)
        except ExistingEffectConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "confirm effect existed before targeted rule landed",
                    "idempotency_keys": exc.idempotency_keys,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/ledger")
    def get_ledger() -> dict[str, dict[str, int]]:
        return {"counts": core.ledger_counts()}

    return router
