"""Direct-session intake tests for the paths HTTP cannot reach deterministically.

`test_place_order.py` covers the concurrent race, but a race can only *probably*
exercise the recovery branch — if the requests happen to serialize, every one
takes the fast path and the test passes without raising anything. These tests
force the same states on purpose.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from psycopg.errors import UniqueViolation
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.intake import (
    FingerprintConflict,
    _new_accept_rows,
    body_fingerprint,
    confirm_idempotency_key,
    is_place_key_unique_violation,
    place_order,
    replay_existing,
)
from order_pipeline.models import (
    INTAKE_PLACE_KEY_UNIQUE,
    IntakeKey,
    Order,
    OrderEvent,
    WorkItem,
)

TTL_HOURS = 48


def _place_key(label: str) -> str:
    return f"test-{label}-{uuid.uuid4()}"


def _accept(factory: sessionmaker[Session], place_key: str, items: list[str]) -> uuid.UUID:
    with factory.begin() as session:
        order = place_order(
            session,
            place_key=place_key,
            items=items,
            cohort_id=None,
            ttl_hours=TTL_HOURS,
        )
        return order.id


def test_losing_racer_hits_the_pinned_constraint_and_leaves_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    place_key = _place_key("dup")
    _accept(session_factory, place_key, ["burrito"])

    now = datetime.now(UTC)
    order, event, work_item, intake_key = _new_accept_rows(
        place_key=place_key,
        items=["burrito"],
        cohort_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        fingerprint=body_fingerprint(items=["burrito"], cohort_id=None),
        now=now,
        expires_at=now + timedelta(hours=TTL_HOURS),
    )
    loser_order_id = order.id

    # Exactly what a racer that lost the insert does: order, event and work item
    # go in first, then the place-key collides.
    with pytest.raises(IntegrityError) as caught:
        with session_factory.begin() as session:
            session.add(order)
            session.flush()
            session.add_all([event, work_item, intake_key])
            session.flush()

    orig = caught.value.orig
    assert isinstance(orig, UniqueViolation)
    assert orig.diag.constraint_name == INTAKE_PLACE_KEY_UNIQUE
    assert is_place_key_unique_violation(caught.value) is True

    # The whole accept rolled back together — timeline B, proven deterministically.
    with session_factory() as session:
        assert session.get(Order, loser_order_id) is None
        assert (
            session.scalars(
                select(OrderEvent).where(OrderEvent.order_id == loser_order_id)
            ).one_or_none()
            is None
        )
        assert (
            session.scalars(
                select(WorkItem).where(WorkItem.order_id == loser_order_id)
            ).one_or_none()
            is None
        )


def test_replay_after_violation_returns_the_winning_order(
    session_factory: sessionmaker[Session],
) -> None:
    place_key = _place_key("replay")
    winner_id = _accept(session_factory, place_key, ["burrito"])

    with session_factory.begin() as session:
        replayed = replay_existing(
            session,
            place_key=place_key,
            fingerprint=body_fingerprint(items=["burrito"], cohort_id=None),
            now=datetime.now(UTC),
        )
        assert replayed.id == winner_id
        assert replayed.state == "placed"


def test_replay_after_violation_conflicts_on_a_different_cart(
    session_factory: sessionmaker[Session],
) -> None:
    place_key = _place_key("replay-conflict")
    _accept(session_factory, place_key, ["burrito"])

    with session_factory.begin() as session:
        with pytest.raises(FingerprintConflict):
            replay_existing(
                session,
                place_key=place_key,
                fingerprint=body_fingerprint(items=["taco"], cohort_id=None),
                now=datetime.now(UTC),
            )


def test_expired_key_mints_a_new_order_and_reuses_the_row(
    session_factory: sessionmaker[Session],
) -> None:
    place_key = _place_key("ttl")
    first_id = _accept(session_factory, place_key, ["burrito"])

    past_ttl = datetime.now(UTC) + timedelta(hours=TTL_HOURS + 1)
    with session_factory.begin() as session:
        second = place_order(
            session,
            place_key=place_key,
            items=["burrito"],
            cohort_id=None,
            ttl_hours=TTL_HOURS,
            now=past_ttl,
        )
        second_id = second.id

    assert second_id != first_id

    with session_factory() as session:
        rows = session.scalars(select(IntakeKey).where(IntakeKey.place_key == place_key)).all()
        assert len(rows) == 1
        assert rows[0].order_id == second_id

        # Both orders keep their own confirm work item with their own stored key.
        for order_id in (first_id, second_id):
            work_items = session.scalars(
                select(WorkItem).where(WorkItem.order_id == order_id)
            ).all()
            assert len(work_items) == 1
            assert work_items[0].idempotency_key == confirm_idempotency_key(order_id)
            assert work_items[0].status == "pending"


def test_within_ttl_the_same_key_replays_without_a_second_order(
    session_factory: sessionmaker[Session],
) -> None:
    place_key = _place_key("fastpath")
    first_id = _accept(session_factory, place_key, ["chips", "taco"])
    second_id = _accept(session_factory, place_key, ["chips", "taco"])
    assert first_id == second_id

    with session_factory() as session:
        orders = session.scalars(select(Order).where(Order.id == first_id)).all()
        assert len(orders) == 1
        events = session.scalars(select(OrderEvent).where(OrderEvent.order_id == first_id)).all()
        assert len(events) == 1
        assert events[0].applied is True
