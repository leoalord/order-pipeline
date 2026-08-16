"""Quiet cook quote math — slowest item + extra seconds per additional item."""

from order_pipeline.restaurant.quote import quiet_cook_s
from order_pipeline.restaurant.settings import CookTimes

COOK = CookTimes().as_map()
EXTRA = 5.0


def test_single_item_is_that_items_cook_time() -> None:
    assert quiet_cook_s(["chips"], COOK, EXTRA) == 12.0
    assert quiet_cook_s(["taco"], COOK, EXTRA) == 18.0
    assert quiet_cook_s(["burrito"], COOK, EXTRA) == 25.0


def test_extras_add_five_seconds_each() -> None:
    assert quiet_cook_s(["chips", "taco"], COOK, EXTRA) == 18.0 + 5.0
    assert quiet_cook_s(["chips", "taco", "burrito"], COOK, EXTRA) == 25.0 + 10.0
    assert quiet_cook_s(["burrito", "burrito"], COOK, EXTRA) == 25.0 + 5.0
