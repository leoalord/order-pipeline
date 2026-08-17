"""Kitchen poll statuses. queued = waiting for a pan; cooking = on a pan."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

KitchenStatus = Literal["queued", "cooking", "ready"]


def cooking_started_at(estimated_ready_at: datetime, quiet_cook_s: float) -> datetime:
    return estimated_ready_at - timedelta(seconds=quiet_cook_s)


def kitchen_status(
    *,
    accepted_at: datetime,
    estimated_ready_at: datetime,
    now: datetime,
    quiet_cook_s: float,
) -> KitchenStatus:
    if now >= estimated_ready_at:
        return "ready"
    if quiet_cook_s <= 0:
        if now >= accepted_at:
            return "cooking"
        return "queued"
    start = cooking_started_at(estimated_ready_at, quiet_cook_s)
    if now >= start:
        return "cooking"
    return "queued"
