"""Restaurant-only durable menu-item counters. Courier must not import this."""

from __future__ import annotations

from collections import Counter

from order_pipeline.menu import MENU_ITEM_IDS
from order_pipeline.sim.ledger import CounterInsertResult, Effect, EffectLedger

OUT_OF_STOCK_DETAIL = "out of stock"
OUT_OF_STOCK_STATUS = 409
DEFAULT_STOCK = 10_000
BONUS_RESTORE_STOCK = 200


class MenuStock:
    """SQLite-backed counts committed in the same transaction as an accept effect."""

    def __init__(self, ledger: EffectLedger, default: int = DEFAULT_STOCK) -> None:
        if default < 1:
            raise ValueError("default stock must be >= 1")
        self._ledger = ledger
        self._items = tuple(sorted(MENU_ITEM_IDS))
        self._ledger.initialize_counters({item: default for item in self._items})

    def snapshot(self) -> dict[str, int]:
        return self._ledger.counter_snapshot(self._items)

    def set(self, item: str, count: int) -> dict[str, int]:
        if item not in MENU_ITEM_IDS:
            raise ValueError(f"unknown item id: {item!r}; menu is chips, taco, burrito")
        if count < 0:
            raise ValueError("count must be >= 0")
        self._ledger.set_counter(item, count)
        return self.snapshot()

    def unavailable(self, items: list[str]) -> bool:
        """True when any requested item cannot be fulfilled (including a zero counter)."""
        needed = Counter(items)
        counts = self.snapshot()
        return any(counts.get(item, 0) < needed[item] for item in needed)

    def insert_accept(self, effect: Effect, items: list[str]) -> CounterInsertResult:
        """Commit the first accept effect and its multiplicity-aware decrement."""
        return self._ledger.insert_with_counter_decrements(effect, Counter(items))
