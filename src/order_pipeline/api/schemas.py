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


class TerminalRates(BaseModel):
    delivered: float
    cancelled: float
    failed: float


class E2eLatency(BaseModel):
    p50: float | None
    p95: float | None


class Conservation(BaseModel):
    accepted: int
    delivered: int
    cancelled: int
    failed: int
    in_flight: int
    parked: int
    residual: int


class TraceEvent(BaseModel):
    id: UUID
    from_state: str | None
    to_state: str
    actor: str
    cause: str
    timestamp: datetime
    applied: bool


class TraceAttempt(BaseModel):
    id: UUID
    work_item_id: UUID
    work_type: str
    started_at: datetime
    ended_at: datetime | None
    lease_owner: str
    outcome: str | None


class OrderTrace(BaseModel):
    order_id: UUID
    order_events: list[TraceEvent]
    attempts: list[TraceAttempt]


class SnapshotResponse(BaseModel):
    """Additive JSON. Field names are frozen once shipped — never rename."""

    cohort_id: UUID
    stages: dict[str, int]
    terminal_rates_per_min: TerminalRates
    e2e_latency_s: E2eLatency
    conservation: Conservation
    duplicate_attempts: int
    duplicate_effects: int | None
    startup_scan: int
    invalid_transitions: int
    state_vs_last_order_events_mismatches: int
    currently_leased: int
    trace: OrderTrace | None
