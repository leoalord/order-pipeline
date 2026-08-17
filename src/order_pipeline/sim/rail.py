"""Parallelism rail: pans/bikes as slots. Quote wait extends service time; never replaces it."""

from __future__ import annotations

import heapq
from collections.abc import Sequence
from datetime import datetime

# (service_started_at, estimated_ready_at) for tickets that are not yet done.
Occupancy = Sequence[tuple[datetime, datetime]]


def rail_wait_s(
    now: datetime,
    *,
    parallelism: int,
    occupancy: Occupancy,
) -> float:
    """Seconds until a slot is free for a new ticket. Zero if a slot is free now.

    ``parallelism`` is pans or bikes — how many tickets run at once, not a bouncer.
    Existing tickets occupy a slot from ``service_started_at`` until ``estimated_ready_at``.
    """
    if parallelism < 1:
        raise ValueError("parallelism must be >= 1")
    slots: list[datetime] = [now] * parallelism
    heapq.heapify(slots)
    for started_at, ready_at in sorted(occupancy, key=lambda window: window[0]):
        free_at = heapq.heappop(slots)
        duration = ready_at - started_at
        actual_start = free_at if free_at > started_at else started_at
        heapq.heappush(slots, actual_start + duration)
    wait = (slots[0] - now).total_seconds()
    return max(0.0, wait)


def quoted_wait_s(rail_wait: float, service_s: float) -> float:
    """Total promise: rail wait plus the quiet service time (cook or trip)."""
    return rail_wait + service_s


def exceeds_busy(*, quoted_wait: float, service_s: float, busy_multiple: int) -> bool:
    """429 when the promise is stretched past N× this ticket's own quiet time."""
    return quoted_wait > busy_multiple * service_s


def exceeds_fuse(*, in_flight: int, fuse: int | None) -> bool:
    """Hard cap so a bad busy_multiple cannot grow occupancy forever."""
    return fuse is not None and in_flight >= fuse
