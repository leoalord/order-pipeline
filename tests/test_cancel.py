"""Pre-pivot cancel: placed/confirmed apply; after being_prepared is evidence-only reject.

Session-level tests own both DoD paths. A live compose worker would race an
HTTP-only cancel-while-placed, so those cases commit place+cancel together.
HTTP tests wait until confirmed / being_prepared so they cannot flake.
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.cancel import CAUSE_CANCEL, CancelOutcome, OrderNotFound, cancel_order
from order_pipeline.intake import place_order
from order_pipeline.lifecycle import CAUSE_INVALID
from order_pipeline.models import Order, OrderEvent, WorkItem
from tests.sim_admin import mix_off

TTL_HOURS = 48
API_URL = "http://localhost:8000"
WAIT_TIMEOUT_S = 120.0
POLL_EVERY_S = 0.2


def _place_in_session(session: Session, items: list[str] | None = None) -> Order:
    return place_order(
        session,
        place_key=f"cancel-{uuid.uuid4()}",
        items=items or ["burrito"],
        cohort_id=None,
        ttl_hours=TTL_HOURS,
    )


def _set_state(session: Session, order: Order, state: str) -> None:
    order.state = state
    order.version += 1
    session.flush()


def _applied_cancel_events(session: Session, order_id: uuid.UUID) -> list[OrderEvent]:
    return list(
        session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id,
                OrderEvent.cause == CAUSE_CANCEL,
                OrderEvent.applied.is_(True),
            )
        ).all()
    )


def _invalid_evidence(session: Session, order_id: uuid.UUID) -> list[OrderEvent]:
    return list(
        session.scalars(
            select(OrderEvent).where(
                OrderEvent.order_id == order_id,
                OrderEvent.cause == CAUSE_INVALID,
                OrderEvent.applied.is_(False),
            )
        ).all()
    )


def test_cancel_from_placed_applies_event(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        order = _place_in_session(session)
        order_id = order.id
        result = cancel_order(session, order_id)
        assert result.outcome is CancelOutcome.APPLIED
        assert result.order.state == "cancelled"

    with session_factory() as session:
        stored = session.get(Order, order_id)
        assert stored is not None
        assert stored.state == "cancelled"
        events = _applied_cancel_events(session, order_id)
        assert len(events) == 1
        assert events[0].actor == "api"
        assert events[0].from_state == "placed"
        assert events[0].to_state == "cancelled"
        assert _invalid_evidence(session, order_id) == []
        work = session.scalars(select(WorkItem).where(WorkItem.order_id == order_id)).all()
        assert work
        assert all(item.status == "cancelled" for item in work)


def test_cancel_from_confirmed_applies_event(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        order = _place_in_session(session)
        _set_state(session, order, "confirmed")
        order_id = order.id
        result = cancel_order(session, order_id)
        assert result.outcome is CancelOutcome.APPLIED
        assert result.order.state == "cancelled"

    with session_factory() as session:
        stored = session.get(Order, order_id)
        assert stored is not None
        assert stored.state == "cancelled"
        events = _applied_cancel_events(session, order_id)
        assert len(events) == 1
        assert events[0].from_state == "confirmed"
        assert events[0].to_state == "cancelled"
        assert events[0].applied is True
        assert _invalid_evidence(session, order_id) == []


def test_cancel_from_confirmed_cancels_parked_work(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        order = _place_in_session(session)
        _set_state(session, order, "confirmed")
        item = session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)).one()
        item.status = "parked"
        item.park_owner = "worker-old"
        item.park_reason = "poll_budget_exhausted"
        item.park_next_action = "redrive"
        order_id = order.id

        result = cancel_order(session, order_id)
        assert result.outcome is CancelOutcome.APPLIED

    with session_factory() as session:
        item = session.scalars(select(WorkItem).where(WorkItem.order_id == order_id)).one()
        assert item.status == "cancelled"
        assert item.lease_owner is None
        assert item.lease_until is None
        assert item.park_owner is None
        assert item.park_reason is None
        assert item.park_next_action is None


def test_cancel_after_being_prepared_is_rejected_with_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        order = _place_in_session(session)
        _set_state(session, order, "being_prepared")
        for item in session.scalars(select(WorkItem).where(WorkItem.order_id == order.id)):
            item.status = "completed"
        order_id = order.id
        version = order.version
        result = cancel_order(session, order_id)
        assert result.outcome is CancelOutcome.REJECTED
        assert result.order.state == "being_prepared"

    with session_factory() as session:
        stored = session.get(Order, order_id)
        assert stored is not None
        assert stored.state == "being_prepared"
        assert stored.version == version
        evidence = _invalid_evidence(session, order_id)
        assert len(evidence) == 1
        assert evidence[0].actor == "api"
        assert evidence[0].from_state == "being_prepared"
        assert evidence[0].to_state == "cancelled"
        assert evidence[0].applied is False
        assert _applied_cancel_events(session, order_id) == []


def test_cancel_already_cancelled_replays_without_resurrecting(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory.begin() as session:
        order = _place_in_session(session)
        first = cancel_order(session, order.id)
        assert first.outcome is CancelOutcome.APPLIED
        second = cancel_order(session, order.id)
        assert second.outcome is CancelOutcome.REPLAY
        assert second.order.state == "cancelled"
        order_id = order.id

    with session_factory() as session:
        stored = session.get(Order, order_id)
        assert stored is not None
        assert stored.state == "cancelled"
        assert len(_applied_cancel_events(session, order_id)) == 1
        assert _invalid_evidence(session, order_id) == []


def test_cancel_missing_order_raises(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        with pytest.raises(OrderNotFound):
            cancel_order(session, uuid.uuid4())


def _http_get(order_id: str) -> httpx.Response:
    try:
        return httpx.get(f"{API_URL}/orders/{order_id}", timeout=5.0)
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")


def _http_cancel(order_id: str) -> httpx.Response:
    try:
        return httpx.post(f"{API_URL}/orders/{order_id}/cancel", timeout=5.0)
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")


def _wait_until(order_id: str, wanted: str) -> str:
    deadline = time.monotonic() + WAIT_TIMEOUT_S
    last: str | None = None
    while time.monotonic() < deadline:
        got = _http_get(order_id)
        assert got.status_code == 200, got.text
        state = got.json()["state"]
        assert isinstance(state, str)
        last = state
        if last == wanted:
            return last
        if last in {"failed", "cancelled"}:
            pytest.fail(f"order {order_id} left the walk early at {last}")
        time.sleep(POLL_EVERY_S)
    pytest.fail(f"order {order_id} did not reach {wanted} within {WAIT_TIMEOUT_S}s; last={last}")


def test_http_cancel_missing_order_is_404() -> None:
    response = _http_cancel(str(uuid.uuid4()))
    assert response.status_code == 404, response.text


def test_http_cancel_from_confirmed_applies() -> None:
    """Wait until confirmed so a live worker cannot steal a placed-only cancel."""
    mix_off()
    place_key = f"http-cancel-confirmed-{uuid.uuid4()}"
    try:
        posted = httpx.post(
            f"{API_URL}/orders",
            json={"items": ["burrito"]},
            headers={"Idempotency-Key": place_key},
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")
    assert posted.status_code == 201, posted.text
    order_id = posted.json()["id"]
    _wait_until(order_id, "confirmed")

    cancelled = _http_cancel(order_id)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    fetched = _http_get(order_id)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["state"] == "cancelled"


def test_http_cancel_after_being_prepared_is_409() -> None:
    mix_off()
    place_key = f"http-cancel-pivot-{uuid.uuid4()}"
    try:
        posted = httpx.post(
            f"{API_URL}/orders",
            json={"items": ["chips"]},
            headers={"Idempotency-Key": place_key},
            timeout=5.0,
        )
    except httpx.RequestError as exc:
        pytest.fail(f"API is down: {exc}")
    assert posted.status_code == 201, posted.text
    order_id = posted.json()["id"]
    state = _wait_until(order_id, "being_prepared")
    assert state == "being_prepared"

    rejected = _http_cancel(order_id)
    assert rejected.status_code == 409, rejected.text
    fetched = _http_get(order_id)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["state"] != "cancelled"
    assert fetched.json()["state"] in {
        "being_prepared",
        "ready",
        "out_for_delivery",
        "delivered",
    }
