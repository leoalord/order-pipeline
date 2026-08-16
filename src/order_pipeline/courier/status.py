"""Courier poll statuses. assigned remains valid; with no fleet wait, the trip starts now."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

CourierStatus = Literal["assigned", "en_route", "delivered"]


def courier_status(
    *,
    accepted_at: datetime,
    estimated_ready_at: datetime,
    now: datetime,
) -> CourierStatus:
    if now >= estimated_ready_at:
        return "delivered"
    if now >= accepted_at:
        return "en_route"
    return "assigned"
