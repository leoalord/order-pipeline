"""Cart mix: mostly 1-item + some 2–3. Bigger tickets take longer."""

from __future__ import annotations

import random

from order_pipeline.menu import MENU_ITEM_IDS

_MENU = tuple(sorted(MENU_ITEM_IDS))


def pick_cart(
    rng: random.Random,
    *,
    one_item_pct: float = 70.0,
    two_item_pct: float = 20.0,
) -> list[str]:
    """Return 1, 2, or 3 menu items. Mix percentages must leave a 3-item remainder."""
    roll = rng.random() * 100.0
    if roll < one_item_pct:
        n = 1
    elif roll < one_item_pct + two_item_pct:
        n = 2
    else:
        n = 3
    return [rng.choice(_MENU) for _ in range(n)]
