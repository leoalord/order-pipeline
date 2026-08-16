"""Unit tests for intake fingerprint, stored confirm key, and cart schema."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from order_pipeline.api.schemas import PlaceOrderRequest
from order_pipeline.intake import (
    DEFAULT_COHORT_ID,
    _new_accept_rows,
    body_fingerprint,
    confirm_idempotency_key,
)
from order_pipeline.menu import MAX_CART_ITEMS, MENU_ITEM_IDS
from order_pipeline.models import IntakeKey, Order, OrderEvent, WorkItem


def test_menu_ids_match_config_cook_keys() -> None:
    assert MENU_ITEM_IDS == frozenset({"chips", "taco", "burrito"})
    assert MAX_CART_ITEMS == 3


def test_fingerprint_is_stable_for_same_cart() -> None:
    left = body_fingerprint(items=["burrito", "chips"], cohort_id=None)
    right = body_fingerprint(items=["burrito", "chips"], cohort_id=None)
    assert left == right
    assert len(left) == 64


def test_fingerprint_changes_when_cart_changes() -> None:
    burrito = body_fingerprint(items=["burrito"], cohort_id=None)
    taco = body_fingerprint(items=["taco"], cohort_id=None)
    swapped = body_fingerprint(items=["chips", "burrito"], cohort_id=None)
    original = body_fingerprint(items=["burrito", "chips"], cohort_id=None)
    assert burrito != taco
    assert swapped != original


def test_fingerprint_includes_explicit_cohort() -> None:
    cohort = uuid4()
    without = body_fingerprint(items=["taco"], cohort_id=None)
    with_cohort = body_fingerprint(items=["taco"], cohort_id=cohort)
    assert without != with_cohort


def test_confirm_idempotency_key_is_stored_tuple_string() -> None:
    order_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    key = confirm_idempotency_key(order_id)
    assert key == f"({order_id}, confirm)"
    assert "confirm" in key
    assert str(order_id) in key


def test_new_accept_rows_always_include_confirm_work_item() -> None:
    now = datetime.now(UTC)
    order, event, work_item, intake = _new_accept_rows(
        place_key="diner-1",
        items=["burrito"],
        cohort_id=DEFAULT_COHORT_ID,
        fingerprint="abc",
        now=now,
        expires_at=now + timedelta(hours=48),
    )
    assert isinstance(order, Order)
    assert isinstance(event, OrderEvent)
    assert isinstance(work_item, WorkItem)
    assert isinstance(intake, IntakeKey)
    assert order.state == "placed"
    assert order.accepted_at == now
    assert event.to_state == "placed"
    assert event.from_state is None
    assert work_item.work_type == "confirm"
    assert work_item.status == "pending"
    assert work_item.idempotency_key == confirm_idempotency_key(order.id)
    assert work_item.order_id == order.id
    assert intake.order_id == order.id
    assert (intake.expires_at - intake.created_at) == timedelta(hours=48)


@pytest.mark.parametrize(
    "payload",
    [
        {"items": []},
        {"items": ["burrito", "taco", "chips", "taco"]},
        {"items": ["chicken_burrito"]},
        {"items": ["burrito"], "note": "extra"},
        {"foo": 1},
    ],
)
def test_malformed_and_over_cap_carts_fail_schema(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PlaceOrderRequest.model_validate(payload)


def test_valid_cart_and_optional_cohort_parse() -> None:
    body = PlaceOrderRequest.model_validate({"items": ["chips", "taco"]})
    assert body.items == ["chips", "taco"]
    assert body.cohort_id is None
    cohort = uuid4()
    with_cohort = PlaceOrderRequest.model_validate({"items": ["burrito"], "cohort_id": str(cohort)})
    assert with_cohort.cohort_id == cohort
