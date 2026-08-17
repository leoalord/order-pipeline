"""Trip-band quote math — near/mid/far seconds; rail wait extends the trip."""

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
    assert near.payload["trip_s"] == 12.0
    assert near.reject_status is None

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

    observed = {draw_band({"order_id": f"order-{index}"}) for index in range(30)}
    assert observed == {"near", "mid", "far"}


def test_fleet_wait_extends_trip_bike_9_is_not_a_429() -> None:
    occupancy = [(NOW, NOW + timedelta(seconds=12))] * 8
    quoted = quote_dispatch(
        {"band": "near"},
        NOW,
        trip_s=TRIP,
        fleet_size=8,
        busy_multiple=3,
        occupancy=occupancy,
    )
    assert quoted.reject_status is None
    assert quoted.estimated_ready_at == NOW + timedelta(seconds=12 + 12)


def test_in_progress_bike_uses_only_remaining_trip_time() -> None:
    occupancy = [(NOW - timedelta(seconds=7), NOW + timedelta(seconds=5))]
    quoted = quote_dispatch(
        {"band": "near"},
        NOW,
        trip_s=TRIP,
        fleet_size=1,
        occupancy=occupancy,
    )
    assert quoted.reject_status is None
    assert quoted.estimated_ready_at == NOW + timedelta(seconds=5 + 12)


def test_courier_429_when_quoted_trip_exceeds_3x_that_band() -> None:
    occupancy = [(NOW, NOW + timedelta(seconds=30))]
    quoted = quote_dispatch(
        {"band": "near"},
        NOW,
        trip_s=TRIP,
        fleet_size=1,
        busy_multiple=3,
        occupancy=occupancy,
    )
    assert quoted.reject_status == 429
    assert quoted.reject_detail == "courier busy"


def test_courier_busy_is_per_band_not_global() -> None:
    # Far's quiet trip is 35; wait 30 + 35 = 65 is not > 3×35=105.
    occupancy = [(NOW, NOW + timedelta(seconds=30))]
    far = quote_dispatch(
        {"band": "far"},
        NOW,
        trip_s=TRIP,
        fleet_size=1,
        busy_multiple=3,
        occupancy=occupancy,
    )
    assert far.reject_status is None
    near = quote_dispatch(
        {"band": "near"},
        NOW,
        trip_s=TRIP,
        fleet_size=1,
        busy_multiple=3,
        occupancy=occupancy,
    )
    assert near.reject_status == 429
