"""Trip-band quote: draw near/mid/far at dispatch; rail wait extends the trip.

estimated_ready_at = now + rail_wait + trip. Fleet size is parallelism, not a bouncer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from order_pipeline.sim.core import Quote, QuoteError
from order_pipeline.sim.rail import (
    Occupancy,
    exceeds_busy,
    quoted_wait_s,
    rail_wait_s,
)

TripBand = Literal["near", "mid", "far"]
TRIP_BANDS: tuple[TripBand, ...] = ("near", "mid", "far")
BUSY_DETAIL = "courier busy"


def trip_seconds(band: str, trip_s: Mapping[str, float]) -> float:
    try:
        return trip_s[band]
    except KeyError as exc:
        raise QuoteError(f"unknown trip band: {band!r}") from exc


def _canonical_body(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, default=str, separators=(",", ":"))


def draw_band(body: dict[str, Any]) -> TripBand:
    """Explicit band wins; otherwise a deterministic draw so replay stays Stripe-safe."""
    raw = body.get("band")
    if raw is not None:
        for band in TRIP_BANDS:
            if raw == band:
                return band
        raise QuoteError(f"unknown trip band: {raw!r}; bands are near, mid, far")
    digest = hashlib.sha256(_canonical_body(body).encode()).digest()
    return TRIP_BANDS[digest[0] % len(TRIP_BANDS)]


def quote_dispatch(
    body: dict[str, Any],
    now: datetime,
    *,
    trip_s: Mapping[str, float],
    fleet_size: int = 8,
    busy_multiple: int = 3,
    occupancy: Occupancy = (),
) -> Quote:
    band = draw_band(body)
    trip = trip_seconds(band, trip_s)
    payload: dict[str, Any] = {"band": band, "trip_s": trip}
    extras = {key: value for key, value in body.items() if key != "band"}
    if extras:
        payload["request"] = extras
    in_flight = list(occupancy)
    wait = rail_wait_s(now, parallelism=fleet_size, occupancy=in_flight)
    quoted = quoted_wait_s(wait, trip)
    if exceeds_busy(quoted_wait=quoted, service_s=trip, busy_multiple=busy_multiple):
        return Quote(
            estimated_ready_at=now + timedelta(seconds=quoted),
            payload=payload,
            reject_status=429,
            reject_detail=BUSY_DETAIL,
        )
    return Quote(
        estimated_ready_at=now + timedelta(seconds=quoted),
        payload=payload,
    )
