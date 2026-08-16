"""Trip-band quote: draw near/mid/far at dispatch; estimated_ready_at = now + trip.

Fleet wait extends this later — it must not replace it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from order_pipeline.sim.core import Quote, QuoteError

TripBand = Literal["near", "mid", "far"]
TRIP_BANDS: tuple[TripBand, ...] = ("near", "mid", "far")


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
) -> Quote:
    band = draw_band(body)
    trip = trip_seconds(band, trip_s)
    payload: dict[str, Any] = {"band": band}
    extras = {key: value for key, value in body.items() if key != "band"}
    if extras:
        payload["request"] = extras
    return Quote(
        estimated_ready_at=now + timedelta(seconds=trip),
        payload=payload,
    )
