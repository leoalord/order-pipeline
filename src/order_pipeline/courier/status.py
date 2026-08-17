"""Courier poll statuses. assigned = waiting for a bike; en_route = on a bike."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

CourierStatus = Literal["assigned", "en_route", "delivered"]


def trip_started_at(estimated_ready_at: datetime, trip_s: float) -> datetime:
    return estimated_ready_at - timedelta(seconds=trip_s)


def courier_status(
    *,
    accepted_at: datetime,
    estimated_ready_at: datetime,
    now: datetime,
    trip_s: float,
) -> CourierStatus:
    if now >= estimated_ready_at:
        return "delivered"
    if trip_s <= 0:
        if now >= accepted_at:
            return "en_route"
        return "assigned"
    start = trip_started_at(estimated_ready_at, trip_s)
    if now >= start:
        return "en_route"
    return "assigned"
