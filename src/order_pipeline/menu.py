"""Tiny v1 menu. Item ids match Config `RSIM_COOK_S` keys (`burrito`, not `chicken_burrito`)."""

MENU_ITEM_IDS: frozenset[str] = frozenset({"chips", "taco", "burrito"})

# Design validates schema + order-size cap. Loadgen mix is 1-item plus some 2–3;
# no numeric cap is named, so 3 is the product-consistent hard cap.
MAX_CART_ITEMS = 3
