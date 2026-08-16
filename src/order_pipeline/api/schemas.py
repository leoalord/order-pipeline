from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from order_pipeline.menu import MAX_CART_ITEMS, MENU_ITEM_IDS


class PlaceOrderRequest(BaseModel):
    """POST /orders body. Extra fields are rejected so the fingerprint matches the cart."""

    model_config = ConfigDict(extra="forbid")

    items: list[str] = Field(min_length=1, max_length=MAX_CART_ITEMS)
    cohort_id: UUID | None = None

    @field_validator("items")
    @classmethod
    def known_menu_items(cls, items: list[str]) -> list[str]:
        for item in items:
            if item not in MENU_ITEM_IDS:
                raise ValueError(f"unknown item id: {item!r}; menu is chips, taco, burrito")
        return items


class OrderResponse(BaseModel):
    id: UUID
    state: str
    accepted_at: datetime
    items: list[str]
    cohort_id: UUID
