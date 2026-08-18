"""Restaurant-only menu-item counters. Courier must not import this."""

from __future__ import annotations

from collections import Counter

from order_pipeline.menu import MENU_ITEM_IDS

OUT_OF_STOCK_DETAIL = "out of stock"
OUT_OF_STOCK_STATUS = 409
DEFAULT_STOCK = 200


class MenuStock:
    """In-memory per-item counts. Decrement is applied only on a new accept effect."""

    def __init__(self, default: int = DEFAULT_STOCK) -> None:
        if default < 1:
            raise ValueError("default stock must be >= 1")
        self._counts = {item: default for item in sorted(MENU_ITEM_IDS)}

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts)

    def set(self, item: str, count: int) -> dict[str, int]:
        if item not in MENU_ITEM_IDS:
            raise ValueError(f"unknown item id: {item!r}; menu is chips, taco, burrito")
        if count < 0:
            raise ValueError("count must be >= 0")
        self._counts[item] = count
        return self.snapshot()

    def unavailable(self, items: list[str]) -> bool:
        """True when any requested item cannot be fulfilled (including a zero counter)."""
        needed = Counter(items)
        return any(self._counts.get(item, 0) < needed[item] for item in needed)

    def decrement(self, items: list[str]) -> dict[str, int]:
        if self.unavailable(items):
            raise RuntimeError("cannot decrement an out-of-stock cart")
        for item, n in Counter(items).items():
            self._counts[item] -= n
        return self.snapshot()
