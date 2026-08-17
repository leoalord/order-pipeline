"""Quiet cook quote math — slowest item + extra seconds per additional item.

Rail extends that promise: ETA = now + rail_wait + quiet_cook. 429 at 3× that
ticket's quiet time, not at seat 21. Fuse 80 is a hard occupancy cap.
"""

from datetime import UTC, datetime, timedelta

from order_pipeline.restaurant.quote import quiet_cook_s, quote_accept
from order_pipeline.restaurant.settings import CookTimes

COOK = CookTimes().as_map()
EXTRA = 5.0
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def test_single_item_is_that_items_cook_time() -> None:
    assert quiet_cook_s(["chips"], COOK, EXTRA) == 12.0
    assert quiet_cook_s(["taco"], COOK, EXTRA) == 18.0
    assert quiet_cook_s(["burrito"], COOK, EXTRA) == 25.0


def test_extras_add_five_seconds_each() -> None:
    assert quiet_cook_s(["chips", "taco"], COOK, EXTRA) == 18.0 + 5.0
    assert quiet_cook_s(["chips", "taco", "burrito"], COOK, EXTRA) == 25.0 + 10.0
    assert quiet_cook_s(["burrito", "burrito"], COOK, EXTRA) == 25.0 + 5.0


def test_empty_rail_eta_is_now_plus_quiet_cook() -> None:
    quoted = quote_accept({"items": ["burrito"]}, NOW, cook_s=COOK, extra_item_s=EXTRA)
    assert quoted.reject_status is None
    assert quoted.estimated_ready_at == NOW + timedelta(seconds=25)
    assert quoted.payload["items"] == ["burrito"]
    assert quoted.payload["quiet_cook_s"] == 25.0


def test_rail_extends_quiet_cook_never_replaces_it() -> None:
    occupancy = [(NOW, NOW + timedelta(seconds=12))] * 20
    quoted = quote_accept(
        {"items": ["burrito"]},
        NOW,
        cook_s=COOK,
        extra_item_s=EXTRA,
        pans=20,
        occupancy=occupancy,
    )
    assert quoted.reject_status is None
    # First of 20 chips frees at +12; burrito still cooks 25 after that.
    assert quoted.estimated_ready_at == NOW + timedelta(seconds=12 + 25)
    assert quoted.payload["quiet_cook_s"] == 25.0


def test_in_progress_pan_uses_only_remaining_time() -> None:
    occupancy = [(NOW - timedelta(seconds=10), NOW + timedelta(seconds=15))]
    quoted = quote_accept(
        {"items": ["burrito"]},
        NOW,
        cook_s=COOK,
        extra_item_s=EXTRA,
        pans=1,
        occupancy=occupancy,
    )
    assert quoted.reject_status is None
    assert quoted.estimated_ready_at == NOW + timedelta(seconds=15 + 25)


def test_elapsed_service_does_not_create_a_false_busy_429() -> None:
    # The occupant started 20s ago and has 40s left. Reapplying its full 60s
    # duration would quote 85s and reject; the real 40s wait + 25s cook accepts.
    occupancy = [(NOW - timedelta(seconds=20), NOW + timedelta(seconds=40))]
    quoted = quote_accept(
        {"items": ["burrito"]},
        NOW,
        cook_s=COOK,
        extra_item_s=EXTRA,
        pans=1,
        busy_multiple=3,
        occupancy=occupancy,
    )
    assert quoted.reject_status is None
    assert quoted.estimated_ready_at == NOW + timedelta(seconds=40 + 25)


def test_ticket_21_is_accepted_not_a_429() -> None:
    occupancy = [(NOW, NOW + timedelta(seconds=25))] * 20
    quoted = quote_accept(
        {"items": ["burrito"]},
        NOW,
        cook_s=COOK,
        extra_item_s=EXTRA,
        pans=20,
        busy_multiple=3,
        occupancy=occupancy,
    )
    assert quoted.reject_status is None
    assert quoted.estimated_ready_at == NOW + timedelta(seconds=25 + 25)


def test_429_when_quoted_wait_exceeds_three_times_this_tickets_quiet() -> None:
    # One pan, a burrito already promised out to +60. New burrito waits 60 + 25 = 85
    # which is > 3×25.
    occupancy = [(NOW, NOW + timedelta(seconds=60))]
    quoted = quote_accept(
        {"items": ["burrito"]},
        NOW,
        cook_s=COOK,
        extra_item_s=EXTRA,
        pans=1,
        busy_multiple=3,
        occupancy=occupancy,
    )
    assert quoted.reject_status == 429
    assert quoted.reject_detail == "kitchen busy"


def test_quoted_wait_at_exactly_3x_is_not_busy() -> None:
    # wait 50 + cook 25 = 75, which is not > 75.
    occupancy = [(NOW, NOW + timedelta(seconds=50))]
    quoted = quote_accept(
        {"items": ["burrito"]},
        NOW,
        cook_s=COOK,
        extra_item_s=EXTRA,
        pans=1,
        busy_multiple=3,
        occupancy=occupancy,
    )
    assert quoted.reject_status is None
    assert quoted.estimated_ready_at == NOW + timedelta(seconds=75)


def test_rail_fuse_80_429s_even_when_3x_would_allow() -> None:
    # 80 in-flight chips, pans=80 so rail wait is 0; 3× would accept. Fuse must not.
    occupancy = [(NOW, NOW + timedelta(seconds=12))] * 80
    quoted = quote_accept(
        {"items": ["chips"]},
        NOW,
        cook_s=COOK,
        extra_item_s=EXTRA,
        pans=80,
        busy_multiple=3,
        rail_fuse=80,
        occupancy=occupancy,
    )
    assert quoted.reject_status == 429
    occupancy_79 = occupancy[:79]
    allowed = quote_accept(
        {"items": ["chips"]},
        NOW,
        cook_s=COOK,
        extra_item_s=EXTRA,
        pans=80,
        busy_multiple=3,
        rail_fuse=80,
        occupancy=occupancy_79,
    )
    assert allowed.reject_status is None
