"""Quiet cook quote: slowest item + extra seconds per additional item.

Rail wait *extends* this — estimated_ready_at = now + rail_wait + quiet_cook.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from order_pipeline.menu import MAX_CART_ITEMS, MENU_ITEM_IDS
from order_pipeline.sim.core import Quote, QuoteError
from order_pipeline.sim.rail import (
    Occupancy,
    exceeds_busy,
    exceeds_fuse,
    quoted_wait_s,
    rail_wait_s,
)

BUSY_DETAIL = "kitchen busy"


def quiet_cook_s(
    items: list[str],
    cook_s: Mapping[str, float],
    extra_item_s: float,
) -> float:
    """max(item cook times) + extra_item_s * (n_items - 1) for n_items >= 1."""
    if not items:
        raise QuoteError("items must be a non-empty list")
    try:
        slowest = max(cook_s[item] for item in items)
    except KeyError as exc:
        raise QuoteError(f"unknown item id: {exc.args[0]!r}") from exc
    extras = max(0, len(items) - 1)
    return slowest + extra_item_s * extras


def parse_accept_items(body: dict[str, Any]) -> list[str]:
    raw = body.get("items")
    if not isinstance(raw, list) or not raw:
        raise QuoteError("items must be a non-empty list")
    if len(raw) > MAX_CART_ITEMS:
        raise QuoteError(f"cart exceeds max of {MAX_CART_ITEMS} items")
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str) or item not in MENU_ITEM_IDS:
            raise QuoteError(f"unknown item id: {item!r}; menu is chips, taco, burrito")
        items.append(item)
    return items


def quote_accept(
    body: dict[str, Any],
    now: datetime,
    *,
    cook_s: Mapping[str, float],
    extra_item_s: float,
    pans: int = 20,
    busy_multiple: int = 3,
    rail_fuse: int = 80,
    occupancy: Occupancy = (),
) -> Quote:
    items = parse_accept_items(body)
    cook = quiet_cook_s(items, cook_s, extra_item_s)
    payload = {"items": items, "quiet_cook_s": cook}
    in_flight = list(occupancy)
    if exceeds_fuse(in_flight=len(in_flight), fuse=rail_fuse):
        return Quote(
            estimated_ready_at=now + timedelta(seconds=cook),
            payload=payload,
            reject_status=429,
            reject_detail=BUSY_DETAIL,
        )
    wait = rail_wait_s(now, parallelism=pans, occupancy=in_flight)
    quoted = quoted_wait_s(wait, cook)
    if exceeds_busy(quoted_wait=quoted, service_s=cook, busy_multiple=busy_multiple):
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
