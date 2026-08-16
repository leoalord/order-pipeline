"""Trip-band quote math — near/mid/far seconds; estimated_ready_at = now + trip."""

from datetime import UTC, datetime, timedelta

from order_pipeline.courier.quote import draw_band, quote_dispatch, trip_seconds
from order_pipeline.courier.settings import TripTimes

TRIP = TripTimes().as_map()
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def test_band_seconds_match_config() -> None:
    assert trip_seconds("near", TRIP) == 12.0
    assert trip_seconds("mid", TRIP) == 20.0
    assert trip_seconds("far", TRIP) == 35.0


def test_eta_is_now_plus_trip() -> None:
    near = quote_dispatch({"band": "near"}, NOW, trip_s=TRIP)
    assert near.estimated_ready_at == NOW + timedelta(seconds=12)
    assert near.payload["band"] == "near"

    mid = quote_dispatch({"band": "mid"}, NOW, trip_s=TRIP)
    assert mid.estimated_ready_at == NOW + timedelta(seconds=20)

    far = quote_dispatch({"band": "far"}, NOW, trip_s=TRIP)
    assert far.estimated_ready_at == NOW + timedelta(seconds=35)


def test_draw_without_band_is_deterministic() -> None:
    body = {"order_id": "same-dispatch"}
    assert draw_band(body) == draw_band(body)
    first = quote_dispatch(body, NOW, trip_s=TRIP)
    second = quote_dispatch(body, NOW, trip_s=TRIP)
    assert first.payload == second.payload
    assert first.estimated_ready_at == second.estimated_ready_at
    assert first.payload["band"] in {"near", "mid", "far"}
