"""Kitchen poll statuses. queued remains valid; with no pans, cooking starts now."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

KitchenStatus = Literal["queued", "cooking", "ready"]


def kitchen_status(
    *,
    accepted_at: datetime,
    estimated_ready_at: datetime,
    now: datetime,
) -> KitchenStatus:
    if now >= estimated_ready_at:
        return "ready"
    if now >= accepted_at:
        return "cooking"
    return "queued"
